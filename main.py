import os

from google.oauth2.credentials import Credentials

from src.ga4 import fetch_event_counts
from src.gads import fetch_conversion_counts, get_gads_client
from src.sheets import get_sheets_client, read_config, write_results
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


def get_severity(config_rows: list[dict], client_id: str, platform: str, event_name: str) -> str:
    for row in config_rows:
        if (row["client_id"] == client_id
                and row["platform"].upper() == platform.upper()
                and row["event_name"] == event_name):
            return row["severity"]
    return "secondary"


def run_checks(client_id: str, platforms: dict[str, str], credentials: Credentials,
               config_rows: list[dict]) -> list[dict]:
    results = []

    if "GA4" in platforms:
        ga4_counts = fetch_event_counts(platforms["GA4"], credentials)
        for event_name, count in ga4_counts.items():
            results.append({
                "client_id": client_id,
                "platform": "GA4",
                "event_name": event_name,
                "severity": get_severity(config_rows, client_id, "GA4", event_name),
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
        gads_counts = fetch_conversion_counts(platforms["GADS"], gads_client)
        for conv_name, count in gads_counts.items():
            results.append({
                "client_id": client_id,
                "platform": "GAds",
                "event_name": conv_name,
                "severity": get_severity(config_rows, client_id, "GAds", conv_name),
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
