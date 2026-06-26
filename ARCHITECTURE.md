# Karma Tracking Monitor — Arquitetura & Workflow

> **Para quem pega neste projeto pela primeira vez (humano ou Claude):** este documento
> explica *o quê*, o *como* e — mais importante — o *porquê* de cada decisão. Lê isto
> primeiro. Para a configuração inicial das credenciais, vê o [SETUP.md](SETUP.md).

---

## 1. O problema que isto resolve

Na Karma gerimos tracking (GA4 + Google Ads) para vários clientes. O risco recorrente:
**uma conversão/evento deixa de ser registado e ninguém repara durante semanas.** O caso
que motivou este projeto foi uma métrica de *Revenue* que deixou de ser passada ao Google
Ads via tag manager — a conversão existia, mas o valor não chegava. Descoberto tarde, por
acaso, por um consultor.

Este projeto é uma **verificação automática diária** que confirma se os eventos/conversões
mais relevantes de cada cliente continuam a ser registados em GA4 e Google Ads, e **alerta
ativamente** quando algo pára.

---

## 2. Princípios de design

1. **Nada hardcoded por cliente.** Toda a configuração vive numa Google Sheet. Adicionar
   um cliente = adicionar linhas na Sheet, sem tocar no código. Começámos com o cliente
   Westlake, mas o código nunca o assume.
2. **A Sheet é a única fonte de verdade.** IDs de conta, eventos a monitorizar, severidade,
   janelas temporais — tudo está na Sheet, não em ficheiros JSON nem no código.
3. **Dois canais de saída com propósitos diferentes.** A Sheet (`results`) é o histórico /
   dashboard; o Slack é o alerta ativo. Nem tudo o que vai à Sheet vai ao Slack.
4. **Evitar falsos positivos acima de tudo.** Um alerta que dispara sem motivo treina a
   equipa a ignorar alertas — e aí a ferramenta perde todo o valor. Várias decisões abaixo
   (limiar de 80%, janela de 48h para GAds) existem só por causa disto.
5. **Auth abstraída.** Hoje usamos OAuth2 com refresh token (a conta do João tem acesso a
   tudo via MCC da Karma). O desenho permite migrar para service accounts por cliente no
   futuro sem reescrever a lógica.

---

## 3. Visão geral do fluxo

```
                    ┌─────────────────────┐
                    │   Google Sheet      │
                    │  ┌───────────────┐  │
                    │  │ config (input)│  │◄──── tu editas isto
                    │  ├───────────────┤  │
                    │  │ results       │  │◄──── escrito pelo check diário
                    │  ├───────────────┤  │
                    │  │ history_analysis│ │◄──── escrito pela análise on-demand
                    │  └───────────────┘  │
                    └──────────┬──────────┘
                               │ lê config
          ┌────────────────────┼────────────────────┐
          │                    │                     │
   ┌──────▼──────┐      ┌───────▼───────┐             │
   │  GA4 Data   │      │  Google Ads   │             │
   │     API     │      │     API       │             │
   └──────┬──────┘      └───────┬───────┘             │
          │                     │                     │
          └─────────┬───────────┘                     │
                    │                                  │
         ┌──────────▼───────────┐          ┌───────────▼──────────┐
         │   main.py            │          │  analyze_history.py  │
         │  (check diário)      │          │  (on-demand)         │
         └──────────┬───────────┘          └───────────┬──────────┘
                    │                                  │
        ┌───────────┴──────────┐            escreve aba history_analysis
        │                      │
   escreve aba          envia alerta
    `results`            ao Slack
                    (só FAIL + critical)
```

Há **dois programas independentes**:

- **`main.py`** — o check diário. Corre via GitHub Actions todos os dias às 08:00 UTC.
- **`analyze_history.py`** — análise on-demand de 90 dias que te ajuda a decidir que
  eventos pôr no check de 24h. Corres quando quiseres (não é diário).

---

## 4. O modelo de dados (a Google Sheet)

### Aba `config` — o que TU controlas

| Coluna | Exemplo | Significado |
|---|---|---|
| `client_id` | `westlake` | Identificador livre do cliente |
| `account_id` | `481386094` | ID da property GA4 **ou** customer ID do Google Ads |
| `platform` | `GA4` ou `GAds` | A que plataforma pertence esta linha |
| `event_name` | `Footer_ContactUs` | Nome do evento (GA4) ou da conversão (GAds) |
| `severity` | `critical` ou `secondary` | `critical` → alerta Slack; `secondary` → só Sheet |
| `goback_days` | `10` | Janela larga de verificação, em dias (default 7 se vazio) |
| `24hours_lookback` | `sim` / `não` | Ativa o check rápido de 24h para este evento |

**Notas importantes:**
- Uma linha por (cliente, plataforma, evento). O mesmo `client_id` aparece em várias linhas.
- O `account_id` é repetido nas linhas do mesmo (cliente, plataforma) — o código deduz
  o mapa `{cliente: {plataforma: account_id}}` a partir destas linhas.
- **Não precisas de listar todos os eventos.** O check diário descobre dinamicamente todos
  os eventos/conversões da conta. Só listas na `config` os que queres configurar
  explicitamente (severidade, janela própria, flag de 24h). Os não listados são verificados
  na mesma e tratados como `secondary` com janela default.

### Aba `results` — escrita pelo check diário

Recriada a cada corrida (histórico limpo, sem acumular). Uma linha por (evento, janela):

| Coluna | Exemplo | Significado |
|---|---|---|
| `checked_at` | `2026-06-26 08:00 UTC` | Quando correu |
| `client_id`, `platform`, `event_name` | | Identificação |
| `severity` | `critical` | Copiado da config |
| `window` | `10d`, `24h`, `48h` | **Que janela** produziu esta linha (ver secção 5) |
| `count` | `5` | Eventos/conversões nessa janela |
| `status` | `OK` / `FAIL` | `FAIL` se `count == 0` |

> Um evento com `24hours_lookback=sim` produz **duas** linhas: uma da janela larga
> (`10d`) e outra da janela curta (`24h`/`48h`).

### Aba `history_analysis` — escrita pelo `analyze_history.py`

Recriada a cada corrida. Ajuda-te a preencher a coluna `24hours_lookback`:

| Coluna | Significado |
|---|---|
| `analyzed_at` | Quando correu a análise |
| `client_id`, `platform`, `event_name` | Identificação |
| `days_fired_of_90` | Em quantos dos últimos 90 dias o evento disparou ≥1 vez |
| `pct_days` | `days_fired_of_90 / 90` em percentagem |
| `avg_per_day` | Volume médio diário nos 90 dias |
| `suggestion_24h` | `sim` se disparou em ≥80% dos dias, senão `não` |

**Workflow de uso:** corres `analyze_history.py` → vês as sugestões → copias `sim`/`não`
para a coluna `24hours_lookback` da aba `config` nos eventos que decidires.

---

## 5. As duas janelas de verificação (o coração da lógica)

Cada evento pode ser verificado em **duas janelas com propósitos distintos**:

### Janela larga (`goback_days`) — sempre ativa
- "Este evento disparou nos últimos X dias?"
- Boa para saúde geral / tendência. Numa janela larga, quase todos os eventos relevantes
  disparam pelo menos uma vez, por isso `count == 0` é um sinal forte de que algo partiu.
- Configurável por evento (coluna `goback_days`).

### Janela curta (24h) — opcional, por evento (`24hours_lookback=sim`)
- "Este evento disparou *ontem*?" — deteção rápida de falhas (a diferença entre dar conta
  hoje vs. daqui a 10 dias).
- **Só faz sentido para eventos de disparo diário.** Um evento de baixo volume (2-3 por
  semana) dá `count == 0` em muitos dias *legitimamente* — verificá-lo a 24h geraria falsos
  positivos. Por isso o flag é seletivo e a sugestão exige ≥80% de dias com disparo.
- **Lógica de falha:** `count == 0` (zero absoluto). Não usamos baseline relativa (ainda).

#### Por que GA4 usa 24h mas GAds usa 48h
GA4 processa os eventos rápido — os dados de "ontem" já estão estáveis. O **Google Ads tem
*conversion lag***: uma conversão de ontem pode só ser totalmente reportada 24-72h depois
(janelas de atribuição). Um check estrito de 24h em GAds daria `count == 0` por os dados
ainda não terem assentado, não por avaria. Para dar margem, o check curto em GAds usa uma
janela de **48h** (`WINDOW_24H_DAYS = {"GA4": 1, "GADS": 2}` em [main.py](main.py)).

> O flag na Sheet chama-se sempre `24hours_lookback`; é o código que ajusta a janela real
> conforme a plataforma. A coluna `window` da aba `results` mostra a janela real (`24h`/`48h`).

---

## 6. Lógica de alertas

| Situação | Sheet `results` | Slack |
|---|---|---|
| `OK` (count > 0) | ✅ regista | — |
| `FAIL` + `secondary` | ✅ regista | — |
| `FAIL` + `critical` (qualquer janela) | ✅ regista | 🔔 alerta |

No Slack, a mensagem **etiqueta a janela** para a equipa perceber a urgência:
- 🔴 janela curta (`24h`/`48h`) = "partiu ontem" — urgente
- 🟠 janela larga (`10d`) = "não dispara há X dias"

Ver [src/slack.py](src/slack.py).

---

## 7. Os ficheiros do projeto

| Ficheiro | Papel |
|---|---|
| [main.py](main.py) | Orquestrador do check diário. Lê config, corre checks, escreve results, alerta. |
| [src/ga4.py](src/ga4.py) | Acesso à GA4 Data API. `fetch_event_counts` (janela) + `fetch_daily_event_counts` (breakdown diário p/ análise). |
| [src/gads.py](src/gads.py) | Acesso à Google Ads API. `fetch_conversion_counts` + `fetch_daily_conversion_counts`. |
| [src/sheets.py](src/sheets.py) | Leitura da config e escrita das abas `results` / `history_analysis`. Define os schemas (headers). |
| [src/slack.py](src/slack.py) | Formata e envia o alerta Slack via Incoming Webhook. |
| [analyze_history.py](analyze_history.py) | Análise de 90 dias on-demand → sugestões de 24h. |
| [setup_oauth.py](setup_oauth.py) | Fluxo OAuth local, uma vez. Gera os 3 valores p/ GitHub Secrets. |
| [.github/workflows/daily_check.yml](.github/workflows/daily_check.yml) | Cron diário (08:00 UTC) + trigger manual. |
| [.github/workflows/analyze_history.yml](.github/workflows/analyze_history.yml) | Trigger manual da análise de 90 dias. |

---

## 8. Autenticação & segredos

- **Auth:** OAuth2 com refresh token. Os scopes: `analytics.readonly`, `spreadsheets`,
  `adwords`. As mesmas credenciais Google servem GA4, Sheets e Google Ads.
- **Google Ads + MCC:** o developer token pertence à MCC da Karma (ID `237-375-5574`).
  Por isso o `login_customer_id` tem de ser o ID da MCC, não o do cliente. O `customer_id`
  na query é o do cliente (que está sob a MCC).
- **Segredos** (nunca no código — só em GitHub Secrets):
  `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`,
  `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`,
  `GOOGLE_SHEET_ID`, `SLACK_WEBHOOK_URL`.
- `client_secret.json` está no `.gitignore` e nunca é commitado.

---

## 9. Como adicionar um novo cliente

Não há código a mudar. Só a Sheet:

1. Na aba `config`, adiciona uma linha por cada (plataforma, evento) do cliente novo:
   - `client_id` novo, `account_id` (property GA4 ou customer ID GAds), `platform`,
     `event_name`, `severity`, `goback_days`.
2. Garante que a tua conta Google (via MCC) tem acesso à conta GAds e à property GA4 do
   cliente. (Para GAds, têm de estar sob a MCC da Karma.)
3. (Opcional) Corre o workflow **90-Day History Analysis** → vê a aba `history_analysis`
   → preenche `24hours_lookback=sim` nos eventos de disparo diário que queres vigiar a 24h.
4. Pronto. O próximo check diário já inclui o cliente novo.

> **Eventos importantes diferem por cliente.** O Westlake é lead-gen puro (conversões de
> *Submit lead form*, só conta `> 0`). Um cliente de e-commerce teria eventos de receita —
> o schema suporta isso, mas a verificação de *valor* (não só contagem) ainda não está
> implementada; é o próximo passo natural quando aparecer um cliente assim.

---

## 10. Decisões e estado (histórico para contexto)

- ✅ Config 100% Sheet-driven (migrámos de ficheiros JSON por cliente).
- ✅ `goback_days` configurável por evento.
- ✅ Check de 24h opcional por evento, com janela ajustada à plataforma (GA4 24h / GAds 48h).
- ✅ Análise de 90 dias para sugerir candidatos a 24h (limiar 80%).
- ⏸️ **Meta Ads** — discutido, adiável. O modelo Sheet-driven já comporta uma `platform`
  nova; faltaria um `src/meta.py` análogo e o ramo respetivo em `main.py`.
- ⏸️ **Verificação de valor/revenue** (não só contagem) — para clientes de e-commerce.
- ⏸️ **Baseline relativa** no check de 24h (alertar a <X% do esperado, não só a zero).

---

## 11. Notas operacionais

- O check diário **recria** a aba `results` a cada corrida — não acumula histórico linha a
  linha. Se quiseres histórico de longo prazo, é preciso mudar para append (decisão futura).
- Versões de dependências em [requirements.txt](requirements.txt). Nota: `google-ads` tem
  de ser uma versão com a API atual (usamos `31.1.0`); versões antigas usavam a API v17 já
  desativada e davam erro `GRPC target method can't be resolved`.
- Runner GitHub usa Python 3.11.
