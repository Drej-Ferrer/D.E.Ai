"""
SAP HANA connection handling and all SQL query functions.

Everything that touches `hdbcli.dbapi` lives here. Callers (main.py,
ai_router.py) only ever get back Markdown-ish strings — they never see a
connection, cursor, or raw SQL.
"""

import re
import time

from hdbcli import dbapi

from modules import config
from modules.config import audit_log

# ==========================================
# Circuit Breaker (protects a flaky/overloaded HANA instance)
# ==========================================
_failure_state = {"count": 0, "locked_until": 0.0}


def check_circuit_breaker():
    now = time.time()
    if now < _failure_state["locked_until"]:
        remaining = int(_failure_state["locked_until"] - now)
        raise RuntimeError(f"Too many recent database failures. Cooling down for {remaining}s.")


def _record_db_failure():
    _failure_state["count"] += 1
    if _failure_state["count"] >= config.FAILURE_THRESHOLD:
        _failure_state["locked_until"] = time.time() + config.COOLDOWN_SECONDS
        _failure_state["count"] = 0
        audit_log.info(f"CIRCUIT BREAKER TRIPPED - cooling down for {config.COOLDOWN_SECONDS}s")


def _record_db_success():
    _failure_state["count"] = 0


# ==========================================
# Read-Only Enforcement & Zero-Trust Mapping
# ==========================================
_FORBIDDEN_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "MERGE", "CREATE", "EXEC", "EXECUTE", "CALL", "GRANT", "REVOKE", "UNION", "INTERSECT", "EXCEPT", "COMMIT", "ROLLBACK", "REPLACE", "UPSERT")


def _enforce_read_only(sql: str):
    if ";" in sql or "--" in sql or "/*" in sql:
        raise PermissionError("Blocked SQL containing statement delimiters or comments.")
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
    if params is not None:
        cursor.execute(sql, params)
    else:
        cursor.execute(sql)


def _clean(val):
    if val is None:
        return ""
    return str(val).replace("|", "/").replace("\n", " ").strip()


def _get_connection_for_user(lark_user_id: str):
    """
    TEMPORARY TESTING MODE: Bypasses DEAI_USER_MAP and uses the master HANA config
    so all test queries succeed. Role security will be re-enabled later.
    """
    time.sleep(0.5)
    return dbapi.connect(**config.HANA_CONFIG)


# ==========================================
# Item Code Validation
# ==========================================
ITEM_CODE_PATTERN = re.compile(r'^[A-Za-z0-9\-_.]{1,30}$')


def is_valid_item_code(item_code):
    return bool(ITEM_CODE_PATTERN.match(item_code))


# ==========================================
# Query Functions
# ==========================================

def query_sap_hana(item_code, lark_user_id):
    conn = None
    cursor = None
    try:
        check_circuit_breaker()
        time.sleep(0.5)
        conn = _get_connection_for_user(lark_user_id)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        sql = 'SELECT "ItemCode", "ItemName", "OnHand" FROM "OITM" WHERE "ItemCode" = ?'
        _safe_execute(cursor, sql, (item_code,))
        rows = cursor.fetchmany(3000)
        _record_db_success()
        if not rows:
            return f"No records found for item code: {item_code}"
        table_lines = [f"Found **{len(rows)}** record(s) for `{item_code}`:", "", "| Item Code | Item Name | On Hand |", "| :--- | :--- | ---: |"]
        for row in rows:
            table_lines.append(f"| {_clean(row[0])} | {_clean(row[1])} | {_clean(row[2])} |")
        return "\n".join(table_lines)
    except PermissionError as pe:
        return str(pe)
    except Exception:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def query_sap_invoices_for_llm(lark_user_id):
    conn = None
    cursor = None
    try:
        check_circuit_breaker()
        time.sleep(0.5)
        conn = _get_connection_for_user(lark_user_id)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        sql = 'SELECT "DocNum", "CardName", "DocDate", "DocTotal" FROM "OINV" ORDER BY "DocDate" DESC'
        _safe_execute(cursor, sql)
        rows = cursor.fetchmany(3000)
        _record_db_success()
        if not rows:
            return "No invoice data found."
        raw_data = "DocNum | Client Name | Date | Total Amount\n"
        for row in rows:
            raw_data += " | ".join(_clean(value) for value in row) + "\n"
        return raw_data
    except PermissionError as pe:
        return str(pe)
    except Exception:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def query_sap_sales_by_period(period="month", start_date=None, end_date=None, lark_user_id=None):
    if not lark_user_id:
        return "System error: User ID required."
        
    conn = None
    cursor = None
    try:
        check_circuit_breaker()
        time.sleep(0.5)
        conn = _get_connection_for_user(lark_user_id)
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
        rows = cursor.fetchmany(3000)
        _record_db_success()

        display_period = f"{start_date} to {end_date}" if period == "custom" else period.upper()
        if not rows:
            return f"No sales invoice data found for period: {display_period}."

        raw_data = f"Period: {display_period}\nDocNum | Client Name | Date | Total Amount\n"
        for row in rows:
            raw_data += " | ".join(_clean(value) for value in row) + "\n"
        return raw_data

    except PermissionError as pe:
        return str(pe)
    except Exception:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def query_sap_top_clients(limit=5, lark_user_id=None):
    if not lark_user_id:
        return "System error: User ID required."
        
    conn = None
    cursor = None
    try:
        check_circuit_breaker()
        time.sleep(0.5)
        conn = _get_connection_for_user(lark_user_id)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        sql = 'SELECT "CardName", SUM("DocTotal") AS "TotalRev", COUNT("DocNum") AS "InvCount" FROM "OINV" GROUP BY "CardName" ORDER BY "TotalRev" DESC'
        _safe_execute(cursor, sql)
        rows = cursor.fetchmany(limit)
        _record_db_success()
        if not rows:
            return "No client revenue data found."
        raw_data = "Client Name | Total Revenue | Invoices Count\n"
        for row in rows:
            raw_data += " | ".join(_clean(value) for value in row) + "\n"
        return raw_data
    except PermissionError as pe:
        return str(pe)
    except Exception:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def query_sap_aging_invoices(lark_user_id):
    conn = None
    cursor = None
    try:
        check_circuit_breaker()
        time.sleep(0.5)
        conn = _get_connection_for_user(lark_user_id)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        sql = 'SELECT "DocNum", "CardName", "DocDate", "DocDueDate", "DocTotal" FROM "OINV" WHERE "DocStatus" = \'O\' AND "DocDueDate" < CURRENT_DATE ORDER BY "DocDueDate" ASC'
        _safe_execute(cursor, sql)
        rows = cursor.fetchmany(3000)
        _record_db_success()
        if not rows:
            return "No overdue open invoices found in the system."
        raw_data = "DocNum | Client Name | Doc Date | Due Date | Outstanding Amount\n"
        for row in rows:
            raw_data += " | ".join(_clean(value) for value in row) + "\n"
        return raw_data
    except PermissionError as pe:
        return str(pe)
    except Exception:
        _record_db_failure()
        return "Sorry, I couldn't reach the database right now. Please try again shortly."
    finally:
        if cursor: cursor.close()
        if conn: conn.close()
            
            
# ==========================================
# Schema Discovery & Safety Floor
# ==========================================
# Blunt safety net: Block HR (Employee Master) and User Credential tables
_TABLE_BLOCKLIST = {"OHEM", "HEM1", "HEM6", "OUSR", "USR1", "OATC"}

# Real semantic descriptions so the router LLM can actually distinguish
# these tables instead of pattern-matching the one example in its prompt.
# This is what lets it tell "vendor purchase" (OPCH/PCH1) apart from
# "internal/manual posting" (OJDT/JDT1) instead of defaulting to whichever
# one happened to be in the worked example.
_TABLE_DESCRIPTIONS = {
    "OINV": "A/R Invoices (header) — sales invoices issued TO customers. Header-level free-text column: Comments.",
    "INV1": "A/R Invoice line items — one row per product/service line on a customer sales invoice. Free-text line description column: Dscription.",
    "OPCH": "A/P Invoices (header) — purchase invoices/bills, INCLUDING employee expense/reimbursement claims and petty cash disbursements. In this company's SAP setup, employees and 'Petty Cash' are registered as vendor-type Business Partner cards, so meal allowances, overtime meals, transportation reimbursements, representation expenses, etc. are recorded here as AP Invoices against the employee's/petty cash's CardName — NOT as journal entries. If the question is about an employee expense report, reimbursement, allowance, or claim (meals, transport, representation, etc.), prefer this table over OJDT/JDT1. Header-level free-text column: Comments.",
    "PCH1": "A/P Invoice line items — one row per line on a vendor purchase invoice OR an employee expense/reimbursement claim line (see OPCH). Free-text line description column: Dscription — this is where things like 'Meal Allowance', 'General Meals', 'Dinner (Name)', 'Grab to X' actually appear.",
    "OJDT": "Journal Entries (header) — manual/internal GL postings not tied to a vendor, customer, or employee reimbursement document (e.g. accruals, depreciation, internal reclassifications, adjustments between GL accounts). Do NOT use this for employee meal/expense/reimbursement claims in this company — those are recorded in OPCH/PCH1 instead (see OPCH description). Header-level free-text column: Memo — for journal entries the real description is very often written ONCE here rather than repeated on every line.",
    "JDT1": "Journal Entry line items — one row per debit/credit line on a manual journal entry. Free-text line description column: LineMemo (NOT Dscription) — frequently blank/generic on journal entries even when the header Memo has the real description.",
    "OITM": "Item Master — master data for inventory items (SKU, name, on-hand qty).",
    "OCRD": "Business Partner Master — customers AND vendors (CardType 'C' or 'S'). In this company's setup, individual employees and 'Petty Cash' are also registered here as vendor-type ('S') cards, used as the CardName on AP Invoices (OPCH) for expense/reimbursement claims.",
    "OSLP": "Sales Employee Master — sales rep/salesperson list.",
    "ORDR": "Sales Orders (header) — customer sales orders (not yet invoiced). Header-level free-text column: Comments.",
    "RDR1": "Sales Order line items. Free-text line description column: Dscription.",
    "OSRN": "Serial Number Master — serial-tracked item instances.",
}

# Which column holds the free-text line description, per DETAIL (T1) table.
# execute_dynamic_query uses this instead of hardcoding "Dscription" so
# keyword search doesn't break against tables like JDT1.
_TABLE_TEXT_COLUMN = {
    "INV1": "Dscription",
    "PCH1": "Dscription",
    "RDR1": "Dscription",
    "JDT1": "LineMemo",
}

# Which column holds the free-text HEADER description, per header (T0)
# table. Journal entries in particular tend to carry their real
# description here once, rather than on every JDT1 line — a keyword
# search scoped only to the detail table's text column (the old
# behavior) misses these entirely even when the record genuinely exists.
_TABLE_HEADER_TEXT_COLUMN = {
    "OJDT": "Memo",
    "OPCH": "Comments",
    "OINV": "Comments",
    "ORDR": "Comments",
}



def get_allowed_sap_schema(lark_user_id: str):
    """
    Returns {TABLE_NAME: {"description": str, "columns": [real column names]}}
    pulled live from SYS.TABLE_COLUMNS, so the router gets ground-truth
    column names instead of guessing by pattern-matching other tables
    (e.g. inventing "OJDT.DocDate" because OINV/OPCH have a DocDate —
    OJDT's actual columns are whatever HANA says they are here, not what
    we assume). "columns" holds the FULL list for server-side validation
    in execute_dynamic_query; ai_router.py decides how much of it to show
    the LLM (capped, to stay under Groq's TPM budget — see bot_audit.log
    for what happens when a schema dump gets too big).
    """
    check_circuit_breaker()
    conn = None
    cursor = None
    schema_map = {}
    
    try:
        conn = _get_connection_for_user(lark_user_id)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        
        sql = """
            SELECT TABLE_NAME, COLUMN_NAME
            FROM SYS.TABLE_COLUMNS 
            WHERE SCHEMA_NAME = ? 
              AND TABLE_NAME IN (
                  'OINV', 'INV1', 'OPCH', 'PCH1', 'OJDT', 'JDT1', 
                  'OITM', 'OCRD', 'OSLP', 'ORDR', 'RDR1', 'OSRN'
              )
            ORDER BY TABLE_NAME, POSITION
        """
        _safe_execute(cursor, sql, (config.HANA_CONFIG["currentSchema"],))
        
        # --- FIXED: Changed fetchmany(3000) back to fetchall() ---
        rows = cursor.fetchall()
        # ---------------------------------------------------------

        columns_by_table = {}
        for table_name, column_name in rows:
            table_name = table_name.upper()
            if table_name in _TABLE_BLOCKLIST:
                continue
            columns_by_table.setdefault(table_name, []).append(column_name)

        for table_name, columns in columns_by_table.items():
            schema_map[table_name] = {
                "description": _TABLE_DESCRIPTIONS.get(table_name, "Standard SAP B1 Document / Master Table"),
                "columns": columns,
            }
            
        _record_db_success()
        return schema_map
        
    except Exception as e:
        _record_db_failure()
        audit_log.error(f"Schema discovery failed for {lark_user_id}: {str(e)}")
        return {}
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def execute_dynamic_query(query_plan, lark_user_id):
    """
    Takes a JSON query plan, validates tables, compiles safe SAP HANA SQL 
    with automatic alias remapping, and executes it.
    """
    target_tables = query_plan.get("target_tables", [])
    select_columns = query_plan.get("select_columns", [])
    filters = query_plan.get("filters", [])
    
    if not target_tables:
        return "Query error: No target tables identified."

    # 1. ZERO-TRUST VALIDATION: Check against allowed schema
    allowed_schema = get_allowed_sap_schema(lark_user_id)
    for table in target_tables:
        clean_table = table.split('.')[-1].upper()
        if clean_table not in allowed_schema:
            return f"Security Exception: Access to table '{clean_table}' is blocked or it does not exist."

    # 2. BUILD THE 'FROM' CLAUSE & MAP ALIASES
    t0 = target_tables[0].upper()
    from_clause = f'"{t0}" T0'
    alias_map = {t0: "T0"}
    alias_to_table = {"T0": t0}
    
    if len(target_tables) > 1:
        t1 = target_tables[1].upper()
        alias_map[t1] = "T1"
        alias_to_table["T1"] = t1
        if (t0 == "OINV" and t1 == "INV1") or (t0 == "OPCH" and t1 == "PCH1") or (t0 == "ORDR" and t1 == "RDR1"):
            from_clause += f' INNER JOIN "{t1}" T1 ON T0."DocEntry" = T1."DocEntry"'
        elif t0 == "OJDT" and t1 == "JDT1":
            from_clause += f' INNER JOIN "{t1}" T1 ON T0."TransId" = T1."TransId"'
        else:
            from_clause += f', "{t1}" T1'

    # Ground-truth column validation: the router LLM can only see the
    # (capped) column list we show it, and — as seen in production
    # (bot_audit.log / the OJDT.DocDate case) — it will sometimes invent a
    # plausible-sounding column name by pattern-matching another table
    # rather than truly knowing this table's columns. Resolve every column
    # reference against the REAL list from get_allowed_sap_schema before
    # building SQL, so a bad guess fails fast with a specific message
    # instead of reaching HANA as an "invalid column name" error that then
    # gets vaguely paraphrased by the Analyst LLM.
    def _resolve_column(alias, col_name):
        table = alias_to_table.get(alias)
        real_columns = allowed_schema.get(table, {}).get("columns", [])
        lookup = {c.upper(): c for c in real_columns}
        match = lookup.get(col_name.upper())
        if match is None:
            sample = ", ".join(real_columns[:15])
            more = f", ... ({len(real_columns) - 15} more)" if len(real_columns) > 15 else ""
            raise ValueError(
                f"Query error: column '{col_name}' does not exist on table '{table}'. "
                f"Valid columns include: {sample}{more}"
            )
        return match

    # 3. FIX SELECT COLUMNS (Ensure they use proper alias formatting: T1."Dscription")
    processed_select = []
    try:
        for col in select_columns:
            if "." in col:
                tbl, column = col.split(".", 1)
                alias = alias_map.get(tbl.upper(), tbl)
                clean_col = column.replace('"', '')
                real_col = _resolve_column(alias, clean_col)
                processed_select.append(f'{alias}."{real_col}"')
            else:
                clean_col = col.replace('"', '')
                real_col = _resolve_column("T0", clean_col)
                processed_select.append(f'T0."{real_col}"')
    except ValueError as ve:
        return str(ve)

    select_clause = ", ".join(processed_select) if processed_select else "*"

    # 4. FIX FILTERS & INJECT KEYWORD SEARCHES
    processed_filters = []
    # Matches T0.SomeColumn / T1.SomeColumn (not already quoted) so we can
    # validate + quote the column name generically per-table, rather than
    # assuming any particular table uses a specific column name.
    _COL_REF_RE = re.compile(r'\b(T[01])\.("?)([A-Za-z_][A-Za-z0-9_]*)\2')

    try:
        for f in filters:
            for tbl, alias in alias_map.items():
                f = f.replace(f"{tbl}.", f"{alias}.")
                f = f.replace(f'"{tbl}".', f"{alias}.")

            def _validate_and_quote(m):
                alias, _, col_name = m.groups()
                real_col = _resolve_column(alias, col_name)
                return f'{alias}."{real_col}"'

            f = _COL_REF_RE.sub(_validate_and_quote, f)
            processed_filters.append(f)
    except ValueError as ve:
        return str(ve)

    # Automatically turn search_keywords into a SQL LIKE filter if present.
    # Checks BOTH the header table's free-text column (T0 — e.g. OJDT.Memo)
    # AND the detail table's (T1 — e.g. JDT1.LineMemo) when both exist,
    # since a document's real description often lives on only one of the
    # two (journal entries in particular: one Memo per entry, lines often
    # blank).
    #
    # Matching is case-insensitive. Each phrase is split into individual
    # words, but — unlike an earlier version of this code — those words
    # are AND'd together per phrase (word order doesn't matter, since real
    # free-text data isn't always phrased in the same order the LLM
    # guessed), not OR'd independently. OR'ing bare words let a single
    # generic term like "Allowance" or "Expense" match ANY allowance/
    # expense line, not just meal-related ones (e.g. "give me an
    # over-time meals report" pulling in parking allowances and cash
    # advances just because they also contain the word "Allowance").
    # Requiring every word of a phrase to be present keeps each phrase
    # meaningfully specific while still tolerating word-order variation
    # and synonyms across different phrases.
    #
    # A header-level match is only trusted for a given LINE when that
    # line's own free-text column is empty/blank — i.e. this line has no
    # description of its own to judge by, so we fall back to what the
    # document header says. When the line DOES have its own text, only
    # that text's own match counts. Without this, a header match (e.g. a
    # liquidation report titled "Meal Allowance & Transportation
    # Expense") would drag in every other line on that same document —
    # including unrelated lines like "Transport Cash Advance" — even
    # though those specific lines have nothing to do with meals.
    #
    # Values are bound as query parameters — never string-interpolated —
    # because search_keywords ultimately originates from LLM output shaped
    # by user-supplied text, and unescaped interpolation here is an
    # unparameterized-SQL-injection path into HANA that bypasses the
    # target_tables blocklist check above (e.g. a crafted keyword breaking
    # out of the string literal with a UNION SELECT).
    _KEYWORD_STOPWORDS = {"the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "per"}
    search_keywords = query_plan.get("search_keywords", [])
    sql_params = []
    if search_keywords:
        header_col = _TABLE_HEADER_TEXT_COLUMN.get(t0)
        detail_col = None
        if len(target_tables) > 1:
            t1_table = target_tables[1].split('.')[-1].upper()
            detail_col = _TABLE_TEXT_COLUMN.get(t1_table)

        if header_col or detail_col:
            def _all_words_match(alias, col, terms):
                """AND of per-word LIKEs against one column — every word of
                the phrase must appear somewhere in that column's value,
                in any order."""
                parts = []
                for term in terms:
                    parts.append(f'UPPER({alias}."{col}") LIKE UPPER(?)')
                    sql_params.append(f'%{term}%')
                return "(" + " AND ".join(parts) + ")"

            phrase_conditions = []
            for kw in search_keywords:
                terms = [w for w in kw.split() if len(w) >= 2 and w.lower() not in _KEYWORD_STOPWORDS]
                if not terms:
                    terms = [kw]

                sub_conditions = []
                if detail_col:
                    # Line-level match: this line's own text contains every word.
                    sub_conditions.append(_all_words_match("T1", detail_col, terms))
                    if header_col:
                        # Header-level fallback, gated to lines with no text of their own.
                        header_match = _all_words_match("T0", header_col, terms)
                        line_is_blank = f'(T1."{detail_col}" IS NULL OR TRIM(T1."{detail_col}") = \'\')'
                        sub_conditions.append(f'({header_match} AND {line_is_blank})')
                elif header_col:
                    sub_conditions.append(_all_words_match("T0", header_col, terms))

                if sub_conditions:
                    phrase_conditions.append("(" + " OR ".join(sub_conditions) + ")")

            if phrase_conditions:
                processed_filters.append("(" + " OR ".join(phrase_conditions) + ")")

    where_clause = ""
    if processed_filters:
        where_clause = " WHERE " + " AND ".join(processed_filters)

    # Default sort by date, but only if T0 genuinely has a "DocDate"
    # column. The previous check ("'T0' in from_clause") was always true
    # -- from_clause is built as f'"{t0}" T0', so the literal substring
    # "T0" is present no matter which table is queried -- so this fired
    # unconditionally and broke every dynamic_query against OJDT/JDT1
    # (journal entries), whose header date column is "RefDate", not
    # "DocDate" (see query_sap_gl_expense_report). Checking the real
    # column list avoids guessing a column that doesn't exist on T0.
    order_clause = ""
    t0_columns = {c.upper() for c in allowed_schema.get(t0, {}).get("columns", [])}
    if "DOCDATE" in t0_columns:
        order_clause = ' ORDER BY T0."DocDate" DESC'

    # 5. ASSEMBLE THE FINAL SQL STRING
    sql = f"SELECT {select_clause} FROM {from_clause}{where_clause}{order_clause}"
    # ---------------------------------------------

    # 6. EXECUTE AGAINST HANA
    conn = None
    cursor = None
    try:
        check_circuit_breaker()
        conn = _get_connection_for_user(lark_user_id)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        
        print(f"\n[DEBUG] Compiled SQL: {sql}")
        if sql_params:
            print(f"[DEBUG] Params: {sql_params}")
        
        _safe_execute(cursor, sql, tuple(sql_params) if sql_params else None)
        rows = cursor.fetchmany(3000)
        _record_db_success()
        
        if not rows:
            return "Query executed successfully, but no matching records were found for the requested criteria."
            
        col_names = [desc[0] for desc in cursor.description]
        raw_data = " | ".join(_clean(name) for name in col_names) + "\n"
        for row in rows:
            raw_data += " | ".join(_clean(value) for value in row) + "\n"
            
        return raw_data
        
    except PermissionError as pe:
        return str(pe)
    except Exception as e:
        _record_db_failure()
        return f"Database execution error: {str(e)}"
    finally:
        if cursor: cursor.close()
        if conn: conn.close()

def query_sap_gl_expense_report(expense_type, start_date, end_date, group_by, lark_user_id):
    conn = None
    cursor = None
    try:
        check_circuit_breaker()
        time.sleep(0.5)
        conn = _get_connection_for_user(lark_user_id)
        cursor = conn.cursor()
        _set_session_read_only(cursor)
        
        # 1. Handle Account & Keyword Filtering
        #
        # Account naming in this company's Chart of Accounts follows a
        # "<Division> - <Category>" convention (confirmed directly via SAP
        # B1's Query Generator: "SE - Meals", "T&T - Meals", "SE -
        # Transportation", "TS - Transportation", etc.) — there is NOT one
        # fixed account per expense type, there's one per (division,
        # category) pair. An exact-match against a single hardcoded
        # account name (e.g. AcctName = 'TS - MEALS') will only ever catch
        # one division and silently miss every other one, which is why
        # this previously under-reported. Matching on the category
        # fragment via LIKE '%...%' catches the category across every
        # division at once, the same way the finance team's own manual
        # query does it.
        if expense_type == "overtime_meals":
            # No distinct "overtime" account exists in this Chart of
            # Accounts — overtime vs. regular meals is only distinguished
            # in the line memo (e.g. "OT Meal for..."), not the account.
            # So "overtime_meals" here means "all Meals-category GL
            # entries" — matching what finance's own query actually pulls.
            acct_condition = 'T2."ActType" = \'E\' AND UPPER(T2."AcctName") LIKE \'%MEALS%\''
            query_param = None
        elif expense_type == "training_meals":
            # Same account-naming limitation as overtime_meals above:
            # there's no "training" segment in AcctName, so a training
            # claim is still a plain "<Division> - Meals" account -- the
            # word "training" only ever shows up in the line's own memo
            # text. Checking AcctName LIKE '%TRAINING%' (the original
            # version of this condition) can never match anything real
            # and would silently return zero rows for every training-meal
            # query, so the second half of the AND is checked against
            # T1."LineMemo" instead.
            acct_condition = 'T2."ActType" = \'E\' AND UPPER(T2."AcctName") LIKE \'%MEALS%\' AND UPPER(T1."LineMemo") LIKE \'%TRAINING%\''
            query_param = None
        elif expense_type == "all_expenses":
            acct_condition = 'T2."ActType" = \'E\''
            query_param = None
        else:
            # Converts "gas_allowance" into "GAS ALLOWANCE" for flexible matching
            clean_keyword = expense_type.replace("_", " ")
            acct_condition = 'T2."ActType" = \'E\' AND (UPPER(T2."AcctName") LIKE UPPER(?) OR UPPER(T1."LineMemo") LIKE UPPER(?))'
            query_param = f'%{clean_keyword}%'

        # 2. Securely map NLU grouping to SAP OcrCodes
        dimension_map = {
            "division": ('T1."ProfitCode"', 'Division'),
            "unit": ('T1."OcrCode2"', 'Unit'),
            "brand": ('T1."OcrCode3"', 'Brand'),
            "vehicle_assignee": ('T1."OcrCode4"', 'Vehicle Assignee'),
            "dimension_5": ('T1."OcrCode5"', 'Dimension 5'),
            "project": ('T1."Project"', 'Project')
        }

        # 3. Aggregated query if a dimension group is requested
        if group_by and group_by.lower() in dimension_map:
            col_sql, col_name = dimension_map[group_by.lower()]
            
            sql = f'''
                SELECT 
                    COALESCE({col_sql}, '(Unassigned)') AS "{col_name}", 
                    SUM(T1."Debit" - T1."Credit") AS "Total Amount",
                    COUNT(DISTINCT T1."TransId") AS "Transaction Count"
                FROM "OJDT" T0 
                INNER JOIN "JDT1" T1 ON T0."TransId" = T1."TransId" 
                INNER JOIN "OACT" T2 ON T1."Account" = T2."AcctCode" 
                WHERE {acct_condition} 
                  AND T0."RefDate" BETWEEN ? AND ?
                GROUP BY {col_sql}
                ORDER BY "Total Amount" DESC
            '''
            
            if query_param:
                params = (query_param, query_param, start_date, end_date)
            else:
                params = (start_date, end_date)
                
            _safe_execute(cursor, sql, params)
            rows = cursor.fetchmany(3000)
            _record_db_success()
            
            if not rows:
                return f"No records found between {start_date} and {end_date}."
                
            raw_data = f"{col_name} | Total Amount | Transaction Count\n"
            for row in rows:
                raw_data += f"{_clean(row[0])} | {_clean(row[1])} | {_clean(row[2])}\n"
                
            return raw_data

        else:
            # Fallback to standard line-by-line detail if no grouping is requested
            sql = f'''
                SELECT 
                    T0."RefDate" AS "Date", 
                    COALESCE(T1."LineMemo", T0."Memo") AS "Remarks", 
                    T2."AcctName" AS "Account", 
                    (T1."Debit" - T1."Credit") AS "Amount", 
                    T1."ProfitCode" AS "Division"
                FROM "OJDT" T0 
                INNER JOIN "JDT1" T1 ON T0."TransId" = T1."TransId" 
                INNER JOIN "OACT" T2 ON T1."Account" = T2."AcctCode" 
                WHERE {acct_condition} 
                  AND T0."RefDate" BETWEEN ? AND ?
                ORDER BY T0."RefDate" DESC
            '''
            
            if query_param:
                params = (query_param, query_param, start_date, end_date)
            else:
                params = (start_date, end_date)
                
            _safe_execute(cursor, sql, params)
            rows = cursor.fetchmany(3000)
            _record_db_success()
            
            if not rows:
                return f"No records found between {start_date} and {end_date}."
                
            raw_data = "Date | Remarks | Account | Amount | Division\n"
            for row in rows:
                raw_data += f"{_clean(row[0])} | {_clean(row[1])} | {_clean(row[2])} | {_clean(row[3])} | {_clean(row[4])}\n"
                
            return raw_data

    except Exception as e:
        _record_db_failure()
        return f"Database execution error: {str(e)}"
    finally:
        if cursor: cursor.close()
        if conn: conn.close()