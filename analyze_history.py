"""
On-demand 90-day frequency analysis.

For every event/conversion found in the last 90 days, computes how many days it
fired, its average/median daily volume and per-weekday medians, then suggests
whether it is a good candidate for the 24h check (fires often enough that a
zero in 24h is genuinely anomalous).

Writes two tabs to the Sheet:
- `history_analysis` — one row per event with the stats above. Use it to fill
  the `24hBackGA4_48hBackGAds` column of `config` and to calibrate
  `baseline_threshold_pct` (the daily check WARNs below 50% of the weekday
  median by default).
- `daily_history` — a date x event matrix of daily counts, updated in place so
  native Sheets charts built on it survive refreshes.

Run on-demand (locally or via the analyze_history GitHub Actions workflow); this
is NOT part of the daily check.
"""
import os
from datetime import date, timedelta
from statistics import median

from src.baseline import date_range, per_weekday_medians
from src.ga4 import fetch_daily_event_counts
from src.gads import fetch_daily_conversion_counts, get_gads_client
from src.sheets import (
    get_sheets_client,
    read_config,
    write_daily_history,
    write_history_analysis,
)
from main import build_credentials, build_client_accounts

ANALYSIS_DAYS = 90
SUGGESTION_THRESHOLD = 0.80  # fired on >= 80% of days -> suggest 'sim'

WEEKDAY_LABELS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def _summarize(daily: dict, client_id: str, display_platform: str,
               dates: list[str]) -> list[dict]:
    rows = []
    for event_name, day_map in daily.items():
        counts = [day_map.get(d, 0) for d in dates]
        days_fired = sum(1 for c in counts if c > 0)
        total = sum(counts)
        pct = days_fired / ANALYSIS_DAYS
        weekday_meds = per_weekday_medians(day_map, dates)
        rows.append({
            "client_id": client_id,
            "platform": display_platform,
            "event_name": event_name,
            "days_fired_of_90": days_fired,
            "pct_days": f"{round(pct * 100)}%",
            "avg_per_day": round(total / ANALYSIS_DAYS, 2),
            "median_per_day": round(float(median(counts)), 1),
            "weekday_medians": " · ".join(
                f"{label} {med:g}" for label, med in zip(WEEKDAY_LABELS, weekday_meds)
            ),
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

    dates = date_range(date.today() - timedelta(days=1), ANALYSIS_DAYS)

    all_rows = []
    all_series: dict[str, dict[str, float]] = {}
    for client_id, platforms in client_accounts.items():
        print(f"Analyzing {client_id} (last {ANALYSIS_DAYS} days)...")

        if "GA4" in platforms:
            daily = fetch_daily_event_counts(platforms["GA4"], credentials, days=ANALYSIS_DAYS)
            all_rows.extend(_summarize(daily, client_id, "GA4", dates))
            for event_name, day_map in daily.items():
                all_series[f"{client_id}|GA4|{event_name}"] = day_map

        if "GADS" in platforms:
            gads_client = get_gads_client(
                client_id=os.environ["GOOGLE_CLIENT_ID"],
                client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
                developer_token=os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
                login_customer_id=os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
            )
            daily = fetch_daily_conversion_counts(platforms["GADS"], gads_client, days=ANALYSIS_DAYS)
            all_rows.extend(_summarize(daily, client_id, "GAds", dates))
            for event_name, day_map in daily.items():
                all_series[f"{client_id}|GAds|{event_name}"] = day_map

    write_history_analysis(sheet_id, sheets_client, all_rows)
    write_daily_history(sheet_id, sheets_client, dates, all_series)
    suggested = sum(1 for r in all_rows if r["suggestion_24h"] == "sim")
    print(f"Wrote {len(all_rows)} rows to history_analysis ({suggested} suggested for 24h).")
    print(f"Wrote daily_history: {len(dates)} days x {len(all_series)} series.")


if __name__ == "__main__":
    main()
