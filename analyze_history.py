"""
On-demand 90-day frequency analysis.

For every event/conversion found in the last 90 days, computes how many days it
fired and its average daily volume, then suggests whether it is a good candidate
for the 24h check (fires often enough that a zero in 24h is genuinely anomalous).

Writes the result to the `history_analysis` tab of the Sheet. You then copy the
`suggestion_24h` value into the `24hours_lookback` column of the `config` tab for
the events you want monitored at 24h.

Run on-demand (locally or via the analyze_history GitHub Actions workflow); this
is NOT part of the daily check.
"""
import os

from src.ga4 import fetch_daily_event_counts
from src.gads import fetch_daily_conversion_counts, get_gads_client
from src.sheets import get_sheets_client, read_config, write_history_analysis
from main import build_credentials, build_client_accounts

ANALYSIS_DAYS = 90
SUGGESTION_THRESHOLD = 0.80  # fired on >= 80% of days -> suggest 'sim'


def _summarize(daily: dict, client_id: str, display_platform: str) -> list[dict]:
    rows = []
    for event_name, day_map in daily.items():
        days_fired = sum(1 for c in day_map.values() if c > 0)
        total = sum(day_map.values())
        pct = days_fired / ANALYSIS_DAYS
        rows.append({
            "client_id": client_id,
            "platform": display_platform,
            "event_name": event_name,
            "days_fired_of_90": days_fired,
            "pct_days": f"{round(pct * 100)}%",
            "avg_per_day": round(total / ANALYSIS_DAYS, 2),
            "suggestion_24h": "sim" if pct >= SUGGESTION_THRESHOLD else "não",
        })
    # Most-frequent first, so the best 24h candidates surface at the top.
    rows.sort(key=lambda r: r["days_fired_of_90"], reverse=True)
    return rows


def main() -> None:
    sheet_id = os.environ["GOOGLE_SHEET_ID"]

    credentials = build_credentials()
    sheets_client = get_sheets_client(credentials)
    config_rows = read_config(sheet_id, sheets_client)
    client_accounts = build_client_accounts(config_rows)

    all_rows = []
    for client_id, platforms in client_accounts.items():
        print(f"Analyzing {client_id} (last {ANALYSIS_DAYS} days)...")

        if "GA4" in platforms:
            daily = fetch_daily_event_counts(platforms["GA4"], credentials, days=ANALYSIS_DAYS)
            all_rows.extend(_summarize(daily, client_id, "GA4"))

        if "GADS" in platforms:
            gads_client = get_gads_client(
                client_id=os.environ["GOOGLE_CLIENT_ID"],
                client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
                developer_token=os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
                login_customer_id=os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
            )
            daily = fetch_daily_conversion_counts(platforms["GADS"], gads_client, days=ANALYSIS_DAYS)
            all_rows.extend(_summarize(daily, client_id, "GAds"))

    write_history_analysis(sheet_id, sheets_client, all_rows)
    suggested = sum(1 for r in all_rows if r["suggestion_24h"] == "sim")
    print(f"Wrote {len(all_rows)} rows to history_analysis ({suggested} suggested for 24h).")


if __name__ == "__main__":
    main()
