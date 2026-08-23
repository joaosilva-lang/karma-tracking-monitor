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


def fetch_live_container(service, container_path: str) -> tuple[list[dict], list[dict]]:
    """Returns (tags, variables) of the container's published (live) version.

    Variables are needed to resolve shared "Google Tag: Event Settings"
    (`gtes`) variables that GA4 tags may reference instead of inline params.
    """
    version = (
        service.accounts().containers().versions()
        .live(parent=container_path)
        .execute()
    )
    return version.get("tag", []), version.get("variable", [])


def _param(tag: dict, key: str) -> str:
    for p in tag.get("parameter", []):
        if p.get("key") == key:
            return p.get("value", "") or ""
    return ""


def build_event_tag_map(tags: list[dict]) -> tuple[dict, list]:
    """Pure join over GTM tags.

    Returns (mapping, dynamic):
    - mapping: {("GA4", event_name) | ("GADS", conversion_label): [tag names]},
      in container order. Paused tags are EXCLUDED — they don't fire, so they
      don't belong in the config's tag columns (the triage agent still surfaces
      paused tags via its separate paused_tags list).
    - dynamic: [(tag_name, event_name_expr)] for GA4 tags whose event name uses
      GTM variables ({{...}}) — those can't be matched deterministically.
    """
    mapping: dict[tuple[str, str], list[str]] = {}
    dynamic: list[tuple[str, str]] = []

    for tag in tags:
        if tag.get("paused"):
            continue
        name = tag.get("name", "")

        tag_type = tag.get("type")
        if tag_type == TYPE_GA4_EVENT:
            event_name = _param(tag, "eventName").strip()
            if not event_name:
                continue
            if "{{" in event_name:
                dynamic.append((name, event_name))
                continue
            mapping.setdefault(("GA4", event_name), []).append(name)
        elif tag_type == TYPE_GADS_CONVERSION:
            label = _param(tag, "conversionLabel").strip()
            if label:
                mapping.setdefault(("GADS", label), []).append(name)

    return mapping, dynamic


# The GA4 event-parameter table appears under either of these keys depending
# on container age; map entries use "parameter"/"parameterValue" (current) or
# "name"/"value" (older exports). Handle all combinations defensively.
_EVENT_PARAM_LIST_KEYS = ("eventSettingsTable", "eventParameters")

# GTM variable type of the shared "Google Tag: Event Settings" variable.
TYPE_EVENT_SETTINGS_VAR = "gtes"

NO_PARAMS = "(sem params)"


def _map_entry(entry: dict) -> tuple[str, str] | None:
    kv = {p.get("key"): p.get("value", "") or "" for p in entry.get("map", [])}
    name = kv.get("parameter") or kv.get("name") or ""
    value = kv.get("parameterValue") or kv.get("value") or ""
    return (name, value) if name else None


def _extract_event_params(entity: dict) -> list[tuple[str, str]]:
    """Event parameter (name, value_expr) pairs of a gaawe tag or gtes variable."""
    pairs = []
    for p in entity.get("parameter", []):
        if p.get("key") in _EVENT_PARAM_LIST_KEYS and p.get("type") == "list":
            for entry in p.get("list", []):
                pair = _map_entry(entry)
                if pair:
                    pairs.append(pair)
    return pairs


def _iter_tag_params(tags: list[dict], variables: list[dict]):
    """Yields (key, tag_name, pairs) for each active tag that carries event
    parameters — key is ("GA4", event_name) or ("GADS", conversion_label),
    pairs is a list of (name, value_expr). Shared by build_event_param_map
    (keeps the per-tag display string) and build_event_param_names (keeps only
    the names, unioned per event) so the two never drift apart.
    """
    settings_vars = {
        v.get("name"): _extract_event_params(v)
        for v in variables
        if v.get("type") == TYPE_EVENT_SETTINGS_VAR
    }

    for tag in tags:
        if tag.get("paused"):
            continue
        tag_type = tag.get("type")
        if tag_type == TYPE_GA4_EVENT:
            event_name = _param(tag, "eventName").strip()
            if not event_name or "{{" in event_name:
                continue
            pairs = _extract_event_params(tag)
            settings_ref = _param(tag, "eventSettingsVariable").strip()
            if settings_ref.startswith("{{") and settings_ref.endswith("}}"):
                for pair in settings_vars.get(settings_ref[2:-2].strip(), []):
                    if pair not in pairs:
                        pairs.append(pair)
            key = ("GA4", event_name)
        elif tag_type == TYPE_GADS_CONVERSION:
            label = _param(tag, "conversionLabel").strip()
            if not label:
                continue
            pairs = [
                (name, value)
                for name in ("conversionValue", "currencyCode")
                if (value := _param(tag, name).strip())
            ]
            key = ("GADS", label)
        else:
            continue

        yield key, tag.get("name", ""), pairs


def build_event_param_map(tags: list[dict], variables: list[dict]) -> dict[tuple[tuple[str, str], str], str]:
    """Pure join: which parameters is each tag configured to send?

    Returns {(("GA4", event_name) | ("GADS", conversion_label), tag_name):
    "a={{X}}, b=EUR"} — keyed PER TAG (each config row carries exactly one
    tag, so each row shows its own tag's params). Paused tags are excluded,
    same as build_event_tag_map. GA4 tags referencing a shared Event Settings
    variable get its params merged with the inline ones. Tags sending no
    params map to NO_PARAMS.
    """
    return {
        (key, tag_name): (
            ", ".join(f"{name}={value}" for name, value in pairs) if pairs else NO_PARAMS
        )
        for key, tag_name, pairs in _iter_tag_params(tags, variables)
    }


def build_event_param_names(tags: list[dict], variables: list[dict]) -> dict[tuple[str, str], list[str]]:
    """Pure join: which parameter NAMES does an event carry, across all its
    active tags?

    Returns {("GA4", event_name) | ("GADS", conversion_label): [param_name,
    ...]} — union of names across every active tag for that event, in
    first-seen order. This is what the params-check queries against the GA4
    Data API: it needs structured names, not build_event_param_map's display
    string ("a={{X}}, b=EUR"), which is ambiguous once a value itself contains
    a comma.
    """
    names: dict[tuple[str, str], list[str]] = {}
    for key, _tag_name, pairs in _iter_tag_params(tags, variables):
        bucket = names.setdefault(key, [])
        for name, _value in pairs:
            if name not in bucket:
                bucket.append(name)
    return names


def extract_conversion_label(snippet: str) -> str | None:
    """Pulls the conversion label out of a GAds event tag snippet, which
    contains a send_to like 'AW-123456789/AbCdEfGhIj'. None if absent."""
    match = re.search(r"AW-\d+/([\w-]+)", snippet or "")
    return match.group(1) if match else None


def format_tag_names(tag_names: list[str]) -> str:
    """Cell value for the Nome_Tag_GTM column."""
    return " + ".join(tag_names) if tag_names else NO_TAG
