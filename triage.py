"""
Triage agent: turns critical alerts into a diagnosis, posted to Slack.

Runs right after main.py in the daily workflow. main.py writes
triage_input.json whenever critical FAIL/WARN issues exist; this script then:
1. Enriches each issue with GTM context — does the tag still exist in the live
   container? Is it paused? Which container versions exist (was one published
   around the time the event died)?
2. Asks Gemini for a short probable-cause diagnosis per issue (in Portuguese).
3. Posts a follow-up "🤖 Diagnóstico" message to the same Slack webhook.

GATED AND FAIL-SAFE by design:
- No GEMINI_API_KEY secret -> silent no-op (like the Slack webhook pattern).
- No triage_input.json (no critical issues) -> no-op.
- Any exception anywhere -> printed and swallowed; exit code stays 0. The
  deterministic Slack alert was already sent by main.py — this script can only
  add information, never block or replace it.
"""
import json
import os

# Keep in sync with main.py (main writes it, this script reads it). Defined
# here too so the gating path has zero heavy imports — the no-op exit must
# never fail, whatever the environment looks like.
TRIAGE_INPUT_FILE = "triage_input.json"

GEMINI_MODEL_DEFAULT = "gemini-2.5-flash"

PROMPT_TEMPLATE = """És um especialista em web analytics (GA4, Google Ads, GTM) da agência Karma.
O monitor diário de tracking detetou falhas críticas. Abaixo tens, em JSON:
- "issues": cada falha (cliente, plataforma, evento, tipo de check "count" ou "value",
  janela, contagem observada, mediana esperada, histórico diário recente).
- "gtm_context": para cada cliente, o estado das tags no container GTM publicado
  (a tag do evento existe? está pausada?) e as versões do container.

Para CADA issue, escreve um diagnóstico em português de Portugal com no máximo
3 frases: causa mais provável + onde verificar primeiro. Sê concreto (nomes de
tags, datas, versões) e diz explicitamente quando não há evidência suficiente.
Formato: uma linha "• <evento> (<cliente>):" seguida do diagnóstico. Sem
introdução nem conclusão — só os diagnósticos.

JSON:
{payload}
"""


def build_gtm_context(data: dict) -> dict:
    """Best-effort GTM enrichment; any failure returns partial context."""
    containers = data.get("gtm_containers", {})
    affected_clients = {issue["client_id"] for issue in data.get("issues", [])}
    context: dict[str, dict] = {}
    if not containers:
        return context

    try:
        from src.gtm import (
            build_event_tag_map,
            build_gtm_credentials,
            fetch_live_container,
            get_gtm_service,
            resolve_container,
        )
        service = get_gtm_service(build_gtm_credentials())
    except Exception as exc:
        print(f"Triage: GTM context unavailable ({exc}) — diagnosing without it.")
        return context

    for client_id in affected_clients:
        public_id = containers.get(client_id)
        if not public_id:
            continue
        try:
            container_path = resolve_container(service, public_id)
            if not container_path:
                continue
            tags, _variables = fetch_live_container(service, container_path)
            tag_map, dynamic = build_event_tag_map(tags)
            versions = (
                service.accounts().containers().version_headers()
                .list(parent=container_path)
                .execute()
                .get("containerVersionHeader", [])
            )
            context[client_id] = {
                "live_tags_by_event": {
                    f"{platform}:{name}": tag_names
                    for (platform, name), tag_names in tag_map.items()
                },
                "paused_tags": [t.get("name") for t in tags if t.get("paused")],
                "dynamic_event_tags": [name for name, _expr in dynamic],
                "container_versions": [
                    {"id": v.get("containerVersionId"), "name": v.get("name", "")}
                    for v in versions[-5:]
                ],
            }
        except Exception as exc:
            print(f"Triage: GTM context failed for '{client_id}': {exc}")
    return context


def call_gemini(api_key: str, prompt: str) -> str:
    from google import genai

    client = genai.Client(api_key=api_key)
    model = os.environ.get("GEMINI_MODEL", GEMINI_MODEL_DEFAULT)
    response = client.models.generate_content(model=model, contents=prompt)
    return (response.text or "").strip()


def run_triage(api_key: str, slack_webhook: str) -> None:
    import requests

    with open(TRIAGE_INPUT_FILE) as fh:
        data = json.load(fh)

    issues = data.get("issues", [])
    if not issues:
        print("Triage: no issues in input — nothing to do.")
        return

    payload = json.dumps(
        {"issues": issues, "gtm_context": build_gtm_context(data)},
        ensure_ascii=False,
    )
    diagnosis = call_gemini(api_key, PROMPT_TEMPLATE.format(payload=payload))
    if not diagnosis:
        print("Triage: empty diagnosis from Gemini — nothing posted.")
        return

    message = f"🤖 *Diagnóstico automático* ({len(issues)} issue(s))\n{diagnosis}"
    response = requests.post(slack_webhook, json={"text": message}, timeout=10)
    response.raise_for_status()
    print("Triage: diagnosis posted to Slack.")


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")

    if not api_key:
        print("Triage: GEMINI_API_KEY not configured — skipping (add the secret to enable).")
        return
    if not os.path.exists(TRIAGE_INPUT_FILE):
        print("Triage: no critical issues this run — skipping.")
        return
    if not slack_webhook:
        print("Triage: SLACK_WEBHOOK_URL not configured — nowhere to post, skipping.")
        return

    try:
        run_triage(api_key, slack_webhook)
    except Exception as exc:
        # Never fail the job: the deterministic alert already went out.
        print(f"Triage failed (original alert unaffected): {exc}")


if __name__ == "__main__":
    main()
