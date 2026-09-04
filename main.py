"""
Entry point for the DEAi Lark <-> SAP HANA bridge.

Run with:  python main.py

This file owns only the orchestration: receiving Lark WebSocket events,
enforcing per-user rate limits / dedup / locks, dispatching to the right
command handler, and formatting+sending the reply. All the actual work
(SQL, LLM calls, card building) lives in modules/.
"""

import json
import socket
import sys
import time
from collections import defaultdict

import lark_oapi as lark
from lark_oapi.ws import Client

from modules import config, database, ai_router, lark_ui
from modules.config import audit_log

# Tracks chats currently processing a heavy command to block retries
_active_chat_locks = set()

# ==========================================
# Rate Limiting & Dedup
# ==========================================
_last_command_time = defaultdict(float)


def is_rate_limited(user_id):
    now = time.time()
    if now - _last_command_time[user_id] < config.MIN_SECONDS_BETWEEN_COMMANDS:
        return True
    _last_command_time[user_id] = now
    return False


_seen_message_ids = {}


def _is_duplicate_event(message_id):
    now = time.time()
    stale = [mid for mid, ts in _seen_message_ids.items() if now - ts > config.DEDUP_WINDOW_SECONDS]
    for mid in stale:
        del _seen_message_ids[mid]
    if message_id in _seen_message_ids:
        return True
    _seen_message_ids[message_id] = now
    return False


# ==========================================
# Command Help Text
# ==========================================
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


# ==========================================
# !ask Dispatch (natural language -> DB action)
# ==========================================
def _handle_ask(query, lark_user_id):
    # Pass lark_user_id to the router so it can fetch the live schema
    nlp_result = ai_router.parse_natural_language(query, lark_user_id)
    action = nlp_result.get("action")
    params = nlp_result.get("params", {})

    if nlp_result.get("error") == "model_error":
        return "⚠️ **AI Routing Model Unavailable:** The natural language router needs to be updated — please contact the bot admin."
    if nlp_result.get("error") == "quota_error":
        return "⚠️ **AI Router Quota Exhausted:** The NLP router is temporarily unavailable due to quota/rate limits. Please try again in a moment."
    if nlp_result.get("error") == "router_refused":
        return "⚠️ **AI Router Declined:** The router model treated this question as sensitive (e.g. payroll/compensation-sounding wording) and refused to build a query instead of returning JSON. Try rephrasing without terms like \"expenses\"/\"allowance\" in the same sentence, or contact the bot admin — this can also be reduced by switching the router to a less conservative model."
    if nlp_result.get("error") == "llm_failure":
        return "⚠️ **AI Router Error:** Something went wrong parsing the AI's response. Please try again or contact the bot admin if this keeps happening."

    raw_data = ""
    
    if action == "stock":
        item_code = params.get("item_code", "")
        print(f"\nQuerying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_hana(item_code, lark_user_id) if item_code else "I couldn't identify a specific item code in your question."
        
    elif action == "sales":
        print(f"\nQuerying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_sales_by_period(
            period=params.get("period", "month"),
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
            lark_user_id=lark_user_id
        )

    elif action == "gl_expense_report":
        print(f"\nQuerying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_gl_expense_report(
            expense_type=params.get("expense_type", "all_expenses"),
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
            group_by=params.get("group_by"),
            lark_user_id=lark_user_id
        )
    
    elif action == "top_clients":
        print(f"\nQuerying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_top_clients(params.get("limit", 5), lark_user_id)
        
    elif action == "aging":
        print(f"\nQuerying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_aging_invoices(lark_user_id)
        
    elif action == "report":
        print("\nFetching latest SAP Invoice Data...")
        print(f"Querying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_invoices_for_llm(lark_user_id)
        
    elif action == "dynamic_query":
        print("\nCompiling JSON Query Plan into secure SQL...")
        print(f"Querying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        
        # Pull the query plan and run it through the real compiler!
        query_plan = nlp_result.get("query_plan", {})
        raw_data = database.execute_dynamic_query(query_plan, lark_user_id)
        
    elif action == "rate_limit":
        return "⚠️ **AI Quota Exhausted:** The AI is receiving too many requests right now or has reached its daily limit. Please try again later."
    else:
        return "I'm not sure how to look that up yet. Try asking about stock, sales, top clients, or overdue invoices."

    if raw_data:
        print("Passing data to Analyst LLM for analysis...")
        return ai_router.generate_conversational_reply(raw_data, query)
        
    return raw_data


# ==========================================
# Command Router
# ==========================================
def _route_command(user_text, lark_user_id):
    """Returns (card_title, reply_text) for a given raw command string."""
    if user_text.startswith("!stock"):
        parts = user_text.split()
        if len(parts) > 1:
            item_code = parts[1]
            if not database.is_valid_item_code(item_code):
                reply_text = "Invalid item code format."
            else:
                print(f"\nQuerying SAP HANA for item: {item_code}...")
                print(f"Querying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
                print("Spawning isolated database session...")
                reply_text = database.query_sap_hana(item_code, lark_user_id)
        else:
            reply_text = "Please specify an item code. Example: `!stock 19-LAP-0001`"
        return "Stock Lookup", reply_text

    elif user_text.startswith("!sales"):
        parts = user_text.split()
        period = parts[1] if len(parts) > 1 and parts[1].lower() in ["today", "week", "month"] else "month"
        print(f"\nFetching sales data for period: {period}...")
        print(f"Querying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_sales_by_period(period, lark_user_id=lark_user_id)
        print("Passing data to Analyst LLM for analysis...")
        return "Sales Velocity Summary", ai_router.generate_llm_sales_report(raw_data, period)

    elif user_text.startswith("!topclients"):
        parts = user_text.split()
        limit = 5
        if len(parts) > 1 and parts[1].isdigit():
            limit = min(int(parts[1]), 20)
        print(f"\nFetching top {limit} clients...")
        print(f"Querying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_top_clients(limit, lark_user_id)
        print("Passing data to Analyst LLM for analysis...")
        return "Top Accounts Analysis", ai_router.generate_llm_top_clients_report(raw_data, limit)

    elif user_text.startswith("!aging"):
        print("\nFetching overdue open invoices...")
        print(f"Querying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_aging_invoices(lark_user_id)
        print("Passing data to Analyst LLM for analysis...")
        return "Overdue Receivables Aging", ai_router.generate_llm_aging_report(raw_data)

    elif user_text.startswith("!report"):
        print("\nFetching latest SAP Invoice Data...")
        print(f"Querying DEAI_USER_MAP for Lark open_id: {lark_user_id}")
        print("Spawning isolated database session...")
        raw_data = database.query_sap_invoices_for_llm(lark_user_id)
        print("Passing data to Analyst LLM for analysis...")
        return "SAP Executive Report", ai_router.generate_llm_report(raw_data)

    elif user_text.startswith("!ask"):
        query = user_text[4:].strip()
        if not query:
            reply_text = "What would you like to ask? (e.g., `!ask what are our top 5 clients?`)"
        else:
            print(f"Parsing natural language: {query}")
            # NOW PASSING lark_user_id DOWN TO _handle_ask
            reply_text = _handle_ask(query, lark_user_id)
        return "AI Assistant", reply_text

    elif user_text.startswith("!help"):
        return "Available Commands", get_help_text()

    else:
        return "SAP HANA Bot", "Unknown command. Type `!help` to see everything I can do."


# ==========================================
# Lark WebSocket Message Handler
# ==========================================
def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    event = data.event
    message = event.message
    chat_id = message.chat_id
    message_id = message.message_id
    sender_id = event.sender.sender_id.open_id if event.sender and event.sender.sender_id else "unknown"

    if message_id and _is_duplicate_event(message_id):
        return
    if chat_id in _active_chat_locks:
        return

    content_json = json.loads(message.content)
    user_text = content_json.get("text", "").strip()

    print(f"\nReceived command: {user_text} from user {sender_id}")

    if is_rate_limited(sender_id):
        lark_ui.send_lark_reply(message_id, "You're sending commands too quickly. Please wait a few seconds and try again.")
        return

    try:
        database.check_circuit_breaker()
    except RuntimeError as e:
        lark_ui.send_lark_reply(message_id, str(e))
        return

    _active_chat_locks.add(chat_id)
    try:
        lark_ui.send_lark_text(message_id, "⏳ _Analyzing SAP records, please wait a moment..._")
        
        # PASSING sender_id TO THE ROUTER
        try:
            card_title, reply_text = _route_command(user_text, sender_id)
        except Exception as e:
            config.audit_log.error(f"Command routing failed for {sender_id}: {e}", exc_info=True)
            card_title = "SAP HANA Bot"
            reply_text = "Sorry, I couldn't process that command right now. Please try again shortly."
        
        lark_ui.send_lark_reply(message_id, reply_text, title=card_title)
        
        print("Reply sent to Lark successfully!\n")
        
    finally:
        _active_chat_locks.discard(chat_id)


def _do_message_read_v1(data) -> None:
    pass


# ==========================================
# Single-Instance Lock
# ==========================================
_instance_lock_socket = None


def _acquire_single_instance_lock():
    global _instance_lock_socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", config.SINGLE_INSTANCE_LOCK_PORT))
    except OSError:
        print("Another instance of this bot is already running. Close it first.")
        sys.exit(1)
    _instance_lock_socket = sock


# ==========================================
# Main Execution
# ==========================================
def main():
    _acquire_single_instance_lock()
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
        .register_p2_customized_event("im.message.message_read_v1", _do_message_read_v1) \
        .build()
    ws_client = Client(config.APP_ID, config.APP_SECRET, event_handler=event_handler, domain=config.LARK_DOMAIN, log_level=lark.LogLevel.INFO)
    print("Starting direct Lark-to-SAP-HANA WebSocket bridge (with AI Integration)...")
    ws_client.start()


if __name__ == "__main__":
    main()