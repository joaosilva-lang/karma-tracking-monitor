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
        if client_id not in accounts:
            accounts[client_id] = {}
        accounts[client_id][platform] = account_id
    return accounts


def get_event_config(config_rows: list[dict], client_id: str, platform: str, event_name: str) -> dict:
    """Returns {severity, goback_days} for a given event, with defaults if not configured."""
    for row in config_rows:
        if (row["client_id"] == client_id
                and row["platform"].upper() == platform.upper()
                and row["event_name"] == event_name):
            return {
                "severity": row.get("severity", "secondary"),
                "goback_days": int(row["goback_days"]) if row.get("goback_days") else DEFAULT_GOBACK_DAYS,
            }
    return {"severity": "secondary", "goback_days": DEFAULT_GOBACK_DAYS}


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
        if days not in counts_by_days:
            if platform_upper == "GA4":
                counts_by_days[days] = fetch_event_counts(account_id, credentials, days=days)
            elif platform_upper == "GADS":
                counts_by_days[days] = fetch_conversion_counts(account_id, gads_client, days=days)

    # Build event_config map for quick lookup
    event_days_map = {
        row["event_name"]: int(row["goback_days"]) if row.get("goback_days") else DEFAULT_GOBACK_DAYS
        for row in config_rows
        if row["client_id"] == client_id and row["platform"].upper() == platform_upper
    }

    # Combine: all events from all calls, each mapped to its appropriate days result
    all_events: set[str] = set()
    for counts in counts_by_days.values():
        all_events.update(counts.keys())

    result = {}
    for event_name in all_events:
        days = event_days_map.get(event_name, DEFAULT_GOBACK_DAYS)
        count = counts_by_days.get(days, {}).get(event_name, 0)
        result[event_name] = (count, days)

    return result


def run_checks(client_id: str, platforms: dict[str, str], credentials: Credentials,
               config_rows: list[dict]) -> list[dict]:
    results = []

    if "GA4" in platforms:
        event_results = fetch_counts_by_days(
            platforms["GA4"], "GA4", config_rows, client_id, credentials
        )
        for event_name, (count, days) in event_results.items():
            cfg = get_event_config(config_rows, client_id, "GA4", event_name)
            results.append({
                "client_id": client_id,
                "platform": "GA4",
                "event_name": event_name,
                "severity": cfg["severity"],
                "goback_days": days,
                "count_7d": count,
                "status": "OK" if count > 0 else "FAIL",
            })

    if "GADS" in platforms:
        gads_client = get_gads_client(
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            developer_token=os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
            login_customer_id=os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"],
        )
        event_results = fetch_counts_by_days(
            platforms["GADS"], "GADS", config_rows, client_id, credentials,
            gads_client=gads_client
        )
        for conv_name, (count, days) in event_results.items():
            cfg = get_event_config(config_rows, client_id, "GAds", conv_name)
            results.append({
                "client_id": client_id,
                "platform": "GAds",
                "event_name": conv_name,
                "severity": cfg["severity"],
                "goback_days": days,
                "count_7d": count,
                "status": "OK" if count > 0 else "FAIL",
            })

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
        results = run_checks(client_id, platforms, credentials, config_rows)
        all_results.extend(results)

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
