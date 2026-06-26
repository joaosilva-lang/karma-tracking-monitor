import requests


def send_alert(webhook_url: str, failures: list[dict]) -> None:
    """Posts a Slack message listing all critical failures."""
    if not failures:
        return

    lines = [f"*Tracking Monitor — {len(failures)} critical issue(s) detected*\n"]
    for f in failures:
        lines.append(
            f"• `{f['client_id']}` | {f['platform']} | `{f['event_name']}` — "
            f"*{f['count_7d']} events* in last 7 days"
        )

    payload = {"text": "\n".join(lines)}
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
