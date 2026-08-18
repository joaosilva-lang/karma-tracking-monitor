from datetime import datetime
import gspread
from google.oauth2.credentials import Credentials

from src.gtm import NO_PARAMS, NO_TAG

README_TAB = "_leia-me"
CONFIG_TAB = "config"
RESULTS_TAB = "results"
HISTORY_TAB = "history_analysis"
DAILY_TAB = "daily_history"
PROPOSAL_TAB = "config_proposta"
PARAMS_TAB = "params_analysis"

# Optional columns: baseline_threshold_pct is the WARN threshold for the
# relative check as a % of the weekday median (empty -> 50%);
# gtm_container_id (GTM-XXXXXX, one non-empty value per client is enough),
# Nome_Tag_GTM and GTM_Event_Params (both filled by the analyze_history GTM
# step, never by hand) drive the informational event->GTM mapping.
# params_check overrides which GA4 event parameters are verified daily/weekly
# (comma-separated names); empty means "use the names GTM configures".
CONFIG_HEADERS = [
    "client_id", "account_id", "platform", "event_name",
    "severity", "goback_days", "24hBackGA4_48hBackGAds", "baseline_threshold_pct",
    "gtm_container_id", "Nome_Tag_GTM", "GTM_Event_Params", "params_check",
]
GTM_TAG_COLUMN = "Nome_Tag_GTM"
GTM_PARAMS_COLUMN = "GTM_Event_Params"
# Written into surplus duplicate config rows when an event has fewer active
# tags than rows (e.g. a tag was paused after the rows were created). Rows are
# never deleted — the marker tells the human which duplicates to clean up.
NO_ACTIVE_TAG = "(sem tag ativa correspondente)"

# Onboarding proposal: the config columns first (in the LIVE config tab's own
# order, resolved at write time — see write_config_proposal) so reviewed rows
# copy-paste straight into config, followed by these informational stats.
STAT_HEADERS = [
    "median_per_day", "pct_days", "pct_days_with_value",
    "value_carrying", "max_gap_days", "goback_days_sugerido",
    "weekday_medians",
]
# Fallback header order used only when the config tab doesn't exist yet.
PROPOSAL_HEADERS = CONFIG_HEADERS + STAT_HEADERS
DEFAULT_GOBACK_DAYS = 7

# `window` distinguishes the short 24h-style check ("24h"/"48h") from the
# wider goback window ("10d", etc). `check` is "count" (event/conversion
# counting), "value" (monetary value carried by the event — for value rows
# the `count` column holds the value sum) or "param" (GA4 event-parameter
# presence — `param` names which one, `count` is fires that carried it,
# `expected` is total fires of the event in the window). `expected` is the
# weekday-median baseline the count was compared against for count rows
# (empty for rows without a relative check).
RESULTS_HEADERS = [
    "checked_at", "client_id", "platform", "event_name", "check", "param",
    "severity", "window", "count", "expected", "status",
]

HISTORY_HEADERS = [
    "analyzed_at", "client_id", "platform", "event_name",
    "days_fired_of_90", "pct_days", "avg_per_day",
    "median_per_day", "weekday_medians",
    "pct_days_with_value", "value_carrying",
    "max_gap_days", "goback_days_sugerido", "suggestion_24h",
]

# One row per (client, platform, event, param) verified against GA4. `estado`
# is "verificado" (custom dimension) / "built-in" / "não registado no GA4 —
# criar custom dimension" / "não verificável (Google Ads não expõe parâmetros
# de conversão)". `vigiado_no_diario` = "sim" when the daily check alerts on
# this param (critical event + historically consistent presence).
PARAMS_HEADERS = [
    "analyzed_at", "client_id", "platform", "event_name", "param", "tag_gtm",
    "estado", "pct_disparos_com_param", "dias_com_param_de_90", "vigiado_no_diario",
]


def get_sheets_client(credentials: Credentials) -> gspread.Client:
    return gspread.authorize(credentials)


def read_config(sheet_id: str, client: gspread.Client) -> list[dict]:
    """Returns config rows from the config tab."""
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(CONFIG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=CONFIG_TAB, rows=100, cols=10)
        ws.append_row(CONFIG_HEADERS)
        return []

    # Only require the headers that actually exist in the Sheet, so adding an
    # optional column here doesn't break runs before the Sheet catches up.
    present = ws.row_values(1)
    expected = [h for h in CONFIG_HEADERS if h in present]
    records = ws.get_all_records(expected_headers=expected)
    return [r for r in records if r.get("client_id") and r.get("event_name")]


def plan_config_gtm_updates(header: list[str], data_rows: list[list[str]],
                            values_by_key: dict[tuple[str, str, str], list[dict[str, str]]],
                            ) -> tuple[list[tuple[int, int, str]], list[tuple[int, list[str]]]]:
    """Pure planner for update_config_columns (testable without gspread).

    values_by_key: {(client_id, PLATFORM_UPPER, event_name): [{column: value},
    ...]} — ONE dict per desired row for that event (one tag per row). Rows of
    the same key are matched in sheet order: entry i -> matching row i.
    - More entries than rows: extra entries become new rows inserted below the
      key's last row (a copy of it with the named columns replaced) — the one
      case where the script creates config rows.
    - Fewer entries than rows: surplus rows get NO_ACTIVE_TAG in the named
      columns (never deleted).
    - Keys with no matching row are skipped: config decides what is monitored.

    Returns (cell_updates, row_insertions):
    - cell_updates: [(row_number, col_number, value)] — 1-indexed, coordinates
      valid BEFORE any insertion (apply these first).
    - row_insertions: [(insert_at_row_number, [row_values, ...])] — apply
      bottom-up (descending anchor) so earlier anchors don't shift.
    """
    def col_index(name: str) -> int | None:
        return header.index(name) if name in header else None

    ci, pi, ei = col_index("client_id"), col_index("platform"), col_index("event_name")
    if None in (ci, pi, ei):
        return [], []

    rows_by_key: dict[tuple[str, str, str], list[int]] = {}
    for row_number, row in enumerate(data_rows, start=2):
        def cell(idx):
            return row[idx].strip() if idx < len(row) else ""
        rows_by_key.setdefault((cell(ci), cell(pi).upper(), cell(ei)), []).append(row_number)

    cell_updates: list[tuple[int, int, str]] = []
    row_insertions: list[tuple[int, list[str]]] = []

    for key, entries in values_by_key.items():
        row_numbers = rows_by_key.get(key, [])
        if not row_numbers or not entries:
            continue
        writable_columns = {c for entry in entries for c in entry if col_index(c) is not None}

        # entry i -> row i
        for row_number, entry in zip(row_numbers, entries):
            for column in writable_columns:
                cell_updates.append(
                    (row_number, col_index(column) + 1, entry.get(column, "")))

        # more tags than rows -> insert copies of the last row below it
        if len(entries) > len(row_numbers):
            anchor = row_numbers[-1]
            template = list(data_rows[anchor - 2])
            template += [""] * (len(header) - len(template))
            block = []
            for entry in entries[len(row_numbers):]:
                new_row = list(template)
                for column in writable_columns:
                    new_row[col_index(column)] = entry.get(column, "")
                block.append(new_row)
            row_insertions.append((anchor + 1, block))

        # more rows than tags -> mark the surplus, never delete
        for row_number in row_numbers[len(entries):]:
            for column in writable_columns:
                cell_updates.append((row_number, col_index(column) + 1, NO_ACTIVE_TAG))

    return cell_updates, row_insertions


def update_config_columns(sheet_id: str, client: gspread.Client,
                          values_by_key: dict[tuple[str, str, str], list[dict[str, str]]]) -> int:
    """Fills script-owned columns of the config tab, one tag per row.

    See plan_config_gtm_updates for the matching semantics. Only the named
    columns are touched (and only those present in the Sheet header); the only
    structural change ever made is inserting duplicate rows when an event has
    more active GTM tags than config rows. Returns cells updated + rows added.
    """
    sh = client.open_by_key(sheet_id)
    ws = sh.worksheet(CONFIG_TAB)

    all_values = ws.get_all_values()
    if not all_values:
        return 0
    header, data_rows = all_values[0], all_values[1:]

    cell_updates, row_insertions = plan_config_gtm_updates(header, data_rows, values_by_key)

    if cell_updates:
        ws.update_cells(
            [gspread.Cell(r, c, v) for r, c, v in cell_updates],
            value_input_option="RAW",
        )
    # Bottom-up so lower anchors aren't shifted by earlier insertions.
    inserted = 0
    for anchor, block in sorted(row_insertions, reverse=True):
        ws.insert_rows(block, row=anchor, value_input_option="RAW")
        inserted += len(block)

    return len(cell_updates) + inserted


def _replace_tab_contents(sh: gspread.Spreadsheet, title: str, table: list[list],
                          min_rows: int = 100, spare_cols: int = 5) -> gspread.Worksheet:
    """Replaces a tab's whole contents IN PLACE. Never deletes the worksheet.

    Deleting a worksheet destroys everything anchored to it: cross-tab formulas
    break (they point at a sheet that no longer exists), and charts, conditional
    formatting and column widths go with it. Recreating a tab with the same name
    does NOT bring them back — it's a new sheet with a new id.

    That matters here because the config tab carries hand-written VLOOKUPs into
    history_analysis, so a weekly delete+recreate silently broke them until the
    cell was re-entered by hand. Every writer in this module therefore grows the
    grid if needed, clears, and rewrites — the tab itself survives.
    """
    needed_cols = max((len(row) for row in table), default=1)
    try:
        ws = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=title,
            rows=max(min_rows, len(table) + 10),
            cols=needed_cols + spare_cols,
        )

    if ws.col_count < needed_cols:
        ws.add_cols(needed_cols - ws.col_count)
    if ws.row_count < len(table):
        ws.add_rows(len(table) - ws.row_count)

    # clear() empties the whole grid, so rows left over from a longer previous
    # run don't survive as trailing garbage.
    ws.clear()
    if table:
        ws.update(table, value_input_option="RAW")
    return ws


def write_results(sheet_id: str, client: gspread.Client, rows: list[dict]) -> None:
    """Rewrites the results tab (clean slate each run) with all result rows."""
    sh = client.open_by_key(sheet_id)

    checked_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    table = [RESULTS_HEADERS]
    for row in rows:
        table.append([
            checked_at,
            row["client_id"],
            row["platform"],
            row["event_name"],
            row.get("check", "count"),
            row.get("param", ""),
            row["severity"],
            row["window"],
            row["count"],
            row.get("expected", ""),
            row["status"],
        ])
    _replace_tab_contents(sh, RESULTS_TAB, table, min_rows=1000)


def write_history_analysis(sheet_id: str, client: gspread.Client, rows: list[dict]) -> None:
    """Rewrites the history_analysis tab with 90-day frequency suggestions.

    Updated in place: the config tab has VLOOKUPs pointing here.
    """
    sh = client.open_by_key(sheet_id)

    analyzed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    table = [HISTORY_HEADERS]
    for row in rows:
        table.append([
            analyzed_at,
            row["client_id"],
            row["platform"],
            row["event_name"],
            row["days_fired_of_90"],
            row["pct_days"],
            row["avg_per_day"],
            row["median_per_day"],
            row["weekday_medians"],
            row["pct_days_with_value"],
            row["value_carrying"],
            row["max_gap_days"],
            row["goback_days_sugerido"],
            row["suggestion_24h"],
        ])
    _replace_tab_contents(sh, HISTORY_TAB, table, min_rows=500)


def write_config_proposal(sheet_id: str, client: gspread.Client, rows: list[dict]) -> None:
    """Rewrites the config_proposta tab with the onboarding proposal.

    The config columns are laid out in the LIVE config tab's own header order
    (read here at write time), so reviewed rows copy-paste straight into config
    without column misalignment — whatever order the user keeps config in. The
    informational stats (STAT_HEADERS) always follow to the right; pasting a
    whole row is safe because those extra columns land under no config header
    and read_config ignores them. Never touches the real config tab.
    """
    sh = client.open_by_key(sheet_id)

    # Mirror config's current column order; fall back to the canonical order
    # only if config doesn't exist yet or is empty.
    try:
        config_header = sh.worksheet(CONFIG_TAB).row_values(1)
    except gspread.exceptions.WorksheetNotFound:
        config_header = []
    if not config_header:
        config_header = list(CONFIG_HEADERS)

    headers = config_header + STAT_HEADERS

    table = [headers]
    for row in rows:
        table.append([row.get(header, "") for header in headers])
    _replace_tab_contents(sh, PROPOSAL_TAB, table, min_rows=200)


def write_params_analysis(sheet_id: str, client: gspread.Client, rows: list[dict]) -> None:
    """Rewrites the params_analysis tab: one row per (client, platform, event,
    param) checked against GA4 (or reported non-verifiable for Google Ads).
    Updated in place, like every other tab — see _replace_tab_contents.
    """
    sh = client.open_by_key(sheet_id)

    analyzed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    table = [PARAMS_HEADERS]
    for row in rows:
        table.append([
            analyzed_at,
            row["client_id"],
            row["platform"],
            row["event_name"],
            row["param"],
            row.get("tag_gtm", ""),
            row["estado"],
            row.get("pct_disparos_com_param", ""),
            row.get("dias_com_param_de_90", ""),
            row.get("vigiado_no_diario", "não"),
        ])
    _replace_tab_contents(sh, PARAMS_TAB, table, min_rows=500)


def write_readme_tab(sheet_id: str, client: gspread.Client) -> None:
    """Generates a plain-text self-documentation tab from the header
    constants, so it can never drift out of sync with the actual schema.
    Run at the end of the weekly analysis; safe to run any time.
    """
    sh = client.open_by_key(sheet_id)

    column_help = {
        "client_id": "identificador do cliente (livre — o mesmo texto em todas as abas).",
        "account_id": "GA4 property id ou Google Ads customer id, conforme a platform.",
        "platform": "GA4 ou GADS.",
        "event_name": "nome do evento GA4, ou o nome dado à conversão GAds.",
        "severity": "critical (alerta no Slack + triagem) ou secondary (só aparece na results).",
        "goback_days": "janela larga: FAIL se zero eventos nos últimos N dias. Vazio = 7.",
        "24hBackGA4_48hBackGAds": "sim/não — ativa o zero-check diário para eventos abaixo do volume mínimo.",
        "baseline_threshold_pct": "limiar do WARN da baseline automática, em % da mediana. Vazio = 50%.",
        "gtm_container_id": "GTM-XXXXXX; uma linha não vazia por cliente já basta.",
        "Nome_Tag_GTM": "preenchido pelo script — tag GTM que dispara o evento.",
        "GTM_Event_Params": "preenchido pelo script — parâmetros que essa tag envia (configuração, não prova de entrega).",
        "params_check": "override opcional dos parâmetros a verificar na GA4 (separados por vírgula). Vazio = automático a partir do GTM.",
    }
    config_lines = [f"  {col}: {column_help.get(col, '')}" for col in CONFIG_HEADERS]

    lines = [
        "TRACKING MONITOR — GUIA RÁPIDO (gerado automaticamente pelo script — não editar à mão)",
        "",
        "ABAS ESCRITAS PELO SCRIPT — substituídas por inteiro a cada corrida",
        "(a folha em si sobrevive: fórmulas e formatação não se perdem, só o conteúdo é atualizado):",
        f"  {RESULTS_TAB} — diária, pelo Daily Tracking Monitor.",
        f"  {HISTORY_TAB} — semanal, pelo 90-Day History Analysis.",
        f"  {DAILY_TAB} — diária e semanal (os dois jobs mantêm-na fresca).",
        f"  {PARAMS_TAB} — semanal, pelo 90-Day History Analysis (verificação de parâmetros GA4).",
        f"  {PROPOSAL_TAB} — só no Onboard New Client.",
        "",
        f"ABA '{CONFIG_TAB}' — a única editada à mão. Colunas:",
        *config_lines,
        "",
        "LEITURA DE OK / WARN / FAIL: FAIL = zero eventos na janela. WARN = abaixo do",
        "limiar face à mediana do dia-da-semana, ou 'silêncio recorde' (evento esporádico",
        "calado há mais tempo do que alguma vez esteve nos últimos 90 dias).",
        "",
        f"MARCADORES: '{NO_TAG}' e '{NO_PARAMS}' — evento sem tag GTM ativa correspondente.",
        f"'{NO_ACTIVE_TAG}' — linha de config a mais face às tags ativas (nunca apagada, só marcada).",
        "",
        f"ABA '{PARAMS_TAB}' — estado de cada parâmetro: 'verificado'/'built-in' (a GA4",
        "confirma que chega preenchido), 'não registado no GA4' (é preciso criar uma custom",
        "dimension para o poder verificar — é a to-do list), 'não verificável' (Google Ads",
        "não expõe parâmetros de conversão, nada a fazer).",
    ]

    table = [[line] for line in lines]
    _replace_tab_contents(sh, README_TAB, table, min_rows=len(table) + 10, spare_cols=1)


def write_daily_history(sheet_id: str, client: gspread.Client,
                        dates: list[str], series: dict[str, dict[str, float]]) -> None:
    """Writes the daily_history tab: one row per date, one column per
    (client|platform|event) series.

    Updated IN PLACE (clear + update, never delete + recreate) so native
    Sheets charts the user builds on this tab survive each refresh.
    """
    sh = client.open_by_key(sheet_id)
    labels = sorted(series)
    table = [["date"] + labels]
    for d in dates:
        table.append([d] + [series[label].get(d, 0) for label in labels])

    _replace_tab_contents(sh, DAILY_TAB, table, min_rows=len(table) + 10)
