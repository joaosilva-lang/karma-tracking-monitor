import json
import os
from datetime import date, timedelta

from google.oauth2.credentials import Credentials

from src.baseline import (
    BASELINE_DAYS,
    BASELINE_MIN_MEDIAN,
    RECORD_SILENCE_MIN_FIRED_DAYS,
    date_range,
    days_since_last_firing,
    is_value_carrying,
    max_zero_gap,
    parse_threshold,
    short_check_status,
    weekday_median,
    window_count,
)
from src.ga4 import fetch_daily_event_data
from src.gads import fetch_daily_conversion_data, get_gads_client
from src.sheets import (
    DEFAULT_GOBACK_DAYS,
    get_sheets_client,
    read_config,
    write_daily_history,
    write_results,
)
from src.slack import send_alert

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/adwords",
]

# Most recent *stable* day per platform. GA4 data for yesterday is settled; a
# Google Ads conversion can take 24-72h to be fully attributed, so the short
# check tests the day before yesterday instead of yesterday.
STABLE_DAY_OFFSET = {"GA4": 1, "GADS": 2}
WINDOW_24H_LABEL = {"GA4": "24h", "GADS": "48h"}

TRUTHY = {"sim", "yes", "true", "1", "y", "s"}

# Written when critical issues exist, consumed by triage.py (which is a no-op
# unless GEMINI_API_KEY is configured).
TRIAGE_INPUT_FILE = "triage_input.json"
TRIAGE_HISTORY_DAYS = 30


def build_credentials() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def build_client_accounts(config_rows: list[dict]) -> dict:
    """Returns {client_id: {platform: account_id}} derived from Sheet config rows.

    The last row wins per (client, platform) — so differing account_ids for
    the same pair are almost certainly a typo in a duplicated row, and would
    silently point the checks at the wrong account. Warn loudly.
    """
    accounts: dict[str, dict[str, str]] = {}
    for row in config_rows:
        client_id = row["client_id"]
        platform = row["platform"].upper()
        account_id = str(row["account_id"])
        previous = accounts.setdefault(client_id, {}).get(platform)
        if previous is not None and previous != account_id:
            print(f"⚠️ config: '{client_id}' {platform} tem account_ids diferentes "
                  f"({previous} vs {account_id}) — a usar {account_id}. Corrige a config.")
        accounts[client_id][platform] = account_id
    return accounts


def get_event_config(config_rows: list[dict], client_id: str, platform: str, event_name: str) -> dict:
    """Returns {severity, goback_days, threshold} for an event, with defaults."""
    for row in config_rows:
        if (row["client_id"] == client_id
                and row["platform"].upper() == platform.upper()
                and row["event_name"] == event_name):
            return {
                "severity": row.get("severity", "secondary") or "secondary",
                "goback_days": int(row["goback_days"]) if row.get("goback_days") else DEFAULT_GOBACK_DAYS,
                "threshold": parse_threshold(row.get("baseline_threshold_pct")),
            }
    return {
        "severity": "secondary",
        "goback_days": DEFAULT_GOBACK_DAYS,
        "threshold": parse_threshold(None),
    }


def get_24h_events(config_rows: list[dict], client_id: str, platform: str) -> set:
    """Returns the set of event names flagged 24hBackGA4_48hBackGAds='sim' for a platform."""
    flagged = set()
    for row in config_rows:
        if (row["client_id"] == client_id and row["platform"].upper() == platform.upper()
                and str(row.get("24hBackGA4_48hBackGAds", "")).strip().lower() in TRUTHY):
            flagged.add(row["event_name"])
    return flagged


def get_params_check_override(config_rows: list[dict], client_id: str,
                              platform: str, event_name: str) -> list[str] | None:
    """Returns the params_check override for an event (comma-separated names,
    parsed and stripped), or None to use the automatic GTM-derived list. An
    empty/absent column means None — never "check nothing"."""
    for row in config_rows:
        if (row["client_id"] == client_id and row["platform"].upper() == platform.upper()
                and row["event_name"] == event_name):
            raw = str(row.get("params_check", "")).strip()
            return [p.strip() for p in raw.split(",") if p.strip()] if raw else None
    return None


def _fetch_daily(account_id: str, platform: str, credentials: Credentials,
                 gads_client=None) -> tuple[dict, dict]:
    """Returns (counts, values) maps: {event: {date: n}} over BASELINE_DAYS."""
    if platform == "GA4":
        return fetch_daily_event_data(account_id, credentials, days=BASELINE_DAYS)
    return fetch_daily_conversion_data(account_id, gads_client, days=BASELINE_DAYS)


def _row(client_id: str, platform: str, event_name: str, severity: str,
         window: str, count: float, status: str, expected: float = None,
         check: str = "count", history: dict = None, dry_days: int = None) -> dict:
    return {
        "client_id": client_id,
        "platform": platform,
        "event_name": event_name,
        "check": check,
        "severity": severity,
        "window": window,
        "count": round(count, 2) if check == "value" else int(round(count)),
        "status": status,
        "expected": round(expected, 1) if expected is not None else "",
        # Not written to the Sheet — carried along for Slack/triage context.
        "history": history or {},
        # Set only on record-silence WARN rows (current dry spell length).
        "dry_days": dry_days,
    }


def _check_platform(client_id: str, platform: str, display_name: str, account_id: str,
                    credentials: Credentials, config_rows: list[dict],
                    gads_client=None) -> tuple[list[dict], dict[str, dict]]:
    """Runs all checks for one platform from a single 90-day daily fetch.

    The wide goback window, the short 24h/48h check and the weekday-median
    baseline are all computed locally from the same {event: {date: count}}
    matrix — one API call per (client, platform).

    Returns (result_rows, daily_counts). The raw matrix is handed back because
    it is exactly what daily_history stores: the caller writes it to the Sheet
    instead of throwing away 90 days of data already paid for.
    """
    daily_counts, daily_values = _fetch_daily(
        account_id, platform, credentials, gads_client=gads_client
    )

    configured = {
        row["event_name"] for row in config_rows
        if row["client_id"] == client_id and row["platform"].upper() == platform
    }
    # Union with configured events so a fully dead event still produces a FAIL
    # row instead of silently vanishing from the API response.
    all_events = sorted(set(daily_counts) | configured)

    yesterday = date.today() - timedelta(days=1)
    test_day = date.today() - timedelta(days=STABLE_DAY_OFFSET[platform])
    flagged = get_24h_events(config_rows, client_id, platform)
    all_dates = date_range(yesterday, BASELINE_DAYS)

    def recent(day_map: dict) -> dict:
        return {d: day_map[d] for d in date_range(yesterday, TRIAGE_HISTORY_DAYS) if d in day_map}

    results = []
    for event_name in all_events:
        cfg = get_event_config(config_rows, client_id, platform, event_name)
        day_map = daily_counts.get(event_name, {})
        value_map = daily_values.get(event_name, {})
        history = recent(day_map)

        expected = weekday_median(day_map, test_day)

        # --- Wide goback-window check (one row per event) ---
        # FAIL: zero events in the whole window (as always). WARN (record
        # silence): the CURRENT dry spell is longer than any silence observed
        # between firings in 90 days — the earliest statistically defensible
        # death signal for sporadic events, long before the FAIL at
        # goback_days. Guards: only events without the automatic baseline
        # (high-volume ones already FAIL same-day via the short check), not
        # 24h-flagged (their zero-check fires same-day too), and with enough
        # fired days for the record to mean anything.
        goback_days = min(cfg["goback_days"], BASELINE_DAYS)
        wide_count = window_count(day_map, yesterday, goback_days)
        wide_status, record_gap, dry = "OK" if wide_count > 0 else "FAIL", None, None
        if (wide_status == "OK" and expected < BASELINE_MIN_MEDIAN
                and event_name not in flagged):
            stable_dates = date_range(test_day, BASELINE_DAYS)
            fired_days = sum(1 for d in stable_dates if day_map.get(d, 0) > 0)
            if fired_days >= RECORD_SILENCE_MIN_FIRED_DAYS:
                gap = max_zero_gap(day_map, stable_dates)
                current_dry = days_since_last_firing(day_map, stable_dates)
                if current_dry is not None and current_dry > gap:
                    wide_status, record_gap, dry = "WARN", gap, current_dry
        results.append(_row(
            client_id, display_name, event_name, cfg["severity"],
            f"{goback_days}d", wide_count, wide_status,
            expected=record_gap, dry_days=dry, history=history,
        ))

        # --- Short check: automatic baseline above the volume floor, plain
        # zero-check for events explicitly flagged 24hBackGA4_48hBackGAds=sim ---
        if expected >= BASELINE_MIN_MEDIAN:
            count = day_map.get(test_day.isoformat(), 0)
            status = short_check_status(count, expected, cfg["threshold"])
            results.append(_row(
                client_id, display_name, event_name, cfg["severity"],
                WINDOW_24H_LABEL[platform], count, status, expected=expected,
                history=history,
            ))
        elif event_name in flagged:
            count = window_count(day_map, yesterday, STABLE_DAY_OFFSET[platform])
            results.append(_row(
                client_id, display_name, event_name, cfg["severity"],
                WINDOW_24H_LABEL[platform], count, "OK" if count > 0 else "FAIL",
                history=history,
            ))

        # --- Value check: only for events whose history proves they carry
        # value. Binary (count>0 but value==0), no baseline — zero false
        # positives by construction. This is the original Revenue incident. ---
        if is_value_carrying(day_map, value_map, all_dates):
            value_history = recent(value_map)
            wide_value = window_count(value_map, yesterday, goback_days)
            results.append(_row(
                client_id, display_name, event_name, cfg["severity"],
                f"{goback_days}d", wide_value,
                "FAIL" if wide_count > 0 and wide_value == 0 else "OK",
                check="value", history=value_history,
            ))
            day_count = day_map.get(test_day.isoformat(), 0)
            day_value = value_map.get(test_day.isoformat(), 0)
            results.append(_row(
                client_id, display_name, event_name, cfg["severity"],
                WINDOW_24H_LABEL[platform], day_value,
                "FAIL" if day_count > 0 and day_value == 0 else "OK",
                check="value", history=value_history,
            ))

    return results, daily_counts


def run_checks(client_id: str, platforms: dict[str, str], credentials: Credentials,
               config_rows: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """Returns (result_rows, daily series keyed 'client|Platform|event').

    The series key matches the one analyze_history.py builds, so both jobs
    write daily_history in the same shape and either can refresh it.
    """
    results = []
    series: dict[str, dict[str, float]] = {}

    def collect(display_name: str, platform_output: tuple[list[dict], dict[str, dict]]) -> None:
        platform_results, daily_counts = platform_output
        results.extend(platform_results)
        for event_name, day_map in daily_counts.items():
            series[f"{client_id}|{display_name}|{event_name}"] = day_map

    if "GA4" in platforms:
        collect("GA4", _check_platform(
            client_id, "GA4", "GA4", platforms["GA4"], credentials, config_rows,
        ))

    if "GADS" in platforms:
        gads_client = get_gads_client(
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            developer_token=os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            login_customer_id=os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        )
        collect("GAds", _check_platform(
            client_id, "GADS", "GAds", platforms["GADS"], credentials, config_rows,
            gads_client=gads_client
        ))

    return results, series


def main() -> None:
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")

    credentials = build_credentials()
    sheets_client = get_sheets_client(credentials)
    config_rows = read_config(sheet_id, sheets_client)
    client_accounts = build_client_accounts(config_rows)

    all_results = []
    all_series: dict[str, dict[str, float]] = {}
    for client_id, platforms in client_accounts.items():
        print(f"Checking {client_id}...")
        results, series = run_checks(client_id, platforms, credentials, config_rows)
        all_results.extend(results)
        all_series.update(series)

    write_results(sheet_id, sheets_client, all_results)

    critical_issues = [
        r for r in all_results
        if r["status"] in ("FAIL", "WARN") and r["severity"] == "critical"
    ]

    if critical_issues and slack_webhook:
        send_alert(slack_webhook, critical_issues)
        print(f"Slack alert sent: {len(critical_issues)} critical issue(s).")
    elif critical_issues:
        print(f"WARNING: {len(critical_issues)} critical issue(s) found but no Slack webhook configured.")
    else:
        print("All checks passed.")

    if critical_issues:
        write_triage_input(critical_issues, config_rows)

    # Last, and fail-safe. The 90-day matrix is already in memory (the checks ran
    # off it), so this is a write, not a fetch — it keeps daily_history fresh
    # every day instead of only on Mondays, which is what the dashboard reads.
    # It feeds a view, never an alert: a transient Sheets error here must not
    # turn a delivered alert into a red job, nor skip the triage step (which the
    # workflow only runs on success). Same fail-safe stance as the triage agent.
    try:
        dates = date_range(date.today() - timedelta(days=1), BASELINE_DAYS)
        write_daily_history(sheet_id, sheets_client, dates, all_series)
        print(f"Wrote daily_history: {len(dates)} days x {len(all_series)} series.")
    except Exception as error:
        print(f"WARNING: daily_history not written ({error}). Checks and alerts unaffected.")


def write_triage_input(critical_issues: list[dict], config_rows: list[dict]) -> None:
    """Dumps critical issues (+ per-client GTM container) for triage.py."""
    containers = {}
    for row in config_rows:
        container_id = str(row.get("gtm_container_id", "")).strip()
        if container_id and row["client_id"] not in containers:
            containers[row["client_id"]] = container_id

    payload = {
        "generated_at_day": date.today().isoformat(),
        "gtm_containers": containers,
        "issues": critical_issues,
    }
    with open(TRIAGE_INPUT_FILE, "w") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f"Wrote {TRIAGE_INPUT_FILE} for triage ({len(critical_issues)} issue(s)).")


if __name__ == "__main__":
    main()
