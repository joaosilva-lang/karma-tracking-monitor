import requests

# A failure in a short 24h/48h window means "broke yesterday" (urgent); a
# failure in the wide goback window means "hasn't fired in a while".
SHORT_WINDOWS = {"24h", "48h"}


def _window_phrase(window: str) -> str:
    if window in SHORT_WINDOWS:
        return f"nas últimas {window}"
    return f"nos últimos {window.rstrip('d')} dias"


def send_alert(webhook_url: str, failures: list[dict]) -> None:
    """Posts a Slack message listing all critical failures, labelled by window."""
    if not failures:
        return

    lines = [f"*Tracking Monitor — {len(failures)} critical issue(s) detected*\n"]
    for f in failures:
        emoji = "🔴" if f["window"] in SHORT_WINDOWS else "🟠"
        lines.append(
            f"{emoji} `{f['client_id']}` | {f['platform']} | `{f['event_name']}` — "
            f"*{f['count']} eventos* {_window_phrase(f['window'])}"
        )

    payload = {"text": "\n".join(lines)}
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
