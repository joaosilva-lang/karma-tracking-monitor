import json
import os
from pathlib import Path

from google.oauth2.credentials import Credentials

from src.ga4 import fetch_event_counts
from src.gads import fetch_conversion_counts, get_gads_client
from src.sheets import get_sheets_client, read_config, write_results
from src.slack import send_alert

CLIENTS_DIR = Path(__file__).parent / "clients"

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


def load_clients() -> list[dict]:
    return [
        json.loads(p.read_text())
        for p in CLIENTS_DIR.glob("*.json")
    ]


def run_checks(client_cfg: dict, credentials: Credentials, config_rows: list[dict]) -> list[dict]:
    client_id = client_cfg["id"]

    # --- GA4 ---
    ga4_counts = fetch_event_counts(client_cfg["ga4_property_id"], credentials)

    # --- GAds ---
    gads_client = get_gads_client(
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        developer_token=os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        login_customer_id=client_cfg["gads_login_customer_id"],
    )
    gads_counts = fetch_conversion_counts(client_cfg["gads_customer_id"], gads_client)

    # Build severity map from Sheet config: {(platform, event_name): severity}
    severity_map = {
        (r["platform"].upper(), r["event_name"]): r["severity"]
        for r in config_rows
        if r["client_id"] == client_id
    }

    results = []

    for event_name, count in ga4_counts.items():
        severity = severity_map.get(("GA4", event_name), "secondary")
        results.append({
            "client_id": client_id,
            "platform": "GA4",
            "event_name": event_name,
            "severity": severity,
            "count_7d": count,
            "status": "OK" if count > 0 else "FAIL",
        })

    for conv_name, count in gads_counts.items():
        severity = severity_map.get(("GADS", conv_name), "secondary")
        results.append({
            "client_id": client_id,
            "platform": "GAds",
            "event_name": conv_name,
            "severity": severity,
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
    clients = load_clients()

    all_results = []
    for client_cfg in clients:
        print(f"Checking {client_cfg['name']}...")
        results = run_checks(client_cfg, credentials, config_rows)
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
