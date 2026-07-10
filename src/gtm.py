"""Google Tag Manager API access + deterministic event<->tag matching.

Reads the LIVE (published) container version — that's what actually fires in
production, not the workspace draft. The matching itself is a pure join:
- GA4 event tags (type `gaawe`) carry the event name in their `eventName`
  parameter.
- Google Ads conversion tags (type `awct`) carry `conversionLabel`, which is
  matched against the labels in each conversion action's tag snippets (see
  `fetch_conversion_labels` in src/gads.py).

`build_event_tag_map` and `extract_conversion_label` are pure (no API client)
so they can be unit-tested with synthetic data.
"""
import os
import re

from google.oauth2.credentials import Credentials

TAGMANAGER_SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"

GTM_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/adwords",
    TAGMANAGER_SCOPE,
]

# GTM tag types we know how to match.
TYPE_GA4_EVENT = "gaawe"
TYPE_GADS_CONVERSION = "awct"

NO_TAG = "(sem tag GTM)"


def build_gtm_credentials() -> Credentials:
    """Like main.build_credentials but requesting the Tag Manager scope too.

    Kept separate so the daily check never depends on the extra scope: if the
    stored refresh token was consented without it, only the GTM step fails.
    """
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GTM_SCOPES,
    )


def get_gtm_service(credentials: Credentials):
    # Lazy import: keeps this module importable (and its pure helpers testable)
    # without google-api-python-client installed.
    from googleapiclient.discovery import build

    return build("tagmanager", "v2", credentials=credentials, cache_discovery=False)


def resolve_container(service, public_id: str) -> str | None:
    """Returns the API path of the container whose publicId is e.g. GTM-ABC123,
    searching every account the authorized user can read. None if not found."""
    page_token = None
    while True:
        accounts_resp = service.accounts().list(pageToken=page_token).execute()
        for account in accounts_resp.get("account", []):
            container_token = None
            while True:
                containers_resp = (
                    service.accounts().containers()
                    .list(parent=account["path"], pageToken=container_token)
                    .execute()
                )
                for container in containers_resp.get("container", []):
                    if container.get("publicId") == public_id:
                        return container["path"]
                container_token = containers_resp.get("nextPageToken")
                if not container_token:
                    break
        page_token = accounts_resp.get("nextPageToken")
        if not page_token:
            return None


def fetch_live_tags(service, container_path: str) -> list[dict]:
    """Returns the tags of the container's published (live) version."""
    version = (
        service.accounts().containers().versions()
        .live(parent=container_path)
        .execute()
    )
    return version.get("tag", [])


def _param(tag: dict, key: str) -> str:
    for p in tag.get("parameter", []):
        if p.get("key") == key:
            return p.get("value", "") or ""
    return ""


def build_event_tag_map(tags: list[dict]) -> tuple[dict, list]:
    """Pure join over GTM tags.

    Returns (mapping, dynamic):
    - mapping: {("GA4", event_name) | ("GADS", conversion_label): [tag names]}.
      Paused tags are included but annotated "<name> (pausada)".
    - dynamic: [(tag_name, event_name_expr)] for GA4 tags whose event name uses
      GTM variables ({{...}}) — those can't be matched deterministically.
    """
    mapping: dict[tuple[str, str], list[str]] = {}
    dynamic: list[tuple[str, str]] = []

    for tag in tags:
        name = tag.get("name", "")
        display = f"{name} (pausada)" if tag.get("paused") else name

        tag_type = tag.get("type")
        if tag_type == TYPE_GA4_EVENT:
            event_name = _param(tag, "eventName").strip()
            if not event_name:
                continue
            if "{{" in event_name:
                dynamic.append((name, event_name))
                continue
            mapping.setdefault(("GA4", event_name), []).append(display)
        elif tag_type == TYPE_GADS_CONVERSION:
            label = _param(tag, "conversionLabel").strip()
            if label:
                mapping.setdefault(("GADS", label), []).append(display)

    return mapping, dynamic


def extract_conversion_label(snippet: str) -> str | None:
    """Pulls the conversion label out of a GAds event tag snippet, which
    contains a send_to like 'AW-123456789/AbCdEfGhIj'. None if absent."""
    match = re.search(r"AW-\d+/([\w-]+)", snippet or "")
    return match.group(1) if match else None


def format_tag_names(tag_names: list[str]) -> str:
    """Cell value for the Nome_Tag_GTM column."""
    return " + ".join(tag_names) if tag_names else NO_TAG
