import os
import re
import sys
import json
import time
import socket
import logging
from collections import defaultdict
from typing import Any, Dict, Optional

import lark_oapi as lark
from lark_oapi.ws import Client
from hdbcli import dbapi
from dotenv import load_dotenv

# ==========================================
# 1. Load Environment Variables & Config
# ==========================================
load_dotenv("lark-hana.env", override=True)


# Tracks chats currently processing a heavy command to block retries
_active_chat_locks = set()


# ==========================================
# LLM Provider Abstraction Layer
# ==========================================
class LLMError(Exception):
    """Base exception for LLM operations."""
    pass


class QuotaExhaustedError(LLMError):
    """Raised when LLM quota/rate limit is exhausted."""
    pass


class ModelNotFoundError(LLMError):
    """Raised when a model doesn't exist or was deprecated."""
    pass


def _get_client(provider: str) -> Any:
    """
    Lazy client factory: instantiate and cache LLM SDK clients.
    Only active providers are initialized on first use.
    
    Args:
        provider: LLM provider name (groq, gemini, anthropic, openai)
    
    Returns:
        Initialized SDK client for the provider
    
    Raises:
        ValueError: If provider is not supported
    """
    provider = provider.lower()
    
    # Return cached client if available
    if provider in _llm_clients:
        return _llm_clients[provider]
    
    if provider == "groq":
        from groq import Groq
        client = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=LLM_REQUEST_TIMEOUT)
        _llm_clients[provider] = client
        return client
    
    elif provider == "gemini":
        import google.generativeai as genai
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        # Return generativeai module directly (has GenerativeModel factory)
        _llm_clients[provider] = genai
        return genai
    
    elif provider == "anthropic":
        from anthropic import Anthropic
        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), timeout=LLM_REQUEST_TIMEOUT)
        _llm_clients[provider] = client
        return client
    
    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=LLM_REQUEST_TIMEOUT)
        _llm_clients[provider] = client
        return client
    
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}. Supported: groq, gemini, anthropic, openai")


def _strip_json_fence(text: str) -> str:
    """Remove markdown code fences from JSON responses."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _llm_call(provider: str, model: str, prompt: str, is_json: bool = False, max_retries: int = 3) -> str:
    """
    Unified LLM execution wrapper across all supported providers.
    Handles provider-specific API calls, JSON formatting, and error recovery.
    
    Args:
        provider: LLM provider name (groq, gemini, anthropic, openai)
        model: Model identifier specific to the provider
        prompt: The prompt to send to the LLM
        is_json: Whether to request JSON-formatted output
        max_retries: Maximum retry attempts for rate limits
    
    Returns:
        Plain text response from the LLM
    
    Raises:
        ModelNotFoundError: If model doesn't exist or was deprecated
        QuotaExhaustedError: If quota/rate limit exhausted after retries
        RuntimeError: For other unrecoverable errors
    """
    provider = provider.lower()
    attempts = 0
    
    while attempts < max_retries:
        try:
            if provider == "groq":
                client = _get_client("groq")
                response = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model=model,
                    response_format={"type": "json_object"} if is_json else None,
                    temperature=0.7
                )
                text = response.choices[0].message.content
                if is_json:
                    text = _strip_json_fence(text)
                return text
            
            elif provider == "gemini":
                genai = _get_client("gemini")
                model_instance = genai.GenerativeModel(model)
                config = {}
                if is_json:
                    config["response_mime_type"] = "application/json"
                response = model_instance.generate_content(prompt, generation_config=config)
                text = response.text
                if is_json:
                    text = _strip_json_fence(text)
                return text
            
            elif provider == "anthropic":
                client = _get_client("anthropic")
                response = client.messages.create(
                    model=model,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}]
                )
                text = response.content[0].text
                if is_json:
                    text = _strip_json_fence(text)
                return text
            
            elif provider == "openai":
                client = _get_client("openai")
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    response_format={"type": "json_object"} if is_json else None
                )
                text = response.choices[0].message.content
                if is_json:
                    text = _strip_json_fence(text)
                return text
            
            else:
                raise ValueError(f"Unsupported LLM provider: {provider}")
        
        except Exception as e:
            error_msg = str(e)
            
            # Detect model not found / deprecation errors
            if "model_not_found" in error_msg.lower() or "does not exist" in error_msg.lower():
                print(f"!!! LLM Model Error: {error_msg} !!!")
                audit_log.error(f"LLM Model Deprecation/NotFound Error [{provider}/{model}]: {error_msg}")
                raise ModelNotFoundError(error_msg)
            
            # Detect rate limit / quota errors (429, quota exceeded, etc.)
            if "429" in error_msg or "quota" in error_msg.lower() or "rate_limit" in error_msg.lower():
                attempts += 1
                if attempts >= max_retries:
                    print(f"--- MAX RETRIES REACHED: LLM {provider} quota/rate limit exhausted ---")
                    audit_log.error(f"LLM quota exhausted [{provider}/{model}] after {max_retries} retries")
                    raise QuotaExhaustedError(f"{provider} quota/rate limit exhausted")
                
                wait_time = 10 * attempts  # Exponential backoff
                print(f"--- RATE LIMIT HIT (Attempt {attempts}/{max_retries}): Waiting {wait_time}s... ---")
                time.sleep(wait_time)
                continue
            
            # For other errors, attempt retries with backoff
            attempts += 1
            if attempts >= max_retries:
                print(f"!!! LLM Error [{provider}/{model}] after {max_retries} attempts: {error_msg} !!!")
                audit_log.error(f"LLM Error [{provider}/{model}]: {error_msg}")
                raise RuntimeError(f"LLM call failed after {max_retries} retries: {error_msg}")
            
            wait_time = 5 * attempts
            print(f"--- Transient LLM Error (Attempt {attempts}/{max_retries}): Waiting {wait_time}s... ---")
            time.sleep(wait_time)
    
    raise RuntimeError("LLM call failed after all retries")


def _call_router(prompt: str, is_json: bool = False) -> str:
    """
    Call the ROUTER LLM (fast intent classification for !ask command).
    Uses ROUTER_LLM_PROVIDER and ROUTER_LLM_MODEL from environment.
    
    Args:
        prompt: The prompt to send
        is_json: Whether to request JSON output
    
    Returns:
        Plain text response from the router LLM
    
    Raises:
        ModelNotFoundError, QuotaExhaustedError, RuntimeError
    """
    return _llm_call(ROUTER_LLM_PROVIDER, ROUTER_LLM_MODEL, prompt, is_json=is_json)


def _call_analyst(prompt: str, is_json: bool = False) -> str:
    """
    Call the ANALYST LLM (heavy report generation & summarization).
    Uses ANALYST_LLM_PROVIDER and ANALYST_LLM_MODEL from environment.
    
    Args:
        prompt: The prompt to send
        is_json: Whether to request JSON output
    
    Returns:
        Plain text response from the analyst LLM
    
    Raises:
        ModelNotFoundError, QuotaExhaustedError, RuntimeError
    """
    return _llm_call(ANALYST_LLM_PROVIDER, ANALYST_LLM_MODEL, prompt, is_json=is_json)


APP_ID = os.getenv("LARK_APP_ID")
APP_SECRET = os.getenv("LARK_APP_SECRET")

# SAP Settings and Constraints
MAX_ROWS = int(os.getenv("SAP_MAX_ROWS", 50))
ALLOW_RAW_SQL = os.getenv("ALLOW_RAW_SAP_SQL", "false").lower() == "true"

if ALLOW_RAW_SQL:
    raise RuntimeError(
        "ALLOW_RAW_SAP_SQL must remain false — HANASA has write access and "
        "this bot relies on app-layer read-only enforcement, not DB grants."
    )

# SAP HANA Connection Dictionary
HANA_CONFIG = {
    "address": os.getenv("SAP_HOST"),
    "port": int(os.getenv("SAP_PORT", 30015)),
    "user": os.getenv("SAP_USER"),
    "password": os.getenv("SAP_PASSWORD"),
    "currentSchema": os.getenv("SAP_SCHEMA"),
    "connectTimeout": int(os.getenv("SAP_CONNECT_TIMEOUT", 10000)),
    "encrypt": True,
    "sslValidateCertificate": True,
    "sslHostNameInCertificate": os.getenv("SAP_SSL_HOSTNAME", "DIRECHANASERVER")
}

# ==========================================
# 1a. LLM Provider Configuration (Role-Based)
# ==========================================
# ROUTER: Fast intent classification (NLU parsing for !ask command)
ROUTER_LLM_PROVIDER = os.getenv("ROUTER_LLM_PROVIDER", "groq").lower()
ROUTER_LLM_MODEL = os.getenv("ROUTER_LLM_MODEL", "openai/gpt-oss-20b")

# ANALYST: Heavy report generation & summarization
ANALYST_LLM_PROVIDER = os.getenv("ANALYST_LLM_PROVIDER", "gemini").lower()
ANALYST_LLM_MODEL = os.getenv("ANALYST_LLM_MODEL", "gemini-3.6-flash")

# Request timeout for all LLM SDK clients (seconds)
LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", 60))

# Lazy client cache (only instantiate active providers)
_llm_clients: Dict[str, Any] = {}

# ==========================================
# 1b. Audit Logging & Safeguards
# ==========================================
logging.basicConfig(filename="bot_audit.log", level=logging.INFO, format="%(asctime)s | %(message)s")
audit_log = logging.getLogger("audit")

_failure_state = {"count": 0, "locked_until": 0.0}
FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 300 

def _check_circuit_breaker():
    now = time.time()
    if now < _failure_state["locked_until"]:
        remaining = int(_failure_state["locked_until"] - now)
        raise RuntimeError(f"Too many recent database failures. Cooling down for {remaining}s.")

def _record_db_failure():
    _failure_state["count"] += 1
    if _failure_state["count"] >= FAILURE_THRESHOLD:
        _failure_state["locked_until"] = time.time() + COOLDOWN_SECONDS
        _failure_state["count"] = 0
        audit_log.info(f"CIRCUIT BREAKER TRIPPED - cooling down for {COOLDOWN_SECONDS}s")

def _record_db_success():
    _failure_state["count"] = 0

_last_command_time = defaultdict(float)
MIN_SECONDS_BETWEEN_COMMANDS = 3

def is_rate_limited(user_id):
    now = time.time()
    if now - _last_command_time[user_id] < MIN_SECONDS_BETWEEN_COMMANDS:
        return True
    _last_command_time[user_id] = now
    return False

ITEM_CODE_PATTERN = re.compile(r'^[A-Za-z0-9\-_.]{1,30}$')
def is_valid_item_code(item_code):
    return bool(ITEM_CODE_PATTERN.match(item_code))

COMMANDS = [
    {"usage": "!ask <question>", "description": "Ask a natural language question about sales, stock, clients, or overdue invoices."},
    {"usage": "!report", "description": "Generate an AI executive operational summary from recent SAP invoices."},
    {"usage": "!sales [today|week|month]", "description": "Generate an AI summary of sales velocity and revenue trends over a timeframe."},
    {"usage": "!topclients [limit]", "description": "Analyze top revenue-generating clients and account concentration risk."},
    {"usage": "!aging", "description": "Generate an AI overdue receivable aging report and collections action plan."},
    {"usage": "!stock <ItemCode>", "description": "Look up current on-hand stock for an exact item code."},
    {"usage": "!help", "description": "Show this list of available commands."}
]

def get_help_text():
    lines = ["Here's what I can do:", ""]
    for cmd in COMMANDS:
        lines.append(f"- **`{cmd['usage']}`** — {cmd['description']}")
    return "\n".join(lines)

_seen_message_ids = {}
DEDUP_WINDOW_SECONDS = 600

def _is_duplicate_event(message_id):
    now = time.time()
    stale = [mid for mid, ts in _seen_message_ids.items() if now - ts > DEDUP_WINDOW_SECONDS]
    for mid in stale: del _seen_message_ids[mid]
    if message_id in _seen_message_ids: return True
    _seen_message_ids[message_id] = now
    return False

_FORBIDDEN_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "MERGE", "CREATE", "EXEC", "EXECUTE", "CALL", "GRANT", "REVOKE")

def _enforce_read_only(sql: str):
    normalized = " ".join(sql.strip().upper().split())
    if not normalized.startswith("SELECT"):
        raise PermissionError(f"Blocked non-SELECT statement: {sql[:50]}...")
    padded = f" {normalized} "
    for kw in _FORBIDDEN_KEYWORDS:
        if f" {kw} " in padded:
            raise PermissionError(f"Blocked statement containing forbidden keyword '{kw}': {sql[:50]}...")

def _set_session_read_only(cursor):
    cursor.execute("SET TRANSACTION READ ONLY")

def _safe_execute(cursor, sql, params=None):
    _enforce_read_only(sql)
    if params is not None: cursor.execute(sql, params)
    else: cursor.execute(sql)


# ==========================================
# 2. SAP HANA Query Functions
# ==========================================

def query_sap_hana(item_code):
    _check_circuit_breaker()
    conn = None
    cursor = None
    try:
        time.sleep(0.5)
        conn = dbapi.connect(**HANA_CONFIG)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        sql = 'SELECT "ItemCode", "ItemName", "OnHand" FROM "OITM" WHERE "ItemCode" = ?'
        _safe_execute(cursor, sql, (item_code,))
        rows = cursor.fetchmany(MAX_ROWS)
        _record_db_success()
        if not rows: return f"No records found for item code: {item_code}"
        table_lines = [f"Found **{len(rows)}** record(s) for `{item_code}`:", "", "| Item Code | Item Name | On Hand |", "| :--- | :--- | ---: |"]
        for row in rows: table_lines.append(f"| {row[0]} | {row[1]} | {row[2]} |")
        return "\n".join(table_lines)
    except Exception as e:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def query_sap_invoices_for_llm():
    _check_circuit_breaker()
    conn = None
    cursor = None
    try:
        time.sleep(0.5)
        conn = dbapi.connect(**HANA_CONFIG)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        sql = 'SELECT "DocNum", "CardName", "DocDate", "DocTotal" FROM "OINV" ORDER BY "DocDate" DESC'
        _safe_execute(cursor, sql)
        rows = cursor.fetchmany(MAX_ROWS)
        _record_db_success()
        if not rows: return "No invoice data found."
        raw_data = "DocNum | Client Name | Date | Total Amount\n"
        for row in rows: raw_data += f"{row[0]} | {row[1]} | {row[2]} | {row[3]}\n"
        return raw_data
    except Exception as e:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def query_sap_sales_by_period(period="month", start_date=None, end_date=None):
    _check_circuit_breaker()
    conn = None
    cursor = None
    try:
        time.sleep(0.5)
        conn = dbapi.connect(**HANA_CONFIG)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        
        period = period.lower()
        params = None
        
        if period == "today":
            sql = 'SELECT "DocNum", "CardName", "DocDate", "DocTotal" FROM "OINV" WHERE "DocDate" = CURRENT_DATE ORDER BY "DocTotal" DESC'
        elif period == "week":
            sql = 'SELECT "DocNum", "CardName", "DocDate", "DocTotal" FROM "OINV" WHERE "DocDate" >= ADD_DAYS(CURRENT_DATE, -7) ORDER BY "DocDate" DESC'
        elif period == "custom" and start_date and end_date:
            sql = 'SELECT "DocNum", "CardName", "DocDate", "DocTotal" FROM "OINV" WHERE "DocDate" BETWEEN ? AND ? ORDER BY "DocDate" DESC'
            params = (start_date, end_date)
        else:
            period = "month"
            sql = 'SELECT "DocNum", "CardName", "DocDate", "DocTotal" FROM "OINV" WHERE "DocDate" >= ADD_DAYS(CURRENT_DATE, -30) ORDER BY "DocDate" DESC'
        
        _safe_execute(cursor, sql, params)
        rows = cursor.fetchmany(MAX_ROWS)
        _record_db_success()
        
        display_period = f"{start_date} to {end_date}" if period == "custom" else period.upper()
        if not rows: return f"No sales invoice data found for period: {display_period}."
        
        raw_data = f"Period: {display_period}\nDocNum | Client Name | Date | Total Amount\n"
        for row in rows: raw_data += f"{row[0]} | {row[1]} | {row[2]} | {row[3]}\n"
        return raw_data
        
    except Exception as e:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def query_sap_top_clients(limit=5):
    _check_circuit_breaker()
    conn = None
    cursor = None
    try:
        time.sleep(0.5)
        conn = dbapi.connect(**HANA_CONFIG)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        sql = 'SELECT "CardName", SUM("DocTotal") AS "TotalRev", COUNT("DocNum") AS "InvCount" FROM "OINV" GROUP BY "CardName" ORDER BY "TotalRev" DESC'
        _safe_execute(cursor, sql)
        rows = cursor.fetchmany(limit)
        _record_db_success()
        if not rows: return "No client revenue data found."
        raw_data = "Client Name | Total Revenue | Invoices Count\n"
        for row in rows: raw_data += f"{row[0]} | {row[1]} | {row[2]}\n"
        return raw_data
    except Exception as e:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def query_sap_aging_invoices():
    _check_circuit_breaker()
    conn = None
    cursor = None
    try:
        time.sleep(0.5)
        conn = dbapi.connect(**HANA_CONFIG)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        sql = 'SELECT "DocNum", "CardName", "DocDate", "DocDueDate", "DocTotal" FROM "OINV" WHERE "DocStatus" = \'O\' AND "DocDueDate" < CURRENT_DATE ORDER BY "DocDueDate" ASC'
        _safe_execute(cursor, sql)
        rows = cursor.fetchmany(MAX_ROWS)
        _record_db_success()
        if not rows: return "No overdue open invoices found in the system."
        raw_data = "DocNum | Client Name | Doc Date | Due Date | Outstanding Amount\n"
        for row in rows: raw_data += f"{row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]}\n"
        return raw_data
    except Exception as e:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


# ==========================================
# 3. LLM Reporting & Analytics Functions
# ==========================================

def generate_llm_report(raw_sap_data):
    if "Sorry" in raw_sap_data or "No invoice" in raw_sap_data: 
        return raw_sap_data
    prompt = f"""
    You are an expert corporate financial analyst. I am providing you with recent raw invoice data directly from our SAP HANA database.
    Please analyze this data and generate a concise "Executive Operational Summary" for the current period. Structure the report with exactly these two parts:
    1. A Markdown table titled "Period Performance Overview" with ONLY two columns: Metric | Value. Keep the values clean and direct.
    2. A comprehensive "Detailed Metrics & Executive Insights" section written as a bullet list, NOT a table. Each bullet must start with a short **bold label** followed by a colon and the full explanation.
    Do not use '#' Markdown headers.
    Raw SAP data:
    {raw_sap_data}
    """
    try:
        response_text = _call_analyst(prompt)
        return f"{response_text.strip()}\n\n_Generated by DEAi from live SAP HANA data._"
    except QuotaExhaustedError:
        return "Sorry, the AI is currently at capacity. Please try again in a few moments."
    except ModelNotFoundError:
        return "Sorry, the analyst model is unavailable. Please contact the bot admin."
    except Exception as e:
        return "Sorry, the AI analysis step failed. Please try again shortly."

def generate_llm_sales_report(raw_data, period):
    if "Sorry" in raw_data or "No sales" in raw_data: 
        return raw_data
    prompt = f"""
    You are a corporate sales analyst. Analyze this SAP HANA sales data for the period: '{period.upper()}'.
    Format the response strictly with:
    1. A Markdown table titled "Sales Performance ({period.capitalize()})" with ONLY two columns: Metric | Value.
    2. An "Executive Sales Velocity" section written as 3-4 bullet points with **bold labels**.
    Do not use '#' headers.
    Raw Data:
    {raw_data}
    """
    try:
        response_text = _call_analyst(prompt)
        return f"{response_text.strip()}\n\n_Generated by DEAi from live SAP HANA data._"
    except QuotaExhaustedError:
        return "Sorry, the AI is currently at capacity. Please try again in a few moments."
    except ModelNotFoundError:
        return "Sorry, the analyst model is unavailable. Please contact the bot admin."
    except Exception as e:
        return "Sorry, the AI analysis step failed. Please try again shortly."

def generate_llm_top_clients_report(raw_data, limit):
    if "Sorry" in raw_data or "No client" in raw_data: 
        return raw_data
    prompt = f"""
    You are a corporate financial analyst. Analyze the following top {limit} revenue-generating accounts from SAP HANA.
    Format the response strictly with:
    1. A Markdown table titled "Top {limit} Accounts by Revenue" with columns: Rank | Client Name | Total Revenue | Invoices.
    2. An "Account Concentration & Insights" section written as 3-4 bullet points with **bold labels**.
    Do not use '#' headers.
    Raw Data:
    {raw_data}
    """
    try:
        response_text = _call_analyst(prompt)
        return f"{response_text.strip()}\n\n_Generated by DEAi from live SAP HANA data._"
    except QuotaExhaustedError:
        return "Sorry, the AI is currently at capacity. Please try again in a few moments."
    except ModelNotFoundError:
        return "Sorry, the analyst model is unavailable. Please contact the bot admin."
    except Exception as e:
        return "Sorry, the AI analysis step failed. Please try again shortly."

def generate_llm_aging_report(raw_data):
    if "Sorry" in raw_data or "No overdue" in raw_data: 
        return raw_data
    prompt = f"""
    You are a corporate credit & collections controller. Analyze these overdue open SAP invoices.
    Format the response strictly with:
    1. A Markdown table titled "Receivables Aging Summary" with ONLY two columns: Aging Category | Outstanding Total.
    2. A "Collections Risk & Priority Action" section written as 3-4 bullet points with **bold labels**.
    Do not use '#' headers.
    Raw Data:
    {raw_data}
    """
    try:
        response_text = _call_analyst(prompt)
        return f"{response_text.strip()}\n\n_Generated by DEAi from live SAP HANA data._"
    except QuotaExhaustedError:
        return "Sorry, the AI is currently at capacity. Please try again in a few moments."
    except ModelNotFoundError:
        return "Sorry, the analyst model is unavailable. Please contact the bot admin."
    except Exception as e:
        return "Sorry, the AI analysis step failed. Please try again shortly."

def _parse_natural_language(user_query):
    """Uses the ROUTER LLM to instantly extract intent and parameters from natural language queries."""
    prompt = f"""
    You are an intelligent router for an SAP HANA bot. Analyze the user's request and map it to a specific database action.
    
    Available actions:
    - "stock": user wants to check inventory. Extract 'item_code'.
    - "sales": user wants a sales summary. Extract 'period' (today, week, month, or custom). 
      If the user specifies a specific month, year, or date range (like "January 2026"), set period to "custom" and extract 'start_date' and 'end_date' in exactly "YYYY-MM-DD" format.
    - "top_clients": user wants to see top accounts. Extract 'limit' (number).
    - "aging": user wants overdue/aging invoices.
    - "report": user wants a general executive report.
    - "unknown": cannot determine action.
    
    Respond ONLY with a valid, raw JSON object.
    Format Example: {{"action": "sales", "params": {{"period": "custom", "start_date": "2026-01-01", "end_date": "2026-01-31"}}}}
    
    User Query: "{user_query}"
    """
    try:
        raw_json = _call_router(prompt, is_json=True)
        print(f"--- ROUTER LLM RAW OUTPUT ---\n{raw_json}\n---------------------------")
        return json.loads(raw_json)
        
    except ModelNotFoundError as e:
        error_msg = str(e)
        print(f"!!! NLP Router Model Error: {error_msg} !!!") 
        audit_log.error(f"NLP Router Model Deprecation: {error_msg}")
        return {"action": "unknown", "params": {}, "model_error": True}
    
    except QuotaExhaustedError as e:
        print(f"!!! NLP Router Quota Exhausted: {str(e)} !!!")
        audit_log.error(f"NLP Router Quota Exhausted: {str(e)}")
        return {"action": "unknown", "params": {}, "quota_error": True}
    
    except Exception as e:
        error_msg = str(e)
        print(f"!!! NLP Parsing Error: {error_msg} !!!")
        audit_log.error(f"NLP Parsing Error: {error_msg}")
        return {"action": "unknown", "params": {}}

def generate_conversational_reply(raw_data, user_query):
    if "Sorry" in raw_data or "No " in raw_data: 
        return raw_data
    prompt = f"""
    You are a helpful, professional AI assistant. The user asked you this question: "{user_query}"
    Here is the raw data retrieved from our SAP HANA database to answer it:
    {raw_data}
    Write a natural, conversational, and direct answer to the user's question using this data. You may use a Markdown table if needed. Do not use '#' headers.
    """
    try:
        response_text = _call_analyst(prompt)
        return f"{response_text.strip()}\n\n_Answered by DEAi using live SAP HANA data._"
    except QuotaExhaustedError:
        return "Sorry, the AI is currently at capacity. Please try again in a few moments."
    except ModelNotFoundError:
        return "Sorry, the analyst model is unavailable. Please contact the bot admin."
    except Exception as e:
        return "Sorry, I pulled the data but couldn't format the response. Please try again."


# ==========================================
# 4. Lark WebSocket Message Handler
# ==========================================

def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    event = data.event
    message = event.message
    chat_id = message.chat_id
    message_id = message.message_id
    sender_id = event.sender.sender_id.open_id if event.sender and event.sender.sender_id else "unknown"

    if message_id and _is_duplicate_event(message_id): return
    if chat_id in _active_chat_locks: return

    content_json = json.loads(message.content)
    user_text = content_json.get("text", "").strip()

    print(f"\nReceived command: {user_text}")

    if is_rate_limited(sender_id):
        _send_lark_reply(message_id, "You're sending commands too quickly. Please wait a few seconds and try again.")
        return

    try:
        _check_circuit_breaker()
    except RuntimeError as e:
        _send_lark_reply(message_id, str(e))
        return

    _active_chat_locks.add(chat_id)
    try:
        # ---> FIRE LOADING MESSAGE HERE <---
        _send_lark_text(message_id, "⏳ _Analyzing SAP records, please wait a moment..._")
        
        card_title = "SAP HANA Bot"
        
        if user_text.startswith("!stock"):
            card_title = "Stock Lookup"
            parts = user_text.split()
            if len(parts) > 1:
                item_code = parts[1]
                if not is_valid_item_code(item_code): reply_text = "Invalid item code format."
                else:
                    print(f"Querying SAP HANA for item: {item_code}...")
                    reply_text = query_sap_hana(item_code)
            else: reply_text = "Please specify an item code. Example: `!stock 19-LAP-0001`"

        elif user_text.startswith("!sales"):
            card_title = "Sales Velocity Summary"
            parts = user_text.split()
            period = parts[1] if len(parts) > 1 and parts[1].lower() in ["today", "week", "month"] else "month"
            print(f"Fetching sales data for period: {period}...")
            raw_data = query_sap_sales_by_period(period)
            reply_text = generate_llm_sales_report(raw_data, period)

        elif user_text.startswith("!topclients"):
            card_title = "Top Accounts Analysis"
            parts = user_text.split()
            limit = 5
            if len(parts) > 1 and parts[1].isdigit(): limit = min(int(parts[1]), 20)
            print(f"Fetching top {limit} clients...")
            raw_data = query_sap_top_clients(limit)
            reply_text = generate_llm_top_clients_report(raw_data, limit)

        elif user_text.startswith("!aging"):
            card_title = "Overdue Receivables Aging"
            print("Fetching overdue open invoices...")
            raw_data = query_sap_aging_invoices()
            reply_text = generate_llm_aging_report(raw_data)

        elif user_text.startswith("!report"):
            card_title = "SAP Executive Report"
            print("Fetching latest SAP Invoice Data...")
            raw_data = query_sap_invoices_for_llm()
            print("Passing data to Gemini LLM for analysis...")
            reply_text = generate_llm_report(raw_data)

        elif user_text.startswith("!ask"):
            card_title = "AI Assistant"
            query = user_text[4:].strip()
            if not query:
                reply_text = "What would you like to ask? (e.g., `!ask what are our top 5 clients?`)"
            else:
                print(f"Parsing natural language: {query}")
                nlp_result = _parse_natural_language(query)
                action = nlp_result.get("action")
                params = nlp_result.get("params", {})
                model_error = nlp_result.get("model_error", False)
                quota_error = nlp_result.get("quota_error", False)
                
                if model_error:
                    reply_text = "⚠️ **AI Routing Model Unavailable:** The natural language router needs to be updated — please contact the bot admin."
                    raw_data = None
                elif quota_error:
                    reply_text = "⚠️ **AI Router Quota Exhausted:** The NLP router is temporarily unavailable due to quota limits. Please try again in a moment."
                    raw_data = None
                else:
                    raw_data = ""
                    if action == "stock":
                        item_code = params.get("item_code", "")
                        raw_data = query_sap_hana(item_code) if item_code else "I couldn't identify a specific item code in your question."
                    elif action == "sales": 
                        raw_data = query_sap_sales_by_period(
                            period=params.get("period", "month"),
                            start_date=params.get("start_date"),
                            end_date=params.get("end_date")
                        )
                    elif action == "top_clients": raw_data = query_sap_top_clients(params.get("limit", 5))
                    elif action == "aging": raw_data = query_sap_aging_invoices()
                    elif action == "report": raw_data = query_sap_invoices_for_llm()
                    elif action == "rate_limit":
                        reply_text = "⚠️ **AI Quota Exhausted:** The AI is receiving too many requests right now or has reached its daily limit. Please try again later."
                        raw_data = None
                    else:
                        reply_text = "I'm not sure how to look that up yet. Try asking about stock, sales, top clients, or overdue invoices."
                        raw_data = None
                    
                    if raw_data:
                        print("Generating conversational reply...")
                        reply_text = generate_conversational_reply(raw_data, query)

        elif user_text.startswith("!help"):
            card_title = "Available Commands"
            reply_text = get_help_text()

        else:
            reply_text = "Unknown command. Type `!help` to see everything I can do."

        _send_lark_reply(message_id, reply_text, title=card_title)

    finally:
        _active_chat_locks.discard(chat_id)


# --- Lark Message Formatting Helpers ---
_TABLE_ROW_RE = re.compile(r'^\s*\|(.+)\|\s*$')
_TABLE_SEP_CELL_RE = re.compile(r'^:?-{1,}:?$')

def _split_row(line): return [cell.strip() for cell in line.strip().strip('|').split('|')]
def _is_separator_row(cells): return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells)

def _md_inline_to_lark(text):
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            lines.append(f"**{heading}**")
        else:
            lines.append(line)
    return "\n".join(lines)

def _parse_markdown_segments(reply_text):
    lines = reply_text.split("\n")
    segments = []
    text_buffer = []
    i = 0
    def flush_text():
        if text_buffer:
            joined = "\n".join(text_buffer).strip("\n")
            if joined.strip(): segments.append(("text", _md_inline_to_lark(joined)))
            text_buffer.clear()

    while i < len(lines):
        line = lines[i]
        match = _TABLE_ROW_RE.match(line)
        if match:
            header_cells = _split_row(line)
            if i + 1 < len(lines) and _TABLE_ROW_RE.match(lines[i + 1]) and _is_separator_row(_split_row(lines[i + 1])):
                flush_text()
                j = i + 2
                data_rows = []
                while j < len(lines) and _TABLE_ROW_RE.match(lines[j]):
                    data_rows.append(_split_row(lines[j]))
                    j += 1
                columns = header_cells
                rows = []
                for data_row in data_rows:
                    row = {}
                    for col_idx in range(len(columns)):
                        row[f"col_{col_idx}"] = data_row[col_idx] if col_idx < len(data_row) else ""
                    rows.append(row)
                segments.append(("table", columns, rows))
                i = j
                continue
        text_buffer.append(line)
        i += 1
    flush_text()
    return segments

def _build_card(reply_text, title="SAP HANA Bot"):
    segments = _parse_markdown_segments(reply_text)
    elements = []
    
    for segment in segments:
        if segment[0] == "text":
            elements.append({"tag": "markdown", "content": segment[1]})
        else:
            _, columns, rows = segment
            
            # --- 1. NEW CHART INJECTION ---
            chart_spec = _build_vchart_spec(columns, rows)
            if chart_spec:
                elements.append({
                    "tag": "chart",
                    "aspect_ratio": "16:9",
                    "chart_spec": chart_spec
                })
            
            # --- 2. EXISTING TABLE INJECTION ---
            elements.append({
                "tag": "table", "page_size": 10, "row_height": "low",
                "columns": [{"name": f"col_{idx}", "display_name": name, "data_type": "text", "width": "auto"} for idx, name in enumerate(columns)],
                "rows": rows
            })
            
    if not elements: 
        elements = [{"tag": "markdown", "content": reply_text or " "}]
    
    return {
        "schema": "2.0", 
        "config": {"wide_screen_mode": True}, 
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"}, 
        "body": {"elements": elements}
    }

def _build_vchart_spec(columns, rows):
    """Automatically converts an extracted Markdown table into a Lark VChart JSON spec."""
    if len(columns) < 2 or not rows:
        return None
    
    # 1. Pick the X-Axis (Label): First text column, skipping "Rank" or "ID"
    label_col_idx = 0
    for idx, col in enumerate(columns):
        if col.lower().strip() not in ["rank", "id", "no.", "#"]:
            label_col_idx = idx
            break
            
    label_col_key = f"col_{label_col_idx}"
    label_col_name = columns[label_col_idx][:20] # VChart prefers shorter field names
    
    # 2. Pick the Y-Axis (Value): First numeric column after the label
    value_col_idx = label_col_idx + 1 if label_col_idx + 1 < len(columns) else 1
    for idx in range(label_col_idx + 1, len(columns)):
        val = rows[0].get(f"col_{idx}", "")
        # Check if the column contains numbers
        if re.sub(r'[^\d.-]', '', val):
            value_col_idx = idx
            break
            
    value_col_key = f"col_{value_col_idx}"
    value_col_name = columns[value_col_idx][:20]
    
    # 3. Clean the data and map to chart values
    values = []
    for row in rows:
        label = row.get(label_col_key, "Unknown")
        val_str = row.get(value_col_key, "0")
        
        # Strip currency symbols ($) and commas so Python can plot it
        cleaned = re.sub(r'[^\d.-]', '', val_str)
        try:
            numeric_val = float(cleaned) if cleaned else 0
        except ValueError:
            numeric_val = 0
            
        # Truncate very long labels so the chart doesn't get squished
        if len(label) > 15:
            label = label[:13] + ".."
            
        values.append({label_col_name: label, value_col_name: numeric_val})
        
    # 4. Return the VChart JSON structure
    return {
        "type": "bar",
        "data": [{"id": "barData", "values": values}],
        "xField": label_col_name,
        "yField": value_col_name
    }

def _send_lark_reply(message_id, reply_text, title="SAP HANA Bot"):
    """Replies in-thread to the triggering message_id (shows as 'N replies' in Lark)."""
    client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).domain("https://open.larksuite.com").build()
    card = _build_card(reply_text, title=title)
    req = lark.im.v1.ReplyMessageRequest.builder().message_id(message_id).request_body(
        lark.im.v1.ReplyMessageRequestBody.builder().content(json.dumps(card)).msg_type("interactive").build()
    ).build()
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        print(f"!!! Failed to send Lark reply: code={resp.code} msg={resp.msg} log_id={resp.get_log_id()} !!!")
    else:
        print("Reply sent to Lark successfully!")

def _send_lark_text(message_id, text_content):
    """Sends a simple text reply to act as a loading indicator, threaded under the triggering message."""
    client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).domain("https://open.larksuite.com").build()

    req = lark.im.v1.ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(lark.im.v1.ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text_content}))
            .build()) \
        .build()

    resp = client.im.v1.message.reply(req)
    if not resp.success():
        print(f"!!! Failed to send Lark loading text: code={resp.code} msg={resp.msg} log_id={resp.get_log_id()} !!!")

# ==========================================
# 5. Main Execution
# ==========================================
_SINGLE_INSTANCE_LOCK_PORT = 47291 
_instance_lock_socket = None 

def _acquire_single_instance_lock():
    global _instance_lock_socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try: sock.bind(("127.0.0.1", _SINGLE_INSTANCE_LOCK_PORT))
    except OSError:
        print("Another instance of this bot is already running. Close it first.")
        sys.exit(1)
    _instance_lock_socket = sock

def _do_message_read_v1(data) -> None:
    pass

def main():
    _acquire_single_instance_lock()
    event_handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(do_p2_im_message_receive_v1).register_p2_customized_event("im.message.message_read_v1", _do_message_read_v1).build()
    ws_client = Client(APP_ID, APP_SECRET, event_handler=event_handler, domain="https://open.larksuite.com", log_level=lark.LogLevel.INFO)
    print("Starting direct Lark-to-SAP-HANA WebSocket bridge (with Gemini AI Integration)...")
    ws_client.start()

if __name__ == "__main__":
    main()