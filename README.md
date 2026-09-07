# DEAi — Lark ↔ SAP HANA Bot

Turnover documentation for the next engineer. Start with §8 (Known Issues) —
everything else is reference.

---

## 1. What this system is

DEAi is a chat bot that lives inside **Lark (Feishu)** and lets staff query
the company's **SAP Business One** system (backed by **SAP HANA**) using
either slash-commands or plain natural language. It never lets a user run
arbitrary SQL — every query is either a hardcoded report or a
natural-language question that gets compiled into a validated,
parameterized, read-only SQL statement before it ever touches the database.

Two separate LLMs are used, each for a different job:

| Role | Purpose | Configured provider/model (default) |
|---|---|---|
| **ROUTER** | Classifies a user's natural-language question into one of 7 actions and, when needed, drafts a JSON "query plan" | Groq (`openai/gpt-oss-20b`) |
| **ANALYST** | Writes the human-readable narrative/summary on top of already-fetched, already-verified SAP data | Gemini (`gemini-3.6-flash`) |

Both are swappable per `.env` config — see §4.

---

## 2. Repo layout

```
project-root/
├── main.py                   # Entry point: Lark WS listener, routing, rate-limit/dedup/locks
├── test_connection.py         # Standalone script: confirms the HANA cert works
├── convert_cert.py            # One-off: DER (.cer) -> PEM converter
├── find_expense_tables.py     # One-off diagnostic script (see §8.3)
├── hana_server.cer             # Raw DER cert exported from HANA
├── hana_server.pem             # PEM-converted cert (produced by convert_cert.py)
├── bot_audit.log                # Rolling audit/error log (see §7)
├── .env                          # Real secrets (git-ignored)
├── .gitignore
└── modules/
    ├── __init__.py             # empty, marks package
    ├── config.py                # Loads .env once; every other module imports FROM here
    ├── database.py               # All HANA connection + SQL logic; only module that imports hdbcli
    ├── ai_router.py               # LLM client abstraction, NLU routing, report-writing prompts, grounding/verification
    └── lark_ui.py                  # Converts a Markdown reply string into a Lark interactive card (tables/charts)
```

**Import rule to preserve:** nothing outside `modules/config.py` should call
`os.getenv()` or `load_dotenv()` directly. Every setting flows through
`config.py` so `.env` is read exactly once and every module sees identical
values.

---

## 3. Runtime flow (end to end)

1. `main.py` opens a Lark WebSocket connection and registers
   `do_p2_im_message_receive_v1` as the handler for incoming messages.
2. Each incoming message is deduped (`_is_duplicate_event`, 10-minute
   window), rate-limited per user (`is_rate_limited`, min 3s between
   commands), and blocked if that chat already has a command in flight
   (`_active_chat_locks`).
3. `database.check_circuit_breaker()` is checked before doing any work — if
   3+ DB failures happened recently, the bot short-circuits with a cooldown
   message instead of hammering a struggling HANA instance.
4. A "⏳ Analyzing..." placeholder is sent immediately, then
   `_route_command()` dispatches based on the command prefix.
5. For canned commands (`!stock`, `!sales`, `!topclients`, `!aging`,
   `!report`), `database.py` runs a fixed, known-safe SQL query and
   `ai_router.py` turns the result into a written summary.
6. For `!ask <question>`:
   - `ai_router.parse_natural_language()` fetches the **live** SAP schema
     (`database.get_allowed_sap_schema`, queried fresh from
     `SYS.TABLE_COLUMNS` — never hardcoded) and sends it, plus the user's
     question, to the ROUTER LLM.
   - The ROUTER returns JSON classifying the question into one of 6 fixed
     actions, or a 7th `dynamic_query` action containing a JSON "query
     plan" (target tables, columns, filters, search keywords).
   - If it's `dynamic_query`, `database.execute_dynamic_query()` **never
     trusts the plan** — every table and column name is re-validated
     against the real schema, a hardcoded table blocklist is enforced,
     only `SELECT` is allowed, statement delimiters/comments are blocked,
     and all keyword filters are bound as SQL parameters (not
     string-interpolated).
7. Whatever raw pipe-delimited data comes back is handed to
   `ai_router._generate_grounded_report()`, which:
   - Builds the actual Markdown table in Python (not by asking the LLM to
     format it).
   - Computes record counts and (when it's actually safe to sum — see the
     docstring on `_compute_ground_truth_stats`) a verified total, in
     Python.
   - For large result sets, strips numeric columns out of what the ANALYST
     LLM sees, so it structurally cannot invent or misstate a number — it
     can only cite the pre-verified count/total it was handed as text.
   - Runs the ANALYST LLM to write the narrative, then
     `_verify_narrative`/`_filter_narrative_lines` check **every number
     the narrative states** against real cell values or the verified
     total, and silently drop any sentence that cites something
     unverifiable — so one bad number costs one bullet, not the whole
     reply.
8. `lark_ui.build_card()` parses the final Markdown (text + any tables)
   into a Lark interactive card, auto-generating a bar chart when there's
   a genuinely value-like numeric column to plot (it deliberately avoids
   charting ID-like columns such as DocNum).
9. The card is sent back as a threaded reply to the triggering message.

---

## 4. Configuration — `.env` variables

`.env` lives at the project root (one level above `modules/`) and is loaded
once by `modules/config.py`. **Actual values are not reproduced here** —
this table documents what each key controls so a new engineer knows what to
provision, not what the current secrets are.

| Variable | Purpose |
|---|---|
| `LARK_APP_ID` / `LARK_APP_SECRET` | Lark app credentials (WebSocket + API auth) |
| `SAP_HOST` / `SAP_PORT` | HANA host/port (default port 30015) |
| `SAP_USER` / `SAP_PASSWORD` | HANA login used for all queries (see §8.1 — currently a single shared master credential) |
| `SAP_SCHEMA` | HANA schema name queries run against |
| `SAP_SSL_HOSTNAME` | Expected hostname on the HANA server cert (default `DIRECHANASERVER`) |
| `SAP_CONNECT_TIMEOUT` | HANA connect timeout, ms (default 10000) |
| `SAP_MAX_ROWS` | Real, enforced row ceiling for query results — read as `database.QUERY_ROW_CAP` (default 3000) and used both for `fetchmany()` and for the "(Capped at max limit)" UI note. These two used to be out of sync (see §8.2) — as of this handoff they're the same number. |
| `ALLOW_RAW_SAP_SQL` | **Must stay `false`/unset.** `config.py` raises on import if this is ever `true` — see §8.4 |
| `ROUTER_LLM_PROVIDER` / `ROUTER_LLM_MODEL` | Which LLM classifies intent (`groq`, `gemini`, `anthropic`, or `openai`) |
| `ANALYST_LLM_PROVIDER` / `ANALYST_LLM_MODEL` | Which LLM writes report narratives |
| `ROUTER_LLM_TEMPERATURE` / `ANALYST_LLM_TEMPERATURE` | Sampling temperature per role (router defaults low/deterministic; analyst higher — see comment in `config.py` for why) |
| `LLM_REQUEST_TIMEOUT` | Timeout (s) applied to every LLM SDK client |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | Only the key(s) matching the providers actually selected above are required. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are read the same way if those providers are ever selected. |

Provider SDKs are imported lazily (`ai_router._get_client`) — only the
provider(s) actually configured need their package installed/key set.

---

## 5. Setup / running it

1. Install dependencies (`hdbcli`, `lark-oapi`, `python-dotenv`, plus
   whichever of `groq` / `google-generativeai` / `anthropic` / `openai`
   your configured providers need).
2. Populate `.env` per §4.
3. **Certificate:** SAP HANA needs a PEM cert for TLS validation
   (`sslValidateCertificate: True` in `config.HANA_CONFIG`). If you only
   have a DER-format `.cer` export from HANA, run:
   ```
   python convert_cert.py
   ```
   which wraps it into `hana_server.pem`. Where the app actually looks for
   this PEM at runtime should be confirmed against your `hdbcli`
   configuration/OS trust store setup — it isn't wired through `config.py`
   (see §8.5).
4. Sanity check the DB connection before starting the bot:
   ```
   python test_connection.py
   ```
5. Start the bot:
   ```
   python main.py
   ```
   This acquires a single-instance lock on `127.0.0.1:47291`
   (`config.SINGLE_INSTANCE_LOCK_PORT`) — a second copy will refuse to
   start with "Another instance of this bot is already running." This is
   a cheap safety net against double-replying to the same Lark messages;
   if you ever need to run two environments (e.g. staging + prod) side by
   side on the same host, they'll need different lock ports.

---

## 6. Command reference

| Command | What it does |
|---|---|
| `!ask <question>` | Natural-language query — routed through the ROUTER LLM to one of the actions below, or a dynamic query plan |
| `!report` | AI executive summary of recent SAP invoices (OINV) |
| `!sales [today\|week\|month]` | Sales velocity/revenue summary over a window |
| `!topclients [limit]` | Top revenue-generating clients (default 5, capped at 20) |
| `!aging` | Overdue open receivables |
| `!stock <ItemCode>` | Exact on-hand stock lookup (OITM) |
| `!help` | Lists all commands |

---

## 7. Operational notes

- **Audit log:** `bot_audit.log` (project root) — LLM API errors, circuit
  breaker trips, schema-discovery failures, and dropped/unverifiable
  narrative lines are all logged here via `config.audit_log`. Worth
  checking first when a user reports a wrong-looking answer, since a
  dropped narrative line will show up here even though the user just sees
  a shorter reply.
- **Circuit breaker:** after `FAILURE_THRESHOLD` (3) consecutive DB
  failures, the bot stops hitting HANA for `COOLDOWN_SECONDS` (300s) and
  returns a cooldown message instead. Resets on the next success.
- **Rate limit / dedup:** per-user 3s minimum between commands
  (`MIN_SECONDS_BETWEEN_COMMANDS`); duplicate Lark message IDs are ignored
  for 10 minutes (`DEDUP_WINDOW_SECONDS`).
- **Row caps:** query functions `fetchmany(database.QUERY_ROW_CAP)`
  (default 3000, set via `SAP_MAX_ROWS`); UI tables are additionally
  capped at 100 displayed rows (`lark_ui.MAX_UI_ROWS`) with a note that
  the total/chart still reflect the full result.

---

## 8. Known issues / open items for the next engineer

### 8.1 No per-user database role separation is active — highest priority

`database._get_connection_for_user()` currently connects **every** Lark
user with the same single master `SAP_USER` credential from `.env`. The
`lark_user_id` passed through the whole call chain (and logged as
`"Querying DEAI_USER_MAP for Lark open_id: ..."` throughout `main.py`) is
**not actually used** to scope access — that logging currently implies a
security boundary that doesn't exist. The function's docstring spells out
the two real options:

- **(a)** Implement real per-user credential lookup against a
  `DEAI_USER_MAP` table and connect with that user's own SAP-side
  read role, or
- **(b)** If a single shared identity is the intended design for this
  deployment, remove/rewrite the `DEAI_USER_MAP` log lines elsewhere so
  they stop implying per-user scoping.

Read-only enforcement (`_enforce_read_only`, `SET TRANSACTION READ ONLY`)
still applies regardless — nobody can write — but there is currently no
per-user data scoping. Decide and act on this before this bot is exposed
beyond a fully-trusted user group.

### 8.2 Row cap — fixed as of this handoff

Previously, `config.MAX_ROWS` (from `SAP_MAX_ROWS`, default 50) was defined
but never actually applied to any `fetchmany()` call — every query function
hardcoded `fetchmany(3000)` instead. The "(Capped at max limit)" warning in
`ai_router._generate_grounded_report` compared against the unused 50-row
config value, so it fired on any 50+ row result even when nothing was
actually truncated (the real ceiling was 3000).

This is now fixed: `database.py` exposes a single `QUERY_ROW_CAP` constant
(sourced from `config.MAX_ROWS`/`SAP_MAX_ROWS`) used by **every** query
function's `fetchmany()` call, and `ai_router.py`'s capped-warning check
compares against that same constant. If you want a different effective row
limit, change `SAP_MAX_ROWS` in `.env` — both the fetch and the warning
will move together.

### 8.3 `find_expense_tables.py`

One-off diagnostic script (not imported by the app) written while
investigating where employee expense claims physically live in SAP B1's
schema — it concluded they're AP Invoices against vendor-type Business
Partner cards (OPCH/PCH1), not a custom UDT or OEXD/AEXD. That conclusion
is now baked into `database._TABLE_DESCRIPTIONS` and the ROUTER's system
prompt in `ai_router.parse_natural_language`. Safe to delete or keep as a
reference/audit trail.

### 8.4 `ALLOW_RAW_SAP_SQL` is a hard tripwire, not a feature flag

If it's ever set `true` in `.env`, `config.py` raises `RuntimeError` at
import and refuses to start. This is deliberate — the configured HANA user
(`HANASA`) has write access at the DB-grant level, so the *application
layer* enforcing read-only (`database._enforce_read_only` and
`_set_session_read_only`) is the only thing currently standing between a
bug and a write. Don't relax this without also revisiting the DB-level
grants.

### 8.5 Cert file location isn't centrally configured

`convert_cert.py` reads `hana_server.cer` and writes `hana_server.pem`
relative to wherever it's run from, and `config.HANA_CONFIG` doesn't
reference a file path directly (validation is handled by `hdbcli`/the OS
trust store). Worth confirming exactly how/where the PEM is being trusted
in the current deployment environment before renewing or rotating the
cert. Note: `hana_server.pem` is a public certificate, not a private key —
it identifies the server, it isn't itself a secret — but it should still
move with the deployment, not just live loose in the repo.

### 8.6 Router refusals are a known, expected failure mode — not a bug

`main._handle_ask` explicitly handles a `"router_refused"` error when the
small router model treats payroll/compensation-sounding phrasing (e.g.
"expenses"/"allowance" together) as sensitive and declines to return JSON.
The system prompt already works to keep this from firing on legitimate
accounting questions; if it starts firing more often, the documented
option is switching `ROUTER_LLM_MODEL` to a less conservative model.

---

## 9. SAP B1 table reference (as understood by the router)

This is the live description text the ROUTER LLM is given for each table
(`database._TABLE_DESCRIPTIONS`) — useful shorthand for a human too:

| Table | What it is |
|---|---|
| `OINV` / `INV1` | A/R Invoice header / lines — sales invoices issued to customers |
| `OPCH` / `PCH1` | A/P Invoice header / lines — vendor bills **and** employee expense/reimbursement claims (employees + "Petty Cash" are registered as vendor-type cards) |
| `OJDT` / `JDT1` | Journal Entry header / lines — manual/internal GL postings only (accruals, adjustments) — **not** used for employee expense claims |
| `OITM` | Item Master |
| `OCRD` | Business Partner Master (customers and vendors, incl. employees-as-vendors) |
| `OSLP` | Sales Employee Master |
| `ORDR` / `RDR1` | Sales Order header / lines |
| `OSRN` | Serial Number Master |

Blocked outright regardless of query plan (`database._TABLE_BLOCKLIST`):
`OHEM`, `HEM1`, `HEM6` (HR/employee master), `OUSR`, `USR1`, `OATC` (user
credentials).

---

## 10. Contact points for questions

_(Fill in: who owns the Lark app registration, who owns the SAP HANA
credentials/DBA relationship, and who owns the LLM provider API keys/billing,
if that isn't already documented elsewhere.)_