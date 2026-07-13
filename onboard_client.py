"""
Client onboarding: validate access, analyze 90 days, propose a config.

Reads the new client's identifiers from env vars (set by the onboard_client
GitHub Actions workflow inputs, or exported locally):
- ONBOARD_CLIENT_ID       required, free identifier (e.g. "acme")
- ONBOARD_GA4_PROPERTY    GA4 property ID (optional)
- ONBOARD_GADS_CUSTOMER   Google Ads customer ID (optional; digits, dashes ok)
- ONBOARD_GTM_CONTAINER   GTM public container ID, e.g. GTM-ABC123 (optional)

At least one of GA4/GAds is required. Each access is validated with a clear
error before any writing happens. The result is written to the
`config_proposta` tab — one row per discovered event with suggested flags and
stats — for human review; NOTHING is written to the real config tab. After
reviewing (promote severities to critical where it matters), copy the config
columns into `config`.
"""
import os
import sys
from datetime import date, timedelta

from src.baseline import date_range
from src.ga4 import fetch_daily_event_data
from src.gads import fetch_conversion_labels, fetch_daily_conversion_data
from src.gtm import build_gtm_credentials, format_tag_names, get_gtm_service
from src.sheets import get_sheets_client, write_config_proposal, PROPOSAL_TAB
from main import build_credentials
from analyze_history import (
    ANALYSIS_DAYS,
    _make_gads_client,
    build_gtm_maps_for_client,
    summarize_events,
)

# Only suggest the manual short-check flag for events below the automatic
# baseline floor — above it the daily check already covers them automatically.
FLAG_SUGGESTION_MAX_MEDIAN = 10


def main() -> None:
    client_id = os.environ.get("ONBOARD_CLIENT_ID", "").strip()
    ga4_property = os.environ.get("ONBOARD_GA4_PROPERTY", "").strip()
    gads_customer = os.environ.get("ONBOARD_GADS_CUSTOMER", "").strip().replace("-", "")
    gtm_container = os.environ.get("ONBOARD_GTM_CONTAINER", "").strip()

    if not client_id:
        sys.exit("❌ ONBOARD_CLIENT_ID é obrigatório.")
    if not ga4_property and not gads_customer:
        sys.exit("❌ Indica pelo menos uma plataforma (ONBOARD_GA4_PROPERTY e/ou ONBOARD_GADS_CUSTOMER).")

    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    credentials = build_credentials()
    dates = date_range(date.today() - timedelta(days=1), ANALYSIS_DAYS)

    # --- Validate access + fetch, one platform at a time, failing loudly ---
    platform_data: dict[str, tuple[dict, dict, str]] = {}

    if ga4_property:
        print(f"A validar acesso GA4 à property {ga4_property}...")
        try:
            counts, values = fetch_daily_event_data(ga4_property, credentials, days=ANALYSIS_DAYS)
        except Exception as exc:
            sys.exit(f"❌ GA4: sem acesso à property {ga4_property}: {exc}\n"
                     f"   Confirma o ID e que a conta OAuth tem leitura na property.")
        print(f"✅ GA4 OK — {len(counts)} eventos nos últimos {ANALYSIS_DAYS} dias.")
        platform_data["GA4"] = (counts, values, ga4_property)

    if gads_customer:
        print(f"A validar acesso Google Ads ao customer {gads_customer}...")
        try:
            gads_client = _make_gads_client()
            counts, values = fetch_daily_conversion_data(gads_customer, gads_client, days=ANALYSIS_DAYS)
        except Exception as exc:
            sys.exit(f"❌ GAds: sem acesso ao customer {gads_customer}: {exc}\n"
                     f"   Confirma o ID e que a conta está sob a MCC da Karma.")
        print(f"✅ GAds OK — {len(counts)} conversões nos últimos {ANALYSIS_DAYS} dias.")
        platform_data["GADS"] = (counts, values, gads_customer)

    # --- GTM (informational — a failure warns but doesn't abort) ---
    tag_map: dict = {}
    param_map: dict = {}
    gtm_ok = False
    if gtm_container:
        print(f"A validar acesso GTM ao container {gtm_container}...")
        try:
            service = get_gtm_service(build_gtm_credentials())
            maps = build_gtm_maps_for_client(service, gtm_container, client_id)
        except Exception as exc:
            maps = None
            print(f"⚠️ GTM indisponível ({exc}) — proposta segue sem colunas GTM.")
        if maps:
            tag_map, param_map = maps
            gtm_ok = True
            print(f"✅ GTM OK — {len(tag_map)} eventos/conversões com tag na versão live.")

    labels_by_name: dict[str, str] = {}
    if gtm_ok and "GADS" in platform_data and any(key[0] == "GADS" for key in tag_map):
        try:
            labels_by_name = fetch_conversion_labels(gads_customer, _make_gads_client())
        except Exception as exc:
            print(f"⚠️ Sem labels das conversões GAds ({exc}) — matching GTM só em GA4.")

    # --- Build the proposal ---
    proposal = []
    for platform, display in (("GA4", "GA4"), ("GADS", "GAds")):
        if platform not in platform_data:
            continue
        counts_by_event, values_by_event, account_id = platform_data[platform]
        for stats in summarize_events(counts_by_event, values_by_event, client_id, display, dates):
            event_name = stats["event_name"]

            if platform == "GA4":
                lookup_key = ("GA4", event_name)
            else:
                label = labels_by_name.get(event_name, "")
                lookup_key = ("GADS", label) if label else None
            tag_names = tag_map.get(lookup_key, []) if lookup_key else []

            below_floor = stats["median_per_day"] < FLAG_SUGGESTION_MAX_MEDIAN
            flag = "sim" if stats["suggestion_24h"] == "sim" and below_floor else ""

            # One proposal row PER active tag (stats repeated) — mirrors the
            # one-tag-per-row layout of config. No tags -> a single row.
            for tag in (tag_names or [None]):
                proposal.append({
                    "client_id": client_id,
                    "account_id": account_id,
                    "platform": display,
                    "event_name": event_name,
                    "severity": "secondary",
                    # Pre-filled from the gap analysis (longest dry spell
                    # x1.5), so reviewed rows are copy-paste-ready; adjust
                    # before copying if you disagree.
                    "goback_days": stats["goback_days_sugerido"],
                    "24hBackGA4_48hBackGAds": flag,
                    "baseline_threshold_pct": "",
                    "gtm_container_id": gtm_container,
                    "Nome_Tag_GTM": (tag or format_tag_names([])) if gtm_ok else "",
                    "GTM_Event_Params": (param_map.get((lookup_key, tag), "") if tag else ""),
                    "median_per_day": stats["median_per_day"],
                    "pct_days": stats["pct_days"],
                    "pct_days_with_value": stats["pct_days_with_value"],
                    "value_carrying": stats["value_carrying"],
                    "max_gap_days": stats["max_gap_days"],
                    "goback_days_sugerido": stats["goback_days_sugerido"],
                    "weekday_medians": stats["weekday_medians"],
                })

    sheets_client = get_sheets_client(credentials)
    write_config_proposal(sheet_id, sheets_client, proposal)

    print(f"\n✅ Proposta escrita na aba '{PROPOSAL_TAB}': {len(proposal)} eventos.")
    print("Próximos passos:")
    print("  1. Rever a proposta — promover a 'critical' os eventos que importam.")
    print("  2. Copiar as linhas revistas (colunas da config) para a aba 'config'.")
    print("  3. O próximo check diário passa a incluir o cliente novo.")


if __name__ == "__main__":
    main()
