import os

from google.oauth2.credentials import Credentials

from src.ga4 import fetch_event_counts
from src.gads import fetch_conversion_counts, get_gads_client
from src.sheets import get_sheets_client, read_config, write_results, DEFAULT_GOBACK_DAYS
from src.slack import send_alert

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/adwords",
]

# 24h-style check window per platform. GA4 data for "yesterday" is stable, so a
# strict 1-day window is fine. Google Ads conversions lag (attribution can take
# 24-72h), so we widen to 2 days (~48h) to avoid false positives.
WINDOW_24H_DAYS = {"GA4": 1, "GADS": 2}
WINDOW_24H_LABEL = {"GA4": "24h", "GADS": "48h"}

TRUTHY = {"sim", "yes", "true", "1", "y", "s"}


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
    """Returns {client_id: {platform: account_id}} derived from Sheet config rows."""
    accounts: dict[str, dict[str, str]] = {}
    for row in config_rows:
        client_id = row["client_id"]
        platform = row["platform"].upper()
        account_id = str(row["account_id"])
        accounts.setdefault(client_id, {})[platform] = account_id
    return accounts


def get_event_config(config_rows: list[dict], client_id: str, platform: str, event_name: str) -> dict:
    """Returns {severity, goback_days} for a given event, with defaults if not configured."""
    for row in config_rows:
        if (row["client_id"] == client_id
                and row["platform"].upper() == platform.upper()
                and row["event_name"] == event_name):
            return {
                "severity": row.get("severity", "secondary") or "secondary",
                "goback_days": int(row["goback_days"]) if row.get("goback_days") else DEFAULT_GOBACK_DAYS,
            }
    return {"severity": "secondary", "goback_days": DEFAULT_GOBACK_DAYS}


def get_24h_events(config_rows: list[dict], client_id: str, platform: str) -> set:
    """Returns the set of event names flagged 24hours_lookback='sim' for a platform."""
    flagged = set()
    for row in config_rows:
        if (row["client_id"] == client_id and row["platform"].upper() == platform.upper()
                and str(row.get("24hours_lookback", "")).strip().lower() in TRUTHY):
            flagged.add(row["event_name"])
    return flagged


def fetch_counts_by_days(account_id: str, platform: str, config_rows: list[dict],
                         client_id: str, credentials: Credentials,
                         gads_client=None) -> dict[str, tuple[int, int]]:
    """
    Returns {event_name: (count, goback_days)} using per-event time windows.
    Groups events by goback_days to minimize API calls.
    """
    platform_upper = platform.upper()

    # Build map: goback_days -> set of configured event names for this platform
    days_to_events: dict[int, set] = {}
    for row in config_rows:
        if row["client_id"] == client_id and row["platform"].upper() == platform_upper:
            days = int(row["goback_days"]) if row.get("goback_days") else DEFAULT_GOBACK_DAYS
            days_to_events.setdefault(days, set()).add(row["event_name"])

    # Always include a default call to catch dynamically discovered events
    days_to_events.setdefault(DEFAULT_GOBACK_DAYS, set())

    # One API call per unique goback_days value
    counts_by_days: dict[int, dict[str, int]] = {}
    for days in days_to_events:
        if platform_upper == "GA4":
            counts_by_days[days] = fetch_event_counts(account_id, credentials, days=days)
        elif platform_upper == "GADS":
            counts_by_days[days] = fetch_conversion_counts(account_id, gads_client, days=days)

    event_days_map = {
        row["event_name"]: int(row["goback_days"]) if row.get("goback_days") else DEFAULT_GOBACK_DAYS
        for row in config_rows
        if row["client_id"] == client_id and row["platform"].upper() == platform_upper
    }

    all_events: set[str] = set()
    for counts in counts_by_days.values():
        all_events.update(counts.keys())

    result = {}
    for event_name in all_events:
        days = event_days_map.get(event_name, DEFAULT_GOBACK_DAYS)
        count = counts_by_days.get(days, {}).get(event_name, 0)
        result[event_name] = (count, days)

    return result


def _fetch_24h_counts(account_id: str, platform: str, credentials: Credentials,
                      gads_client=None) -> dict[str, int]:
    days = WINDOW_24H_DAYS[platform]
    if platform == "GA4":
        return fetch_event_counts(account_id, credentials, days=days)
    return fetch_conversion_counts(account_id, gads_client, days=days)


def _check_platform(client_id: str, platform: str, display_name: str, account_id: str,
                    credentials: Credentials, config_rows: list[dict],
                    gads_client=None) -> list[dict]:
    """Runs goback + (optional) 24h checks for one platform, returning result rows."""
    results = []

    # --- Wide goback-window check (one row per event) ---
    event_results = fetch_counts_by_days(
        account_id, platform, config_rows, client_id, credentials, gads_client=gads_client
    )
    for event_name, (count, days) in event_results.items():
        cfg = get_event_config(config_rows, client_id, platform, event_name)
        results.append({
            "client_id": client_id,
            "platform": display_name,
            "event_name": event_name,
            "severity": cfg["severity"],
            "window": f"{days}d",
            "count": count,
            "status": "OK" if count > 0 else "FAIL",
        })

    # --- Short 24h-style check (only for flagged events) ---
    flagged = get_24h_events(config_rows, client_id, platform)
    if flagged:
        counts_24h = _fetch_24h_counts(account_id, platform, credentials, gads_client=gads_client)
        label = WINDOW_24H_LABEL[platform]
        for event_name in flagged:
            count = counts_24h.get(event_name, 0)
            cfg = get_event_config(config_rows, client_id, platform, event_name)
            results.append({
                "client_id": client_id,
                "platform": display_name,
                "event_name": event_name,
                "severity": cfg["severity"],
                "window": label,
                "count": count,
                "status": "OK" if count > 0 else "FAIL",
            })

    return results


def run_checks(client_id: str, platforms: dict[str, str], credentials: Credentials,
               config_rows: list[dict]) -> list[dict]:
    results = []

    if "GA4" in platforms:
        results.extend(_check_platform(
            client_id, "GA4", "GA4", platforms["GA4"], credentials, config_rows
        ))

    if "GADS" in platforms:
        gads_client = get_gads_client(
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            developer_token=os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            login_customer_id=os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        )
        results.extend(_check_platform(
            client_id, "GADS", "GAds", platforms["GADS"], credentials, config_rows,
            gads_client=gads_client
        ))

    return results


def main() -> None:
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")

    credentials = build_credentials()
    sheets_client = get_sheets_client(credentials)
    config_rows = read_config(sheet_id, sheets_client)
    client_accounts = build_client_accounts(config_rows)

    all_results = []
    for client_id, platforms in client_accounts.items():
        print(f"Checking {client_id}...")
        all_results.extend(run_checks(client_id, platforms, credentials, config_rows))

    write_results(sheet_id, sheets_client, all_results)

    critical_failures = [
        r for r in all_results
        if r["status"] == "FAIL" and r["severity"] == "critical"
    ]

    if critical_failures and slack_webhook:
        send_alert(slack_webhook, critical_failures)
        print(f"Slack alert sent: {len(critical_failures)} critical failure(s).")
    elif critical_failures:
        print(f"WARNING: {len(critical_failures)} critical failure(s) found but no Slack webhook configured.")
    else:
        print("All checks passed.")


if __name__ == "__main__":
    main()
