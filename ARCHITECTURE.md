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
                    │  ├───────────────┤  │
                    │  │ daily_history │  │◄──── idem (matriz p/ gráficos, in-place)
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
| `goback_days` | `10` | Janela larga de verificação, em dias (default 7 se vazio; máx. 90) |
| `24hBackGA4_48hBackGAds` | `sim` / `não` | Força o check curto de zero para eventos de baixo volume (ver secção 5) |
| `baseline_threshold_pct` | `40` | *(Opcional)* Limiar do WARN em % da mediana do dia-da-semana (vazio = 50). Aceita `40`, `40%` ou `0.4` |
| `gtm_container_id` | `GTM-ABC123` | *(Opcional)* Container GTM do cliente — basta preencher numa linha do cliente |
| `Nome_Tag_GTM` | `GA4 - Footer Contact` | *(Opcional, **preenchida pelo script**, não à mão)* Que tag GTM dispara este evento — puramente informativa |

> As colunas opcionais podem nem existir na Sheet — o código tolera a ausência
> (só exige as colunas que existirem na primeira linha). A `Nome_Tag_GTM` é a única
> exceção ao princípio "a config é editada por humanos": o passo GTM do
> `analyze_history.py` escreve **apenas** nessa coluna, em linhas existentes,
> e nunca toca em mais nada (ver secção 5-bis).

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
| `expected` | `45.0` | Mediana do dia-da-semana usada como baseline (vazio nas linhas sem check relativo) |
| `status` | `OK` / `WARN` / `FAIL` | `FAIL` se `count == 0`; `WARN` se `count` < limiar × `expected` |

> Um evento verificado na janela curta (por baseline automática ou por flag) produz
> **duas** linhas: uma da janela larga (`10d`) e outra da janela curta (`24h`/`48h`).
> Um evento configurado que **desapareça por completo** dos dados continua a gerar a
> linha da janela larga com `count 0` → `FAIL` (o universo reportado é a união dos
> eventos descobertos na API com os eventos listados na `config`).

### Aba `history_analysis` — escrita pelo `analyze_history.py`

Recriada a cada corrida. Ajuda-te a preencher a coluna `24hBackGA4_48hBackGAds`:

| Coluna | Significado |
|---|---|
| `analyzed_at` | Quando correu a análise |
| `client_id`, `platform`, `event_name` | Identificação |
| `days_fired_of_90` | Em quantos dos últimos 90 dias o evento disparou ≥1 vez |
| `pct_days` | `days_fired_of_90 / 90` em percentagem |
| `avg_per_day` | Volume médio diário nos 90 dias |
| `median_per_day` | Mediana diária nos 90 dias (robusta a picos de campanha) |
| `weekday_medians` | Mediana por dia-da-semana (`Seg 12 · Ter 14 · …`) — para calibrar o `baseline_threshold_pct` |
| `suggestion_24h` | `sim` se disparou em ≥80% dos dias, senão `não` |

**Workflow de uso:** corres `analyze_history.py` → vês as sugestões → copias `sim`/`não`
para a coluna `24hBackGA4_48hBackGAds` da aba `config` nos eventos de baixo volume que decidires
vigiar; usas as medianas para ajustar thresholds.

### Aba `daily_history` — escrita pelo `analyze_history.py`

Matriz de visualização: uma linha por data (últimos 90 dias) e uma coluna por
(`cliente|plataforma|evento`). É atualizada **in-place** (clear + update, nunca
apagada e recriada) precisamente para os **gráficos nativos do Google Sheets** que
criares sobre ela sobreviverem a cada refresh.

---

## 5. As duas janelas de verificação (o coração da lógica)

Cada evento pode ser verificado em **duas janelas com propósitos distintos**:

### Janela larga (`goback_days`) — sempre ativa
- "Este evento disparou nos últimos X dias?"
- Boa para saúde geral / tendência. Numa janela larga, quase todos os eventos relevantes
  disparam pelo menos uma vez, por isso `count == 0` é um sinal forte de que algo partiu.
- Configurável por evento (coluna `goback_days`).

### Janela curta (24h/48h) — automática acima do volume mínimo, opt-in abaixo
- "Este evento disparou no último dia *estável*, e em volume normal?" — deteção rápida
  de falhas (a diferença entre dar conta hoje vs. daqui a 10 dias).
- **Baseline relativa (automática).** Para cada evento calcula-se a **mediana do
  dia-da-semana** testado sobre os 90 dias anteriores (~12 amostras do mesmo dia da
  semana — uma terça compara-se com as últimas ~12 terças). Se essa mediana for
  ≥ `BASELINE_MIN_MEDIAN` (10/dia), o evento entra no check curto **sem configuração
  nenhuma**, com três estados:
  - `FAIL` — `count == 0` (morreu);
  - `WARN` — `count` < 50% da mediana (default; override por `baseline_threshold_pct`);
  - `OK` — caso contrário.
- **Porquê mediana por dia-da-semana?** Resolve dois falsos positivos de uma vez: a
  sazonalidade semanal (sábado tem naturalmente menos volume que terça — comparar com a
  média geral dispararia todos os fins de semana) e os outliers (um pico de campanha
  inflaciona uma média, mas quase não move uma mediana).
- **Flag `24hBackGA4_48hBackGAds=sim`** — continua a existir para eventos **abaixo** do volume
  mínimo que queiras mesmo assim vigiar diariamente: recebem só o check de zero
  (`OK`/`FAIL`), sem WARN, porque em baixo volume a comparação percentual é ruído.
  A sugestão do `analyze_history.py` (≥80% de dias com disparo) mantém-se para este flag.

#### Por que GA4 testa "ontem" mas GAds testa "anteontem"
GA4 processa os eventos rápido — os dados de "ontem" já estão estáveis. O **Google Ads tem
*conversion lag***: uma conversão de ontem pode só ser totalmente reportada 24-72h depois
(janelas de atribuição). Testar "ontem" em GAds daria `count` baixo por os dados ainda não
terem assentado, não por avaria. Por isso o check curto em GAds testa o **dia mais recente
estável — anteontem** (`STABLE_DAY_OFFSET = {"GA4": 1, "GADS": 2}` em [main.py](main.py)),
comparado com a mediana do dia-da-semana respetivo.

> A coluna `window` da aba `results` mostra `24h` (GA4) ou `48h` (GAds) para o check curto.

### Uma só chamada à API por (cliente, plataforma)
O check diário faz **um único fetch de 90 dias com breakdown diário** por conta
(`fetch_daily_event_counts` / `fetch_daily_conversion_counts`) e calcula localmente a
janela larga (soma dos últimos `goback_days` dias), o check curto e a baseline a partir
da mesma matriz `{evento: {data: contagem}}`. Menos chamadas do que uma por
`goback_days` distinto, e uma única fonte de dados para tudo. Consequência: `goback_days`
está limitado a 90.

---

## 5-bis. Mapeamento evento → tag GTM (informativo)

Os nomes dos eventos (GA4/GAds) diferem dos nomes das tags no GTM. Para a Sheet ser
legível, o `analyze_history.py` preenche a coluna `Nome_Tag_GTM` da `config` com a(s)
tag(s) GTM que disparam cada evento. **Só documentação — não entra em nenhum check.**

O matching é **determinístico via Tag Manager API** (nada de LLM/inferência):
- Lê a versão **publicada** (live) do container — o que dispara em produção, não o draft.
- Tag GA4 Event (`gaawe`): o parâmetro `eventName` **é** o nome do evento → join direto.
  Se o `eventName` usa variáveis (`{{...}}`), o nome é dinâmico e não é mapeável — a tag
  é reportada no log do workflow e ignorada.
- Tag de conversão GAds (`awct`): tem `conversionLabel`; do lado GAds, o label extrai-se
  dos `tag_snippets` de cada conversion action (`fetch_conversion_labels` em
  [src/gads.py](src/gads.py)) → join exato por label.

Convenções na célula: múltiplas tags → `Tag A + Tag B`; tag pausada → `Nome (pausada)`;
sem correspondência → `(sem tag GTM)`. O valor reflete o estado do container a cada
corrida (é reescrito, não preservado).

**Falha graciosa:** o passo GTM está isolado em try/except — sem scope no token, sem
acesso ao container, ou container inexistente → aviso no log e a análise completa na
mesma. O check diário (`main.py`) não usa o scope GTM de todo.

---

## 6. Lógica de alertas

| Situação | Sheet `results` | Slack |
|---|---|---|
| `OK` | ✅ regista | — |
| `FAIL` ou `WARN` + `secondary` | ✅ regista | — |
| `FAIL` + `critical` (qualquer janela) | ✅ regista | 🔔 alerta |
| `WARN` + `critical` | ✅ regista | 🔔 alerta |

No Slack, a mensagem **etiqueta a janela e o tipo** para a equipa perceber a urgência:
- 🔴 `FAIL` na janela curta (`24h`/`48h`) = "partiu ontem" — urgente
- 🟠 `FAIL` na janela larga (`10d`) = "não dispara há X dias"
- 🟡 `WARN` = "ainda dispara, mas muito abaixo do normal" (mostra count vs. mediana e % abaixo)

Ver [src/slack.py](src/slack.py).

---

## 7. Os ficheiros do projeto

| Ficheiro | Papel |
|---|---|
| [main.py](main.py) | Orquestrador do check diário. Lê config, corre checks (janelas + baseline), escreve results, alerta. |
| [src/baseline.py](src/baseline.py) | Helpers puros (sem APIs): janelas, mediana por dia-da-semana, decisão OK/WARN/FAIL, parsing do threshold. Testável com dados sintéticos. |
| [src/ga4.py](src/ga4.py) | Acesso à GA4 Data API. `fetch_daily_event_counts` (breakdown diário, base de tudo) + `fetch_event_counts` (janela agregada, legado). |
| [src/gads.py](src/gads.py) | Acesso à Google Ads API. `fetch_daily_conversion_counts` + `fetch_conversion_labels` (labels p/ matching GTM) + `fetch_conversion_counts` (legado). |
| [src/gtm.py](src/gtm.py) | Acesso à Tag Manager API (versão live) + matching determinístico evento↔tag. Helpers puros testáveis sem APIs. |
| [src/sheets.py](src/sheets.py) | Leitura da config e escrita das abas `results` / `history_analysis` / `daily_history`. Define os schemas (headers). |
| [src/slack.py](src/slack.py) | Formata e envia o alerta Slack via Incoming Webhook (🔴/🟠 FAIL, 🟡 WARN). |
| [analyze_history.py](analyze_history.py) | Análise de 90 dias on-demand → sugestões de 24h, medianas p/ calibração, aba `daily_history` e preenchimento da `Nome_Tag_GTM`. |
| [setup_oauth.py](setup_oauth.py) | Fluxo OAuth local, uma vez. Gera os 3 valores p/ GitHub Secrets. |
| [.github/workflows/daily_check.yml](.github/workflows/daily_check.yml) | Cron diário (08:00 UTC) + trigger manual. |
| [.github/workflows/analyze_history.yml](.github/workflows/analyze_history.yml) | Trigger manual da análise de 90 dias. |

---

## 8. Autenticação & segredos

- **Auth:** OAuth2 com refresh token. Os scopes: `analytics.readonly`, `spreadsheets`,
  `adwords` e (desde jul 2026) `tagmanager.readonly`. As mesmas credenciais Google servem
  GA4, Sheets, Google Ads e GTM.
- **Atenção aos scopes:** um refresh token fica preso aos scopes consentidos quando foi
  criado — não se acrescentam depois. Tokens gerados antes de jul 2026 não têm o scope
  GTM: o check diário funciona na mesma (não o usa), mas o passo GTM do analyze é
  saltado com aviso até correres `setup_oauth.py` de novo e atualizares o secret
  `GOOGLE_REFRESH_TOKEN`.
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
   → preenche `24hBackGA4_48hBackGAds=sim` nos eventos de disparo diário que queres vigiar a 24h.
4. Pronto. O próximo check diário já inclui o cliente novo.

> **Eventos importantes diferem por cliente.** O Westlake é lead-gen puro (conversões de
> *Submit lead form*, só conta `> 0`). Um cliente de e-commerce teria eventos de receita —
> o schema suporta isso, mas a verificação de *valor* (não só contagem) ainda não está
> implementada; é o próximo passo natural quando aparecer um cliente assim.

---

## 10. Decisões e estado (histórico para contexto)

- ✅ Config 100% Sheet-driven (migrámos de ficheiros JSON por cliente).
- ✅ `goback_days` configurável por evento (máx. 90).
- ✅ Check curto com janela ajustada à plataforma (GA4 testa ontem / GAds testa anteontem).
- ✅ Análise de 90 dias para sugerir candidatos a 24h (limiar 80%).
- ✅ **Baseline relativa** (jul 2026): mediana por dia-da-semana, WARN abaixo de 50%
  (configurável), automática para eventos com mediana ≥10/dia. Aba `daily_history` para
  visualização. Corrigido também o ponto cego em que um evento configurado totalmente
  morto desaparecia dos results sem FAIL.
- ✅ **Mapeamento GTM** (jul 2026): coluna informativa `Nome_Tag_GTM` preenchida pelo
  analyze_history via Tag Manager API — matching determinístico, sem LLM (a ideia
  original de um workflow com LLM foi descartada: o join por `eventName`/label é exato).
- ⏸️ **Meta Ads** — discutido, adiável. O modelo Sheet-driven já comporta uma `platform`
  nova; faltaria um `src/meta.py` análogo e o ramo respetivo em `main.py`.
- ⏸️ **Verificação de valor/revenue** (não só contagem) — para clientes de e-commerce.
- ⏸️ **Renomeação de eventos** — considerada (jun 2026) e adiada: os nomes atuais são
  autoexplicativos. Se um dia avançar, atenção à descontinuidade de série no GA4.

---

## 11. Notas operacionais

- O check diário **recria** a aba `results` a cada corrida — não acumula histórico linha a
  linha. Se quiseres histórico de longo prazo, é preciso mudar para append (decisão futura).
- A aba `daily_history` é a exceção: é atualizada **in-place** para os gráficos nativos
  do Sheets criados sobre ela não morrerem. (Se o conjunto de eventos mudar, as colunas
  deslocam-se — pode ser preciso reapontar os ranges dos gráficos.)
- Versões de dependências em [requirements.txt](requirements.txt). Nota: `google-ads` tem
  de ser uma versão com a API atual (usamos `31.1.0`); versões antigas usavam a API v17 já
  desativada e davam erro `GRPC target method can't be resolved`.
- Runner GitHub usa Python 3.11.
