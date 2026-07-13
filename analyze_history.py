"""
On-demand 90-day frequency analysis.

For every event/conversion found in the last 90 days, computes how many days it
fired, its average/median daily volume, per-weekday medians and whether it
consistently carries monetary value, then suggests whether it is a good
candidate for the 24h check (fires often enough that a zero in 24h is genuinely
anomalous).

Writes two tabs to the Sheet:
- `history_analysis` — one row per event with the stats above. Use it to fill
  the `24hBackGA4_48hBackGAds` column of `config` and to calibrate
  `baseline_threshold_pct` (the daily check WARNs below 50% of the weekday
  median by default). `value_carrying` shows which events get the automatic
  value check in the daily run.
- `daily_history` — a date x event matrix of daily counts, updated in place so
  native Sheets charts built on it survive refreshes.

Also fills the informational Nome_Tag_GTM and GTM_Event_Params columns of
`config` (which GTM tag fires each event, and with which parameters) for
clients with a gtm_container_id. This step degrades gracefully: if the refresh
token lacks the tagmanager.readonly scope or the container is inaccessible, it
prints a warning and the analysis still completes.

Run on-demand or via the weekly-scheduled analyze_history GitHub Actions
workflow; this is NOT part of the daily check.
"""
import os
from datetime import date, timedelta
from statistics import median

from src.baseline import (
    date_range,
    is_value_carrying,
    max_zero_gap,
    pct_days_with_value,
    per_weekday_medians,
    suggest_goback_days,
)
from src.ga4 import fetch_daily_event_data
from src.gads import fetch_conversion_labels, fetch_daily_conversion_data, get_gads_client
from src.gtm import (
    build_event_param_map,
    build_event_tag_map,
    build_gtm_credentials,
    fetch_live_container,
    format_tag_names,
    get_gtm_service,
    resolve_container,
)
from src.sheets import (
    DEFAULT_GOBACK_DAYS,
    GTM_PARAMS_COLUMN,
    GTM_TAG_COLUMN,
    get_sheets_client,
    read_config,
    update_config_columns,
    write_daily_history,
    write_history_analysis,
)
from main import build_credentials, build_client_accounts, TRUTHY

ANALYSIS_DAYS = 90
# Config goback_days wider than suggested by more than this margin is
# reported in the digest as unnecessarily slow detection.
DIGEST_SLOW_MARGIN = 2

WEEKDAY_LABELS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def summarize_events(counts_by_event: dict, values_by_event: dict, client_id: str,
                     display_platform: str, dates: list[str]) -> list[dict]:
    """One stats row per event — shared by the analysis and the onboarding."""
    rows = []
    for event_name, day_map in counts_by_event.items():
        counts = [day_map.get(d, 0) for d in dates]
        days_fired = sum(1 for c in counts if c > 0)
        total = sum(counts)
        pct = days_fired / len(dates)
        weekday_meds = per_weekday_medians(day_map, dates)
        value_map = values_by_event.get(event_name, {})
        _, value_pct = pct_days_with_value(day_map, value_map, dates)
        max_gap = max_zero_gap(day_map, dates)
        rows.append({
            "client_id": client_id,
            "platform": display_platform,
            "event_name": event_name,
            "days_fired_of_90": days_fired,
            "pct_days": f"{round(pct * 100)}%",
            "avg_per_day": round(total / len(dates), 2),
            "median_per_day": round(float(median(counts)), 1),
            "weekday_medians": " · ".join(
                f"{label} {med:g}" for label, med in zip(WEEKDAY_LABELS, weekday_meds)
            ),
            "pct_days_with_value": f"{round(value_pct * 100)}%",
            "value_carrying": "sim" if is_value_carrying(day_map, value_map, dates) else "não",
            "max_gap_days": max_gap,
            "goback_days_sugerido": suggest_goback_days(max_gap),
            # 24h/48h flag is a plain zero-check, so only events that never
            # missed a single day in the window qualify — one observed gap
            # means the flag would false-FAIL on days like it.
            "suggestion_24h": "sim" if max_gap == 0 else "não",
        })
    # Most-frequent first, so the best 24h candidates surface at the top.
    rows.sort(key=lambda r: r["days_fired_of_90"], reverse=True)
    return rows


def _make_gads_client():
    return get_gads_client(
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        developer_token=os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        login_customer_id=os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
    )


def build_gtm_maps_for_client(service, public_id: str, client_id: str) -> tuple[dict, dict] | None:
    """Returns (tag_map, param_map) for a client's live container, or None
    (with a printed reason) when the container can't be read."""
    try:
        container_path = resolve_container(service, public_id)
        if not container_path:
            print(f"GTM mapping: container {public_id} not found/accessible "
                  f"for '{client_id}' — skipping this client.")
            return None
        tags, variables = fetch_live_container(service, container_path)
    except Exception as exc:
        print(f"GTM mapping failed for '{client_id}' ({public_id}): {exc}")
        print("  (If this is an auth error, the refresh token likely lacks the "
              "tagmanager.readonly scope — re-run setup_oauth.py and update the secret.)")
        return None

    tag_map, dynamic = build_event_tag_map(tags)
    for tag_name, expr in dynamic:
        print(f"GTM mapping: tag '{tag_name}' has a dynamic event name ({expr}) "
              f"— can't be matched deterministically.")
    return tag_map, build_event_param_map(tags, variables)


def map_gtm_tags(config_rows: list[dict], sheet_id: str, sheets_client) -> None:
    """Fills config's Nome_Tag_GTM + GTM_Event_Params columns for clients with
    a gtm_container_id.

    Purely informational; every failure here is printed and swallowed so the
    analysis job never fails because of the GTM step.
    """
    containers: dict[str, str] = {}
    for row in config_rows:
        container_id = str(row.get("gtm_container_id", "")).strip()
        if container_id and row["client_id"] not in containers:
            containers[row["client_id"]] = container_id

    if not containers:
        print("GTM mapping: no gtm_container_id in config — skipping.")
        return

    try:
        service = get_gtm_service(build_gtm_credentials())
    except Exception as exc:
        print(f"GTM mapping skipped (could not build GTM client): {exc}")
        return

    values_by_key: dict[tuple[str, str, str], list[dict[str, str]]] = {}

    for client_id, public_id in containers.items():
        maps = build_gtm_maps_for_client(service, public_id, client_id)
        if maps is None:
            continue
        tag_map, param_map = maps

        # GAds side needs the conversion label of each conversion action.
        labels_by_name: dict[str, str] = {}
        gads_account = next(
            (str(r["account_id"]) for r in config_rows
             if r["client_id"] == client_id and r["platform"].upper() == "GADS"),
            None,
        )
        if gads_account and any(key[0] == "GADS" for key in tag_map):
            try:
                labels_by_name = fetch_conversion_labels(gads_account, _make_gads_client())
            except Exception as exc:
                print(f"GTM mapping: could not fetch GAds conversion labels "
                      f"for '{client_id}': {exc}")

        for row in config_rows:
            if row["client_id"] != client_id:
                continue
            platform = row["platform"].upper()
            event_name = row["event_name"]
            if platform == "GA4":
                lookup_key = ("GA4", event_name)
            elif platform == "GADS":
                label = labels_by_name.get(event_name, "")
                lookup_key = ("GADS", label) if label else None
            else:
                continue
            tag_names = tag_map.get(lookup_key, []) if lookup_key else []
            if tag_names:
                # One entry per active tag — update_config_columns puts each
                # on its own config row (inserting duplicates when needed).
                values_by_key[(client_id, platform, event_name)] = [
                    {GTM_TAG_COLUMN: tag,
                     GTM_PARAMS_COLUMN: param_map.get((lookup_key, tag), "")}
                    for tag in tag_names
                ]
            else:
                values_by_key[(client_id, platform, event_name)] = [
                    {GTM_TAG_COLUMN: format_tag_names([]), GTM_PARAMS_COLUMN: ""}
                ]

    if values_by_key:
        try:
            updated = update_config_columns(sheet_id, sheets_client, values_by_key)
        except Exception as exc:
            print(f"GTM mapping: failed writing GTM columns to config: {exc}")
            return
        if updated:
            print(f"GTM mapping: updated {updated} GTM cell(s) in config.")
        else:
            print(f"GTM mapping: columns {GTM_TAG_COLUMN}/{GTM_PARAMS_COLUMN} not found "
                  "in config — add them to the Sheet header to enable writing.")


def find_config_divergences(config_rows: list[dict], all_rows: list[dict]) -> list[str]:
    """Deterministic comparison of the config's goback_days / 24h flags
    against the 90-day gap analysis. Returns one human-readable line per
    divergence (empty list = config aligned with history)."""
    stats_by_key = {
        (r["client_id"], r["platform"].upper(), r["event_name"]): r
        for r in all_rows
    }
    divergences = []
    for row in config_rows:
        key = (row["client_id"], row["platform"].upper(), row["event_name"])
        stats = stats_by_key.get(key)
        if not stats:
            continue
        max_gap = stats["max_gap_days"]
        suggested = stats["goback_days_sugerido"]
        try:
            cfg_goback = int(row["goback_days"]) if row.get("goback_days") else DEFAULT_GOBACK_DAYS
        except (TypeError, ValueError):
            cfg_goback = DEFAULT_GOBACK_DAYS
        flagged = str(row.get("24hBackGA4_48hBackGAds", "")).strip().lower() in TRUTHY
        label = f"{row['client_id']} | {stats['platform']} | {row['event_name']}"

        if flagged and max_gap > 0:
            divergences.append(
                f"{label}: flag 24h/48h ativo, mas o evento esteve até {max_gap} dia(s) "
                f"seguidos a zero nos últimos 90 — o flag vai dar falsos FAIL em dias assim; "
                f"sugerido: remover o flag e usar goback_days={suggested}.")
        if cfg_goback < suggested:
            divergences.append(
                f"{label}: goback_days={cfg_goback}, mas o maior gap real foi {max_gap} dia(s) "
                f"(sugerido {suggested}) — risco de falso alarme.")
        elif cfg_goback > suggested + DIGEST_SLOW_MARGIN:
            divergences.append(
                f"{label}: goback_days={cfg_goback} vs sugerido {suggested} — uma morte real "
                f"do evento pode demorar até {cfg_goback} dias a ser detetada.")
    return divergences


DIGEST_PROMPT = """És um especialista em web analytics da agência Karma. O monitor de tracking
comparou a configuração atual com a análise dos últimos 90 dias e encontrou as
divergências abaixo (uma por linha, já com os números certos).

Reescreve isto como um digest Slack curto em português de Portugal: agrupa por
cliente, mantém EXATAMENTE os números e nomes de eventos, e termina com uma
recomendação de ação concreta por cliente (1 frase). Sem introdução nem
conclusão genérica. Usa • como bullet.

Divergências:
{lines}
"""


def send_weekly_digest(config_rows: list[dict], all_rows: list[dict]) -> None:
    """Posts the goback_days divergence digest to Slack. Detection is
    deterministic; Gemini (when configured) only phrases the message — on any
    Gemini failure the plain deterministic list is posted instead. No
    divergences -> no message. Fail-safe: never fails the job."""
    try:
        slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        if not slack_webhook:
            print("Digest: SLACK_WEBHOOK_URL not configured — skipping.")
            return

        divergences = find_config_divergences(config_rows, all_rows)
        if not divergences:
            print("Digest: config alinhada com o histórico — nada a reportar.")
            return

        body = "\n".join(f"• {line}" for line in divergences)
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if api_key:
            try:
                from triage import call_gemini
                phrased = call_gemini(api_key, DIGEST_PROMPT.format(lines=body))
                if phrased:
                    body = phrased
            except Exception as exc:
                print(f"Digest: Gemini indisponível ({exc}) — a enviar versão plain.")

        import requests
        message = (f"📋 *Digest semanal — goback_days vs histórico (90d)* "
                   f"({len(divergences)} divergência(s))\n{body}")
        requests.post(slack_webhook, json={"text": message}, timeout=10).raise_for_status()
        print(f"Digest: {len(divergences)} divergência(s) enviadas para o Slack.")
    except Exception as exc:
        print(f"Digest failed (analysis unaffected): {exc}")


def main() -> None:
    sheet_id = os.environ["GOOGLE_SHEET_ID"]

    credentials = build_credentials()
    sheets_client = get_sheets_client(credentials)
    config_rows = read_config(sheet_id, sheets_client)
    client_accounts = build_client_accounts(config_rows)

    dates = date_range(date.today() - timedelta(days=1), ANALYSIS_DAYS)

    all_rows = []
    all_series: dict[str, dict[str, float]] = {}
    for client_id, platforms in client_accounts.items():
        print(f"Analyzing {client_id} (last {ANALYSIS_DAYS} days)...")

        if "GA4" in platforms:
            counts, values = fetch_daily_event_data(platforms["GA4"], credentials, days=ANALYSIS_DAYS)
            all_rows.extend(summarize_events(counts, values, client_id, "GA4", dates))
            for event_name, day_map in counts.items():
                all_series[f"{client_id}|GA4|{event_name}"] = day_map

        if "GADS" in platforms:
            gads_client = _make_gads_client()
            counts, values = fetch_daily_conversion_data(platforms["GADS"], gads_client, days=ANALYSIS_DAYS)
            all_rows.extend(summarize_events(counts, values, client_id, "GAds", dates))
            for event_name, day_map in counts.items():
                all_series[f"{client_id}|GAds|{event_name}"] = day_map

    write_history_analysis(sheet_id, sheets_client, all_rows)
    write_daily_history(sheet_id, sheets_client, dates, all_series)
    suggested = sum(1 for r in all_rows if r["suggestion_24h"] == "sim")
    print(f"Wrote {len(all_rows)} rows to history_analysis ({suggested} suggested for 24h).")
    print(f"Wrote daily_history: {len(dates)} days x {len(all_series)} series.")

    map_gtm_tags(config_rows, sheet_id, sheets_client)
    send_weekly_digest(config_rows, all_rows)


if __name__ == "__main__":
    main()
