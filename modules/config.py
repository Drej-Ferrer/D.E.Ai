"""
Centralized configuration for the DEAi Lark <-> SAP HANA bot.

Every other module imports its settings from here instead of calling
load_dotenv()/os.getenv() itself. This guarantees the .env file is read
exactly once and that every module sees the same values.
"""

import os
import logging
from typing import Dict

from dotenv import load_dotenv

# ==========================================
# Load Environment Variables
# ==========================================
# .env lives at the project root (one level up from modules/).
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_ENV_PATH, override=True)


# ==========================================
# Lark Credentials
# ==========================================
APP_ID = os.getenv("LARK_APP_ID")
APP_SECRET = os.getenv("LARK_APP_SECRET")
LARK_DOMAIN = "https://open.larksuite.com"


# ==========================================
# SAP HANA Settings & Connection Dict
# ==========================================
MAX_ROWS = int(os.getenv("SAP_MAX_ROWS", 50))
ALLOW_RAW_SQL = os.getenv("ALLOW_RAW_SAP_SQL", "false").lower() == "true"

if ALLOW_RAW_SQL:
    raise RuntimeError(
        "ALLOW_RAW_SAP_SQL must remain false — HANASA has write access and "
        "this bot relies on app-layer read-only enforcement, not DB grants."
    )

HANA_CONFIG: Dict = {
    "address": os.getenv("SAP_HOST"),
    "port": int(os.getenv("SAP_PORT", 30015)),
    "user": os.getenv("SAP_USER"),
    "password": os.getenv("SAP_PASSWORD"),
    "currentSchema": os.getenv("SAP_SCHEMA"),
    "connectTimeout": int(os.getenv("SAP_CONNECT_TIMEOUT", 10000)),
    "encrypt": True,
    "sslValidateCertificate": True,
    "sslHostNameInCertificate": os.getenv("SAP_SSL_HOSTNAME", "DIRECHANASERVER"),
}


# ==========================================
# LLM Provider Configuration (Role-Based)
# ==========================================
# ROUTER: Fast intent classification (NLU parsing for !ask command)
ROUTER_LLM_PROVIDER = os.getenv("ROUTER_LLM_PROVIDER", "groq").lower()
ROUTER_LLM_MODEL = os.getenv("ROUTER_LLM_MODEL", "openai/gpt-oss-20b")

# ANALYST: Heavy report generation & summarization
ANALYST_LLM_PROVIDER = os.getenv("ANALYST_LLM_PROVIDER", "gemini").lower()
ANALYST_LLM_MODEL = os.getenv("ANALYST_LLM_MODEL", "gemini-3.6-flash")

# Sampling temperature, per role. The ROUTER classifies intent and invents
# search_keywords variants (see ai_router.parse_natural_language) -- at the
# old flat 0.7, asking the exact same natural-language question twice can
# produce two different keyword lists and therefore two different (each
# individually correct) totals, which looks like the bot is unreliable even
# though every individual run is doing real, honest SQL. Lower temperature
# trades away keyword-phrasing creativity for repeatability, which is the
# right trade for a classification/query-planning step. The ANALYST is
# writing free-text commentary for a human to read, so it keeps more room
# to vary its phrasing.
ROUTER_LLM_TEMPERATURE = float(os.getenv("ROUTER_LLM_TEMPERATURE", 0.2))
ANALYST_LLM_TEMPERATURE = float(os.getenv("ANALYST_LLM_TEMPERATURE", 0.4))

# Request timeout for all LLM SDK clients (seconds)
LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", 60))


# ==========================================
# Audit Logging (shared logger instance)
# ==========================================
logging.basicConfig(filename="bot_audit.log", level=logging.INFO, format="%(asctime)s | %(message)s")
audit_log = logging.getLogger("audit")


# ==========================================
# Safeguard Tunables
# ==========================================
FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 300
MIN_SECONDS_BETWEEN_COMMANDS = 3
DEDUP_WINDOW_SECONDS = 600
SINGLE_INSTANCE_LOCK_PORT = 47291