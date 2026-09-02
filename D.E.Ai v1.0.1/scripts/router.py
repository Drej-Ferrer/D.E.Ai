"""
DEA Router — Hermes -> Lark bot command handler
=================================================

Handles:
  !Group          -> group/channel info command
  !Lark           -> Lark-native command
  !Sap <query>    -> read-only lookup against SAP Business One (SBOLIVE_DBTI schema)

Design notes:
  - All secrets come from environment variables (see .env.example). Nothing is
    hardcoded in this file. Rotate any credential that was ever pasted into
    chat, a ticket, or an LLM prompt.
  - !Sap does NOT accept arbitrary free-text SQL from chat by default. It
    accepts either:
      (a) a short alias that maps to a pre-approved, parameterized query
          (recommended — see APPROVED_QUERIES below), or
      (b) if ALLOW_RAW_SAP_SQL=true in the environment, a raw SELECT that is
          passed through a hard filter (single statement, SELECT-only, no
          dangerous keywords, forced LIMIT). This is a convenience switch for
          trusted internal use, not a real security boundary — prefer (a) for
          anything exposed beyond a small trusted group.
  - DB connections are opened per-request and always closed (context manager).
"""

import os
import re
import logging
import time
import json
from contextlib import contextmanager
from datetime import date
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from dotenv import load_dotenv
from hdbcli import dbapi
import requests

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ---------------------------------------------------------------------------
# Config (env vars — do not hardcode credentials here)
# ---------------------------------------------------------------------------

LARK_APP_ID = os.environ["LARK_APP_ID"]
LARK_APP_SECRET = os.environ["LARK_APP_SECRET"]

SAP_HOST = os.environ.get("SAP_HOST", "192.168.2.128")
SAP_PORT = int(os.environ.get("SAP_PORT", "30015"))
SAP_USER = os.environ["SAP_USER"]
SAP_PASSWORD = os.environ["SAP_PASSWORD"]
SAP_SCHEMA = os.environ.get("SAP_SCHEMA", "SBOLIVE_DBTI")

ALLOW_RAW_SAP_SQL = os.environ.get("ALLOW_RAW_SAP_SQL", "false").lower() == "true"
MAX_ROWS = int(os.environ.get("SAP_MAX_ROWS", "50"))
RETRY_ATTEMPTS = int(os.environ.get("ROUTER_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF_SECONDS = float(os.environ.get("ROUTER_RETRY_BACKOFF_SECONDS", "1"))
SAP_CONNECT_TIMEOUT = int(os.environ.get("SAP_CONNECT_TIMEOUT", "10"))  # seconds

LARK_OPEN_API = "https://open.larksuite.com/open-apis"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dea-router")

# ---------------------------------------------------------------------------
# Approved queries (preferred path for !Sap)
# ---------------------------------------------------------------------------

TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

APPROVED_QUERIES = {
    "OCRD": {
        "sql": f'SELECT TOP {MAX_ROWS} "CardCode", "CardName", "CardType", "Balance" '
               f'FROM "{SAP_SCHEMA}"."OCRD" ORDER BY "CardCode"',
        "params": [],
        "description": "Business Partners master (top rows)",
    },
    "OCRD_SEARCH": {
        "sql": f'SELECT TOP {MAX_ROWS} "CardCode", "CardName", "CardType", "Balance" '
               f'FROM "{SAP_SCHEMA}"."OCRD" WHERE "CardName" LIKE ? ORDER BY "CardCode"',
        "params": ["text"],
        "description": "Search business partners by name",
    },
    "OITM": {
        "sql": f'SELECT TOP {MAX_ROWS} "ItemCode", "ItemName", "OnHand", "ItmsGrpCod" '
               f'FROM "{SAP_SCHEMA}"."OITM" ORDER BY "ItemCode"',
        "description": "Items master (top rows)",
        "params": [],
    },
    "SALES_SUMMARY": {
        "sql": f'SELECT TOP {MAX_ROWS} "DocNum", "CardName", "DocDate", "DocTotal" '
               f'FROM "{SAP_SCHEMA}"."OINV" ORDER BY "DocDate" DESC',
        "params": [],
        "description": "Recent sales invoices and summary totals",
    },
}

def translate_sap_request(request: str):
    normalized = re.sub(r"\s+", " ", request.strip().lower())
    if not normalized:
        return None

    if re.search(r"\b(?:top|best)\s+customers?\b", normalized):
        return (
            f'SELECT TOP {MAX_ROWS} "CardCode", SUM("DocTotal") AS "TotalSales" '
            f'FROM "{SAP_SCHEMA}"."OINV" GROUP BY "CardCode" '
            f'ORDER BY "TotalSales" DESC'
        )

    if re.search(r"\b(?:sales?|summary|summaries|invoices?|2025)\b", normalized):
        return (
            f'SELECT TOP {MAX_ROWS} "DocNum", "CardName", "DocDate", "DocTotal" '
            f'FROM "{SAP_SCHEMA}"."OINV" ORDER BY "DocDate" DESC'
        )

    if re.search(r"\b(?:customers?|business\s+partners?)\b", normalized):
        return (
            f'SELECT TOP {MAX_ROWS} "CardCode", "CardName", "CardType", "Balance" '
            f'FROM "{SAP_SCHEMA}"."OCRD" ORDER BY "CardCode"'
        )

    if re.search(r"\b(?:items?|inventory|stock)\b", normalized):
        return (
            f'SELECT TOP {MAX_ROWS} "ItemCode", "ItemName", "OnHand", "ItmsGrpCod" '
            f'FROM "{SAP_SCHEMA}"."OITM" ORDER BY "ItemCode"'
        )

    return None

# ---------------------------------------------------------------------------
# Raw-SQL safety filter
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|GRANT|REVOKE|"
    r"EXEC|EXECUTE|CALL|COMMIT|ROLLBACK|xp_|sp_)\b",
    re.IGNORECASE,
)
_SELECT_ONLY = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_SINGLE_STATEMENT = re.compile(r";\s*\S")

class UnsafeQueryError(Exception):
    pass

def sanitize_raw_select(raw_sql: str) -> str:
    sql = raw_sql.strip().rstrip(";")
    if not _SELECT_ONLY.match(sql):
        raise UnsafeQueryError("Only SELECT statements are allowed.")
    if _FORBIDDEN_KEYWORDS.search(sql):
        raise UnsafeQueryError("Query contains a disallowed keyword.")
    if _SINGLE_STATEMENT.search(raw_sql):
        raise UnsafeQueryError("Only a single statement is allowed.")
    if SAP_SCHEMA.upper() not in sql.upper():
        raise UnsafeQueryError(f"Query must explicitly reference the {SAP_SCHEMA} schema.")

    if not re.search(r"\bTOP\s+\d+\b", sql, re.IGNORECASE) and not re.search(
        r"\bLIMIT\s+\d+\b", sql, re.IGNORECASE
    ):
        sql = re.sub(r"^\s*SELECT\b", f"SELECT TOP {MAX_ROWS}", sql, count=1, flags=re.IGNORECASE)

    return sql

# ---------------------------------------------------------------------------
# SAP HANA connection
# ---------------------------------------------------------------------------

@contextmanager
def hana_connection():
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        dbapi.connect,
        address=SAP_HOST,
        port=SAP_PORT,
        user=SAP_USER,
        password=SAP_PASSWORD,
    )
    try:
        conn = future.result(timeout=SAP_CONNECT_TIMEOUT)
    except FutureTimeoutError:
        executor.shutdown(wait=False)
        raise dbapi.Error(
            f"Connection to {SAP_HOST}:{SAP_PORT} timed out after "
            f"{SAP_CONNECT_TIMEOUT}s (SAP_CONNECT_TIMEOUT)."
        )
    else:
        executor.shutdown(wait=False)

    try:
        yield conn
    finally:
        conn.close()

def run_sap_query(sql: str, params=None):
    params = params or []
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with hana_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(sql, params)
                    columns = [d[0] for d in cursor.description] if cursor.description else []
                    rows = cursor.fetchmany(MAX_ROWS)
                    return columns, rows
                finally:
                    cursor.close()
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e) or isinstance(e, dbapi.Error):
                if attempt == RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
                continue
            raise

def request_with_retry(method: str, url: str, **kwargs):
    for attempt in range(RETRY_ATTEMPTS):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
            
def _markdown_cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")

def _format_metric(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,.2f}" if isinstance(value, float) else f"{value:,}"
    return str(value)

def _is_numeric_metric(column: str) -> bool:
    name = column.lower()
    return not any(term in name for term in ("code", "num", "id"))

def _numeric_value(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.\-]", "", value)
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None

def _report_title(request_context: str) -> str:
    normalized = request_context.lower()
    if "yesterday" in normalized:
        return "**Yesterday's SAP sales report**"
    if re.search(r"\bq[1-4]\b", normalized):
        quarter = re.search(r"\bq[1-4]\b", normalized).group(0).upper()
        return f"**{quarter} SAP sales report**"
    if "top customers" in normalized or "best customers" in normalized:
        return "**Top SAP customers by sales**"
    if any(term in normalized for term in ("inventory", "stock", "item")):
        return "**SAP inventory report**"
    if any(term in normalized for term in ("customer", "business partner", "partner")):
        return "**SAP customer and business partner report**"
    if any(term in normalized for term in ("sales", "invoice", "summary")):
        return "**SAP sales and invoice summary**"
    return "**SAP executive report**"

def format_rows_for_lark(columns, rows, request_context="") -> str:
    title = _report_title(request_context)
    if not rows:
        return f"{title}\n\nNo records returned.\n\nWhat would you like to explore next?"

    table_rows = [
        "| " + " | ".join(_markdown_cell(column) for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    table_rows.extend(
        "| " + " | ".join(_markdown_cell(value) if value is not None else "" for value in row) + " |"
        for row in rows
    )

    metrics = [f"- **Records returned:** {len(rows):,}"]
    financial_total = 0
    financial_column = None
    status_counts = {}
    largest_value = None
    date_index = None
    status_index = None
    
    for index, column in enumerate(columns):
        values = [row[index] for row in rows if index < len(row)]
        numeric_values = [_numeric_value(value) for value in values]
        numeric_values = [value for value in numeric_values if value is not None]
        column_name = column.lower()
        if numeric_values and _is_numeric_metric(column):
            if any(term in column_name for term in ("total", "amount", "balance", "value")):
                financial_column = column
                financial_total = sum(numeric_values)
            if largest_value is None or max(numeric_values) > largest_value:
                largest_value = max(numeric_values)
        if "status" in column_name:
            status_index = index
            for value in values:
                status_counts[str(value)] = status_counts.get(str(value), 0) + 1
        if "date" in column_name:
            date_index = index
            
    if financial_column:
        metrics.append(f"- **Total {financial_column}:** {_format_metric(financial_total)}")
    if status_counts:
        metrics.append("- **Status breakdown:** " + "; ".join(
            f"{status}: {count:,}" for status, count in status_counts.items()
        ))
    if largest_value is not None:
        metrics.append(f"- **Largest {financial_column or 'numeric'} value:** {_format_metric(largest_value)}")
    if date_index is not None and status_index is not None:
        overdue = []
        for row in rows:
            status = str(row[status_index]).lower()
            try:
                record_date = row[date_index]
                record_date = record_date.date() if hasattr(record_date, "date") else record_date
                if "open" in status and record_date < date.today():
                    overdue.append(record_date)
            except (TypeError, ValueError):
                continue
        if overdue:
            metrics.append(f"- **Attention:** {len(overdue):,} open record(s) are dated before today and may need follow-up.")

    report = title + "\n\n" + "\n".join(table_rows) + "\n\n" + "\n".join(metrics)
    if len(report) > 3500:
        report = report[:3500].rsplit("\n", 1)[0] + "\n\n... (report truncated)"
    return report

# ---------------------------------------------------------------------------
# !Sap command handler
# ---------------------------------------------------------------------------

def handle_sap_command(arg_text: str) -> str:
    arg_text = arg_text.strip()
    if not arg_text:
        aliases = ", ".join(APPROVED_QUERIES)
        return f"Usage: !Sap <alias> [param]. Available: {aliases}"

    parts = arg_text.split(maxsplit=1)
    alias = parts[0].upper()
    rest = parts[1] if len(parts) > 1 else ""

    try:
        translated_sql = translate_sap_request(arg_text)
        if translated_sql:
            columns, rows = run_sap_query(translated_sql)
        elif alias in APPROVED_QUERIES:
            spec = APPROVED_QUERIES[alias]
            params = [f"%{rest}%"] if spec["params"] else []
            if spec["params"] and not rest:
                return f"'{alias}' requires a search term, e.g. !Sap {alias} acme"
            columns, rows = run_sap_query(spec["sql"], params)

        elif ALLOW_RAW_SAP_SQL and _SELECT_ONLY.match(arg_text):
            safe_sql = sanitize_raw_select(arg_text)
            columns, rows = run_sap_query(safe_sql)

        elif ALLOW_RAW_SAP_SQL and TABLE_NAME_RE.match(alias) and not rest:
            sql = f'SELECT TOP {MAX_ROWS} * FROM "{SAP_SCHEMA}"."{alias}"'
            columns, rows = run_sap_query(sql)

        else:
            aliases = ", ".join(APPROVED_QUERIES)
            return (
                f"Unknown command '{alias}'. Available: {aliases}"
                + (" — or start a query with SELECT." if ALLOW_RAW_SAP_SQL else "")
            )

        return format_rows_for_lark(columns, rows, arg_text)

    except UnsafeQueryError as e:
        log.warning("Rejected unsafe SAP query: %s | input=%r", e, arg_text)
        return f"Query rejected: {e}"
    except dbapi.Error:
        log.exception("HANA error")
        return "Database temporarily unavailable. Please try again shortly."
    except Exception as e:
        log.exception("Unexpected error handling !Sap")
        return "SAP request could not be completed. Please try again shortly."

# ---------------------------------------------------------------------------
# Lark send-message helpers (Interactive Cards)
# ---------------------------------------------------------------------------

_tenant_token_cache = {"token": None, "expires_at": 0}

def get_tenant_access_token() -> str:
    if _tenant_token_cache["token"] and _tenant_token_cache["expires_at"] > time.time() + 60:
        return _tenant_token_cache["token"]

    resp = request_with_retry(
        "POST",
        f"{LARK_OPEN_API}/auth/v3/tenant_access_token/internal",
        json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to fetch tenant_access_token: {data}")

    _tenant_token_cache["token"] = data["tenant_access_token"]
    _tenant_token_cache["expires_at"] = time.time() + data.get("expire", 7200)
    return _tenant_token_cache["token"]

def send_interactive_card(chat_id: str, card_json: dict) -> str:
    token = get_tenant_access_token()
    resp = request_with_retry(
        "POST",
        f"{LARK_OPEN_API}/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card_json),
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("data", {}).get("message_id")

def update_interactive_card(message_id: str, card_json: dict):
    token = get_tenant_access_token()
    resp = request_with_retry(
        "PUT",
        f"{LARK_OPEN_API}/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "msg_type": "interactive",
            "content": json.dumps(card_json),
        },
        timeout=10,
    )
    resp.raise_for_status()

def send_lark_message(chat_id: str, text: str, receive_id_type: str = "chat_id"):
    token = get_tenant_access_token()
    resp = request_with_retry(
        "POST",
        f"{LARK_OPEN_API}/im/v1/messages?receive_id_type={receive_id_type}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------------------
# Command router & Webhook handling
# ---------------------------------------------------------------------------

COMMAND_PATTERN = re.compile(r"^\s*!(Group|Lark|Sap)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)

def extract_command(raw_text: str):
    text = raw_text.strip()
    text = re.sub(r"^@\S+\s*", "", text).strip()
    if not text:
        return None, None

    match = COMMAND_PATTERN.match(text)
    if not match:
        return None, None

    command = match.group(1).capitalize()
    remainder = match.group(2).strip()
    return command, remainder

def handle_message_event(event: dict):
    message = event.get("message", {})
    chat_id = message.get("chat_id")

    content = message.get("content", "{}")
    try:
        content_obj = json.loads(content)
    except json.JSONDecodeError:
        content_obj = {}
    raw_text = content_obj.get("text", "")

    command, remainder = extract_command(raw_text)
    if command is None:
        return

    if command == "Sap":
        loading_card = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "⏳ Querying SAP Business One database... Please stand by."}}]
        }
        msg_id = send_interactive_card(chat_id, loading_card) if chat_id else None

        try:
            reply = handle_sap_command(remainder)
        except Exception:
            reply = "SAP request could not be completed. Please try again shortly."

        if chat_id and msg_id:
            final_card = {
                "config": {"wide_screen_mode": True},
                "elements": [{"tag": "markdown", "content": reply}]
            }
            update_interactive_card(msg_id, final_card)
        elif chat_id:
            send_lark_message(chat_id, reply)

    elif command == "Group":
        reply = handle_group_command(remainder, event)
        if chat_id:
            send_lark_message(chat_id, reply)
    elif command == "Lark":
        reply = handle_lark_command(remainder, event)
        if chat_id:
            send_lark_message(chat_id, reply)

def handle_group_command(remainder: str, event: dict) -> str:
    token = get_tenant_access_token()
    resp = request_with_retry(
        "GET",
        f"{LARK_OPEN_API}/im/v1/chats",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        return f"Lark API Error: {data.get('msg')}"

    items = data.get("data", {}).get("items", [])
    if not items:
        return "I am not currently a member of any group chats."

    lines = ["Groups I'm in:"]
    for chat in items:
        lines.append(f"- {chat.get('name')} (chat_id: {chat.get('chat_id')})")
    return "\n".join(lines)

def handle_lark_command(remainder: str, event: dict) -> str:
    return f"Lark command received. args={remainder!r}"

def process_webhook(payload: dict):
    header = payload.get("header", {})
    event_type = header.get("event_type")

    if event_type != "im.message.receive_v1":
        return

    event = payload.get("event", {})
    try:
        handle_message_event(event)
    except Exception:
        log.exception("Failed to handle message event")

if __name__ == "__main__":
    import sys
    query_arg = " ".join(sys.argv[1:]) or "OCRD"
    print(handle_sap_command(query_arg))