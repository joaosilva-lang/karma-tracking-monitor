import requests

# A failure in a short 24h/48h window means "broke yesterday" (urgent); a
# failure in the wide goback window means "hasn't fired in a while". WARN
# means the event still fires but well below its weekday-median baseline.
SHORT_WINDOWS = {"24h", "48h"}


def _window_phrase(window: str) -> str:
    if window in SHORT_WINDOWS:
        return f"nas últimas {window}"
    return f"nos últimos {window.rstrip('d')} dias"


def _format_line(f: dict) -> str:
    prefix = f"`{f['client_id']}` | {f['platform']} | `{f['event_name']}`"
    if f.get("check") == "value":
        return (
            f"💰 {prefix} — eventos registados *SEM valor* "
            f"{_window_phrase(f['window'])} (contagem OK, valor a zero)"
        )
    if f["status"] == "WARN" and f.get("expected"):
        expected = f["expected"]
        pct_below = round((1 - f["count"] / expected) * 100)
        return (
            f"🟡 {prefix} — *{f['count']} eventos* {_window_phrase(f['window'])} "
            f"vs. mediana {expected:g} ({pct_below}% abaixo do normal)"
        )
    emoji = "🔴" if f["window"] in SHORT_WINDOWS else "🟠"
    return (
        f"{emoji} {prefix} — *{f['count']} eventos* {_window_phrase(f['window'])}"
    )


def send_alert(webhook_url: str, issues: list[dict]) -> None:
    """Posts a Slack message listing all critical FAIL/WARN rows, labelled by window."""
    if not issues:
        return

    lines = [f"*Tracking Monitor — {len(issues)} critical issue(s) detected*\n"]
    lines.extend(_format_line(f) for f in issues)

    payload = {"text": "\n".join(lines)}
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
