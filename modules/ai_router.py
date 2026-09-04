"""
LLM provider abstraction layer + all prompt/report-generation logic.

Two roles are used throughout the bot:
  - ROUTER  : fast intent classification for the `!ask` command
  - ANALYST : heavier report generation & summarization

Both roles are configurable per-provider/model via modules/config.py.
"""

import datetime
import json
import re
import time
from typing import Any, Dict

from modules import config
from modules.config import audit_log

# Lazy client cache (only instantiate active providers)
_llm_clients: Dict[str, Any] = {}


# ==========================================
# Exceptions
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


# ==========================================
# Client Factory
# ==========================================
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
    import os

    provider = provider.lower()

    if provider in _llm_clients:
        return _llm_clients[provider]

    if provider == "groq":
        from groq import Groq
        client = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=config.LLM_REQUEST_TIMEOUT)
        _llm_clients[provider] = client
        return client

    elif provider == "gemini":
        import google.generativeai as genai
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        _llm_clients[provider] = genai
        return genai

    elif provider == "anthropic":
        from anthropic import Anthropic
        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), timeout=config.LLM_REQUEST_TIMEOUT)
        _llm_clients[provider] = client
        return client

    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=config.LLM_REQUEST_TIMEOUT)
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


# ==========================================
# Unified LLM Call Wrapper
# ==========================================
def _llm_call(prompt, system_prompt=None, is_json=False, custom_provider=None, custom_model=None, temperature=0.7):
    """
    Universal LLM API dispatcher. 
    Routes requests to the specified provider and normalizes token tracking.
    """
    # Allow is_json to be passed as second positional argument if system_prompt is omitted
    if isinstance(system_prompt, bool):
        is_json = system_prompt
        system_prompt = None

    provider = custom_provider or config.ROUTER_LLM_PROVIDER
    model = custom_model or config.ROUTER_LLM_MODEL
    
    text = ""
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    try:
        if provider in ("groq", "openai"):
            client = _get_client(provider)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            response = client.chat.completions.create(
                messages=messages,
                model=model,
                # Removed response_format flag to prevent Groq API 400 errors
                temperature=temperature
            )
            text = response.choices[0].message.content
            
            if hasattr(response, 'usage') and response.usage:
                prompt_tokens = response.usage.prompt_tokens
                completion_tokens = response.usage.completion_tokens
                total_tokens = response.usage.total_tokens

        elif provider == "gemini":
            client = _get_client("gemini")
            gemini_model = client.GenerativeModel(
                model_name=model,
                system_instruction=system_prompt if system_prompt else None
            )
            generation_config = {"temperature": temperature}
            if is_json:
                generation_config["response_mime_type"] = "application/json"
            response = gemini_model.generate_content(
                prompt, 
                generation_config=generation_config
            )
            text = response.text
            
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                prompt_tokens = response.usage_metadata.prompt_token_count
                completion_tokens = response.usage_metadata.candidates_token_count
                total_tokens = response.usage_metadata.total_token_count

        elif provider == "anthropic":
            client = _get_client("anthropic")
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=system_prompt if system_prompt else "",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature
            )
            text = response.content[0].text
            
            if hasattr(response, 'usage') and response.usage:
                prompt_tokens = response.usage.input_tokens
                completion_tokens = response.usage.output_tokens
                total_tokens = prompt_tokens + completion_tokens

        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")

        # ==========================================
        # GLOBAL DEBUG INJECTION: Output Universal Metadata
        # ==========================================
        print("\n" + "="*60)
        print(f"🧠 [DEBUG] LLM CALL COMPLETE: {provider.upper()} ({model})")
        print("="*60)
        
        if total_tokens > 0:
            print(f"⚡ Tokens: {total_tokens} (Prompt: {prompt_tokens} | Completion: {completion_tokens})")
        else:
            print("⚡ Tokens: (Usage metadata not returned by provider)")
            
        if is_json:
            print("-" * 60)
            print("🔍 RAW JSON OUTPUT:")
            print(text.strip())
            
        print("="*60 + "\n")
        # ==========================================

        if is_json:
            text = _strip_json_fence(text)
            
        return text

    except Exception as e:
        err_str = str(e)
        config.audit_log.error(f"LLM API Error ({provider}): {err_str}")
        print(f"!!! LLM Call Failed: {err_str} !!!")

        if not is_json:
            return f"Sorry, the AI model ({provider}) encountered an error."

        # Classify the failure instead of collapsing everything into one
        # generic "llm_failure" — a TPM/rate-limit rejection, a model
        # refusal (small router models sometimes decline in plain text
        # instead of JSON when a question sounds sensitive, e.g.
        # payroll-adjacent terms), and a genuinely broken model are very
        # different problems and need different messages/fixes.
        lower = err_str.lower()
        if "rate_limit" in lower or "tokens per minute" in lower or " 429" in lower or " 413" in lower or "too large" in lower:
            return '{"action": "error", "error": "quota_error"}'
        if "json_validate_failed" in lower or "failed to generate json" in lower:
            return '{"action": "error", "error": "router_refused"}'
        if "model_not_found" in lower or "does not exist" in lower or "decommissioned" in lower:
            return '{"action": "error", "error": "model_error"}'
        return '{"action": "error", "error": "llm_failure"}'


def call_router(prompt: str, is_json: bool = False) -> str:
    """Call the ROUTER LLM (fast intent classification for !ask command)."""
    return _llm_call(prompt, is_json=is_json, custom_provider=config.ROUTER_LLM_PROVIDER, custom_model=config.ROUTER_LLM_MODEL, temperature=config.ROUTER_LLM_TEMPERATURE)


def call_analyst(prompt: str, is_json: bool = False) -> str:
    """Call the ANALYST LLM (heavy report generation & summarization)."""
    return _llm_call(prompt, is_json=is_json, custom_provider=config.ANALYST_LLM_PROVIDER, custom_model=config.ANALYST_LLM_MODEL, temperature=config.ANALYST_LLM_TEMPERATURE)


# ==========================================
# Report Generation Prompts
# ==========================================
# All four of these used to hand the Analyst LLM the raw pipe-delimited
# data and ask it to build its OWN "Metric | Value" summary table
# directly from that text -- no grounding check, nothing stopping a
# quietly-wrong total from reaching the user. !ask's generate_conversational_reply
# already has the hardened version of this (a deterministic table, a
# code-verified stats line, and a citation-checked narrative) -- these
# four now go through the exact same core (_generate_grounded_report,
# defined below) instead of maintaining a second, weaker standard for
# the same kind of report just because it's reached by a slash command
# instead of !ask.
def generate_llm_report(raw_sap_data):
    small_role = """You are an expert corporate financial analyst reviewing recent SAP invoice data.
Write a "Detailed Metrics & Executive Insights" bullet list (not a table): 3-5 bullets, each starting
with a short **bold label** followed by a colon and a QUALITATIVE explanation -- notable clients,
concentration by date, risk patterns. Do not use '#' Markdown headers. Do not build a table yourself."""
    return _generate_grounded_report(raw_sap_data, "Period Performance Overview", small_role,
                                      no_data_markers=("Sorry", "No invoice"))


def generate_llm_sales_report(raw_data, period):
    small_role = f"""You are a corporate sales analyst reviewing SAP sales invoice data for the period
'{period.upper()}'. Write an "Executive Sales Velocity" bullet list: 3-4 bullets, each starting with a
short **bold label**, covering trend, concentration, or standout clients/days. Do not use '#' headers.
Do not build a table yourself."""
    return _generate_grounded_report(raw_data, f"Sales Performance ({period.capitalize()})", small_role,
                                      no_data_markers=("Sorry", "No sales"))


def generate_llm_top_clients_report(raw_data, limit):
    small_role = """You are a corporate financial analyst reviewing the top revenue-generating SAP
accounts. Write an "Account Concentration & Insights" bullet list: 3-4 bullets, each starting with a
short **bold label**, covering concentration risk, gaps between top accounts, or notable patterns.
Do not use '#' headers. Do not build a table yourself."""
    return _generate_grounded_report(raw_data, f"Top {limit} Accounts by Revenue", small_role,
                                      no_data_markers=("Sorry", "No client"))


def generate_llm_aging_report(raw_data):
    small_role = """You are a corporate credit & collections controller reviewing overdue SAP invoices.
Write a "Collections Risk & Priority Action" bullet list: 3-4 bullets, each starting with a short
**bold label**, covering which accounts need urgent action, notable client concentration, or
recommended next steps. Do not use '#' headers. Do not build a table yourself."""
    return _generate_grounded_report(raw_data, "Receivables Aging Summary", small_role,
                                      no_data_markers=("Sorry", "No overdue"))




def _pipe_text_to_markdown_table(raw_data: str):
    """
    Every query function in database.py already returns rows as plain
    `col1 | col2 | ...` pipe-delimited text. Rather than asking the LLM
    to re-type that into a proper Markdown table (which is only as
    reliable as that specific model's formatting compliance — and is
    exactly what silently broke both table rendering AND chart
    generation for !ask), build the real table here in code:
    deterministic, always complete, always parseable by lark_ui's
    table/chart detector regardless of which Analyst model is active.

    Returns None if raw_data doesn't look tabular (an error string, a
    "No records found" message, or a lone metadata line like
    "Period: MONTH") — in that case there's nothing to chart anyway.
    """
    if not raw_data or not isinstance(raw_data, str):
        return None

    # Some functions (e.g. query_sap_sales_by_period) prepend a
    # non-tabular metadata line ("Period: MONTH") before the header row.
    # Keep only lines that actually look like pipe-delimited rows.
    lines = [ln for ln in raw_data.strip().split("\n") if ln.strip() and "|" in ln]
    if len(lines) < 2:
        return None  # need a header row + at least one data row to be worth a table

    header_cells = [c.strip() for c in lines[0].split("|")]
    col_count = len(header_cells)

    md_lines = ["| " + " | ".join(header_cells) + " |", "| " + " | ".join([":---"] * col_count) + " |"]
    for line in lines[1:]:
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < col_count:
            cells += [""] * (col_count - len(cells))
        elif len(cells) > col_count:
            cells = cells[:col_count]
        md_lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(md_lines)


def generate_conversational_reply(raw_data, query):
    """
    Takes raw SAP data and the user's original query and generates a
    conversational response via the shared _generate_grounded_report core
    (see below) -- the same hardened path the four canned report commands
    (!report/!sales/!topclients/!aging) now also use, so !ask and the
    slash commands carry the same reliability guarantees instead of two
    different standards living side by side in the same bot.
    """
    small_role = f"""You are a Senior Financial Controller and SAP B1 Data Analyst answering the
user's question: "{query}"

Your task is to provide a comprehensive, rigorous executive analysis of this report. You MUST include:
1. **Top Spenders:** Explicitly name the top 2-3 units/dimensions with the highest total expenses and cite their exact amounts from the table.
2. **Transaction Volume vs. Value:** Point out if high transaction counts match high monetary value, or if low-volume units are carrying unusually high costs.
3. **Anomalies & Outliers:** Call out any unusual negative values, reversals, or massive spikes (such as major credit adjustments) so leadership is aware, and name which row they belong to.
4. **Professional Tone:** Sharp, data-driven financial insights management can act on immediately -- no generic filler sentences."""

    large_role = f"""You are a Senior Financial Controller and SAP B1 Data Analyst answering the
user's question: "{query}"

Using the non-numeric columns available (names, categories, dates, descriptions) plus the confirmed
aggregate figures given to you, write 2-4 sentences of focused analysis: which category/client/date
range appears most often, any name worth calling out, any timing pattern -- and reference the
confirmed record count/total where it's actually relevant to answering the question. Professional
tone, no generic filler."""

    return _generate_grounded_report(raw_data, title=None, small_role_prompt=small_role,
                                      large_role_prompt=large_role, no_data_markers=())


def _generate_grounded_report(raw_data, title, small_role_prompt, large_role_prompt=None, no_data_markers=()):
    """
    Shared hardened analysis core, used by generate_conversational_reply
    AND all four canned report functions. Builds a deterministic Markdown
    table of every matching row (see _pipe_text_to_markdown_table),
    computes a verified record count and (when safe) a total in Python
    (see _compute_ground_truth_stats) -- never delegated to the LLM -- and
    produces a short analysis section on top, with different safety
    handling depending on table size:

    - SMALL tables (<= _SAFE_TO_SHOW_NUMBERS_MAX_ROWS, e.g. an already
      SQL-aggregated GROUP BY breakdown) are shown to the LLM with their
      real numbers intact and it's asked for real, numbers-citing
      analysis (top spenders, anomalies).
    - LARGE tables (raw line-item dumps, where an LLM eyeballing/summing
      dozens of rows tends to drift from the real total) have their
      numeric columns stripped from what the model sees -- but, unlike
      an earlier version of this function, the model IS still given the
      separately-verified record count and total as citable ground truth
      text, so it can write "the 52 matching meal expenses total
      ₱28,683.47, concentrated in..." instead of staying silent about
      figures that were, in fact, already verified. It just can't derive
      any OTHER number itself.

    Either way, _verify_narrative checks every number the model's output
    actually contains against what's real (raw cell values, or the
    verified aggregate) before the narrative is allowed through -- so
    citing the confirmed figures is fine, inventing anything else isn't.
    """
    if any(marker in raw_data for marker in no_data_markers):
        return raw_data

    table_md = _pipe_text_to_markdown_table(raw_data)
    if table_md is None:
        return raw_data

    record_count, total, total_label = _compute_ground_truth_stats(raw_data)

    _, parsed_rows = _parse_pipe_table(raw_data)
    row_count = len(parsed_rows)
    is_small = 0 < row_count <= _SAFE_TO_SHOW_NUMBERS_MAX_ROWS
    large_role_prompt = large_role_prompt or small_role_prompt

    if is_small:
        narrative_safe_data = raw_data
        numeric_rule = """The table below has ALREADY been computed and verified directly from the
database -- every figure in it is ground truth. You may cite a figure ONLY if it is copied verbatim
from a single cell in the table (or the confirmed total, if given). Do NOT compute or state an
average, rate, percentage, or a sum/subtotal across a subset of rows -- even an approximate one --
since that number would not appear anywhere in the table and cannot be verified. If a pattern is
best explained with a computed figure, describe it qualitatively instead (e.g. "the highest average
cost per transaction" rather than inventing the number). Format all monetary values using the
Philippine Peso symbol (₱), not $."""
        role_prompt = small_role_prompt
    else:
        narrative_safe_data = _strip_numeric_columns_for_narrative(raw_data)
        confirmed_bits = [f"Record Count = {record_count}"]
        if total is not None:
            confirmed_bits.append(f"{total_label} Sum = ₱{total:,.2f}")
        numeric_rule = f"""This is a large, line-item-level result -- individual amount/quantity/
doc-number columns have been removed from what you're shown below, because summing or citing dozens
of individual rows is exactly where an LLM's arithmetic drifts from the real numbers. However, these
AGGREGATE figures have ALREADY been computed and verified in code and ARE safe to cite exactly as
given: {"; ".join(confirmed_bits)}. Do not state, estimate, or imply ANY other numeric amount,
percentage, or count beyond those. Format all monetary values using the Philippine Peso symbol (₱),
not $."""
        role_prompt = large_role_prompt

    system_prompt = f"{role_prompt}\n\n{numeric_rule}"
    prompt = f"Matching SAP records:\n{narrative_safe_data}"

    try:
        response_text = _llm_call(
            prompt,
            system_prompt=system_prompt,
            is_json=False,
            custom_provider=config.ANALYST_LLM_PROVIDER,
            custom_model=config.ANALYST_LLM_MODEL,
            temperature=config.ANALYST_LLM_TEMPERATURE
        )
    except Exception as e:
        config.audit_log.error(f"Error generating grounded report ({title}): {str(e)}")
        return "Sorry, I pulled the data but couldn't format the response. Please try again."

    # _llm_call swallows its own failures and returns a "Sorry, the AI
    # model (...) encountered an error." string rather than raising —
    # so the except above never actually fires on an LLM-side failure.
    # Without this check, that error string would get the success
    # trailer appended below, misrepresenting a failure as a completed
    # analysis of live data.
    if _is_llm_error_text(response_text):
        return response_text

    narrative = response_text.strip()

    if narrative:
        narrative, _ = _filter_narrative_lines(narrative, raw_data, record_count, total)

    if record_count > 0:
        capped_warning = " *(Capped at max limit)*" if record_count >= config.MAX_ROWS else ""
        if total is not None:
            stats_line = f"Showing **{record_count}** matching record(s){capped_warning} — combined {total_label}: **₱{total:,.2f}**."
        else:
            stats_line = f"Showing **{record_count}** matching record(s){capped_warning}."
        body_intro = f"{stats_line}\n\n{narrative}" if narrative else stats_line
    else:
        body_intro = narrative

    header = f"**{title}**\n\n" if title else ""
    body = f"{header}{body_intro}\n\n{table_md}" if table_md else f"{header}{body_intro}"
    return f"{body}\n\n_Generated by DEAi from live SAP HANA data — please verify important figures before acting on them._"


# Column-name tokens used by _compute_ground_truth_stats to decide which
# numeric column, if any, is safe to sum server-side.
_LINE_LEVEL_SUM_TOKENS = {"linetotal"}  # per-line amount — one real value per row, always safe to sum
_DOC_LEVEL_SUM_TOKENS = {"totalamount", "totalrevenue", "outstandingamount", "doctotal"}  # document-level rollups
_DOC_ID_TOKENS = {"docnum", "docentry"}


def _normalize_col_name(name: str) -> str:
    return "".join(re.findall(r'[a-z0-9]+', name.lower()))


def _parse_pipe_table(raw_data):
    """Same tabular-line filtering as _pipe_text_to_markdown_table, but
    returns (header_cells, [row_cells, ...]) for server-side arithmetic
    instead of a display string."""
    if not raw_data or not isinstance(raw_data, str):
        return None, []
    lines = [ln for ln in raw_data.strip().split("\n") if ln.strip() and "|" in ln]
    if len(lines) < 2:
        return None, []
    header_cells = [c.strip() for c in lines[0].split("|")]
    col_count = len(header_cells)
    data_rows = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < col_count:
            cells += [""] * (col_count - len(cells))
        elif len(cells) > col_count:
            cells = cells[:col_count]
        data_rows.append(cells)
    return header_cells, data_rows


# Same shape as lark_ui._NUMERIC_CELL_RE: a cell counts as numeric only if,
# once currency symbols/commas/% are stripped, what's left is a plain
# number -- not just "contains a digit somewhere" (which would also catch
# dates like "2026-01-14").
_NARRATIVE_NUMERIC_CELL_RE = re.compile(r'^[\s$€£¥₱]*-?[\d,]+\.?\d*\s*%?\s*$')



# A table with this many rows or fewer is treated as "already aggregated"
# (e.g. a GROUP BY division/unit breakdown) -- small enough that showing
# the narrative LLM the real numbers and checking its citations against
# them is safe and worthwhile. Above this, tables are assumed to be raw
# line-item dumps where summing/citing individual figures is the kind of
# thing an LLM drifts on, so numeric columns stay stripped instead (see
# generate_conversational_reply).
_SAFE_TO_SHOW_NUMBERS_MAX_ROWS = 25


def _extract_currency_like_numbers(text):
    """Pulls numeric magnitudes out of currency-shaped substrings in free text, e.g. '-₱13,425.75' -> -13425.75."""
    nums = []
    for m in re.finditer(r'-?[₱$]\s*-?[\d,]+(?:\.\d+)?|-?\b\d[\d,]*\.\d{2}\b', text):
        cleaned = re.sub(r'[^\d.\-]', '', m.group())
        try:
            nums.append(float(cleaned))
        except ValueError:
            pass
    return nums


def _extract_all_numbers_from_raw(raw_data):
    """Every numeric cell value anywhere in the raw pipe-table, used as the ground-truth set to check narrative citations against."""
    header_cells, rows = _parse_pipe_table(raw_data)
    if header_cells is None:
        return set()
    vals = set()
    for row in rows:
        for cell in row:
            cleaned = re.sub(r'[^\d.\-]', '', cell)
            if cleaned and cleaned not in ('-', '.'):
                try:
                    vals.add(round(float(cleaned), 2))
                except ValueError:
                    pass
    return vals


_COUNT_PHRASE_RE = re.compile(
    r'\b(\d+)\s+(?:\w+\s+){0,3}?(?:records?|matching|entries|invoices|lines|claims|items|transactions?)\b',
    re.IGNORECASE
)


def _verify_narrative(narrative, raw_data, record_count, total, tolerance=0.5):
    """
    True if every number the narrative actually states can be traced back
    to something real, whichever path produced the narrative:

    - Currency-shaped figures (₱1,234.56, $500) must match either an
      individual cell value present in raw_data, OR the separately
      verified aggregate `total` -- so a large-table narrative CAN
      legitimately cite the one confirmed total it was handed as ground
      truth (see _generate_grounded_report) without that being flagged as
      "invented", while a number that traces to neither is rejected.
    - Bare count phrases ("52 records", "12 transactions") must match
      EITHER the overall record_count OR some other numeric cell in the
      table. The latter half matters for already-grouped/aggregated
      results: a bullet citing "R&D: ₱746,380.98 (12 transactions)" is
      citing R&D's own per-row Transaction Count cell, which has nothing
      to do with the overall row count (e.g. 24 grouped units) -- an
      earlier version of this check compared every count phrase only
      against record_count, which meant almost any per-row count in a
      grouped report failed verification even when it was copied
      correctly straight off the table.

    Used uniformly for both the small (real numbers shown) and large
    (numbers stripped, verified stats given as text) paths — replaces the
    small-table-only grounding check and the large-table-only "any number
    is suspicious" regex that used to be two separate, narrower rules.
    """
    source_values = _extract_all_numbers_from_raw(raw_data)
    if total is not None:
        source_values = source_values | {round(total, 2)}

    cited = _extract_currency_like_numbers(narrative)
    if cited:
        if not source_values:
            return False
        if not all(any(abs(val - sv) <= tolerance for sv in source_values) for val in cited):
            return False

    allowed_counts = source_values | {record_count}
    for m in _COUNT_PHRASE_RE.finditer(narrative):
        n = int(m.group(1))
        if not any(abs(n - av) <= tolerance for av in allowed_counts):
            return False

    return True


def _filter_narrative_lines(narrative, raw_data, record_count, total, tolerance=0.5):
    """
    Line-by-line wrapper around _verify_narrative. A single unverifiable
    number -- an LLM-computed average, a wrong subtotal, anything that
    doesn't trace back to a real cell or the confirmed total -- used to
    take the ENTIRE narrative down with it, discarding several other,
    perfectly accurate, cited bullets in the same response. This keeps
    every line that verifies clean on its own and drops only the lines
    that don't, so one bad number costs one bullet instead of the whole
    analysis. Returns (filtered_text, whether_anything_was_dropped).
    """
    kept_lines = []
    dropped_any = False
    for line in narrative.split("\n"):
        if line.strip() and not _verify_narrative(line, raw_data, record_count, total, tolerance):
            dropped_any = True
            config.audit_log.warning(f"Dropped unverifiable narrative line: {line!r}")
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip(), dropped_any


def _strip_numeric_columns_for_narrative(raw_data):
    """
    Removes columns whose values are purely numeric (amounts, quantities,
    doc numbers) before raw_data is shown to the narrative-writing LLM call
    in generate_conversational_reply.

    The record count and total are already computed safely in Python by
    _compute_ground_truth_stats, and the system prompt tells the model
    never to restate them -- but as long as the real numbers are still
    sitting in its prompt, "don't mention them" is just an instruction the
    model can ignore or misremember (this is exactly what produced a
    fully-prose summary sentence that folded the count/total into its own
    wording instead of using the verified figures verbatim). Removing the
    numeric columns outright makes the model structurally unable to restate
    or silently miscompute a figure that's already been verified elsewhere.
    Text columns (descriptions, names, dates) are kept so the model still
    has enough context to write a real qualitative pattern observation.
    """
    header_cells, rows = _parse_pipe_table(raw_data)
    if header_cells is None:
        return raw_data  # not tabular (error string, "No records found", etc.) -- pass through as-is

    keep_idx = []
    for i in range(len(header_cells)):
        sample = [r[i] for r in rows[:8] if i < len(r) and r[i].strip()]
        if sample and all(_NARRATIVE_NUMERIC_CELL_RE.match(v) for v in sample):
            continue  # every sampled value in this column is a plain number -- drop it
        keep_idx.append(i)

    new_header = [header_cells[i] for i in keep_idx]
    lines = [" | ".join(new_header)]
    for r in rows:
        lines.append(" | ".join(r[i] if i < len(r) else "" for i in keep_idx))
    return "\n".join(lines)


def _compute_ground_truth_stats(raw_data):
    """
    Computes (record_count, total_or_None, total_column_label_or_None)
    entirely in Python from the actual rows — never delegated to the LLM.

    A numeric column is only summed when it's actually safe to sum:
    - A "LineTotal"-named column is always safe: it's the canonical
      per-line amount in this schema, one genuine amount per row.
    - A document-level total (DocTotal, "Total Amount", "Total Revenue",
      "Outstanding Amount") is only safe when each row IS a distinct
      document. If a document-id-like column (DocNum/DocEntry) is present
      AND its values repeat across rows — i.e. this is a header-joined-
      to-detail result with several line rows per document — summing a
      document-level total would count that document's total once per
      matching LINE instead of once per DOCUMENT, wildly overstating the
      real figure. In that case we deliberately do NOT sum it and only
      report the count, rather than show a confidently-wrong total.
    """
    header_cells, rows = _parse_pipe_table(raw_data)
    if header_cells is None:
        return 0, None, None

    record_count = len(rows)
    if record_count == 0:
        return 0, None, None

    normalized = [_normalize_col_name(c) for c in header_cells]

    doc_id_idx = next((i for i, n in enumerate(normalized) if n in _DOC_ID_TOKENS), None)
    has_repeated_doc_id = False
    if doc_id_idx is not None:
        doc_ids = [r[doc_id_idx] for r in rows if doc_id_idx < len(r)]
        has_repeated_doc_id = len(doc_ids) != len(set(doc_ids))

    def _sum_column(col_idx):
        total = 0.0
        for r in rows:
            if col_idx >= len(r):
                continue
            cleaned = re.sub(r'[^\d.\-]', '', r[col_idx])
            if cleaned in ("", "-", "."):
                continue
            try:
                total += float(cleaned)
            except ValueError:
                continue
        return total

    for i, n in enumerate(normalized):
        if n in _LINE_LEVEL_SUM_TOKENS:
            return record_count, _sum_column(i), header_cells[i]

    if not has_repeated_doc_id:
        for i, n in enumerate(normalized):
            if n in _DOC_LEVEL_SUM_TOKENS:
                return record_count, _sum_column(i), header_cells[i]

    return record_count, None, None




def _is_llm_error_text(text: str) -> bool:
    """True if `text` is one of _llm_call's own swallowed-error strings
    (see the `if not is_json: return f"Sorry, the AI model (...)"` branch
    above) rather than a genuine model response. Used to avoid tacking a
    "Generated by DEAi from live SAP HANA data" trailer onto an error
    message."""
    return isinstance(text, str) and text.startswith("Sorry, the AI model (")



# ==========================================
# Natural Language Routing (!ask command)
# ==========================================
from modules import database

import json

def _format_schema_for_prompt(schema_map, max_cols_shown=25):
    """
    Turns {TABLE: {"description": str, "columns": [...]}} into a compact,
    readable block for the router prompt. Column lists are capped per
    table so a wide table can't blow Groq's TPM budget the way an earlier
    full-column dump did (see bot_audit.log — 32,696 tokens vs an 8000
    limit). Full column lists are still used, uncapped, for server-side
    validation in database.execute_dynamic_query — this cap only affects
    what the LLM sees, not what's enforced.
    """
    lines = []
    for table_name in sorted(schema_map.keys()):
        info = schema_map[table_name]
        description = info.get("description", "")
        columns = info.get("columns", [])
        shown = columns[:max_cols_shown]
        col_str = ", ".join(shown)
        if len(columns) > len(shown):
            col_str += f", ... ({len(columns) - len(shown)} more not shown)"
        lines.append(f"- {table_name}: {description}\n  Real columns: {col_str}")
    return "\n".join(lines)


def parse_natural_language(user_text, lark_user_id):
    """
    Determines user intent. If the intent requires dynamic data (like custom expenses),
    it generates a strict JSON Query Plan based on the live SAP schema.
    """
    schema_map = database.get_allowed_sap_schema(lark_user_id)
    live_schema = _format_schema_for_prompt(schema_map)
    
    current_year = datetime.date.today().year
    system_prompt = f"""You are an internal SAP Business One reporting engine used by finance and
operations staff to query the company's own SAP data — not a general-purpose chat assistant. The
questions you'll see are ordinary internal business reporting requests: expense reports, invoice
summaries, sales figures. In this company's day-to-day SAP usage, terms like "meal allowance",
"gas allowance", "overtime meals", "reimbursement", and "expense claim" refer to ordinary approved
GL expense categories (see the GL Expense Reports action below) — they are routine accounting
terms here, not payroll or personal financial data about any individual, and questions using them
should be treated as normal reporting requests and answered with the matching JSON action, not
treated as sensitive. If a request is genuinely ambiguous, make your best-effort guess rather than
refusing — but you should still decline (by returning the "error" action shape your caller expects)
if a request is asking for something outside SAP reporting entirely, e.g. individual salary/HR
records, or anything unrelated to this company's SAP B1 data.
    CRITICAL DATE RULE: The current year is {current_year}. Whenever a user specifies a month (e.g., "July" or "July {current_year}") without explicitly stating a past year, always map it to the year {current_year} (e.g., start_date: "{current_year}-07-01", end_date: "{current_year}-07-31"). Never default to a year before this one unless the user explicitly names an earlier year.

    You are a strict SAP Business One Data Architect and Internal Reporting Engine.
Classify the user's question into EXACTLY ONE of the actions below and return ONLY the matching JSON object — no prose, no markdown fences.

1. Current on-hand stock for one specific item code:
{{"action": "stock", "params": {{"item_code": "<ItemCode>"}}}}

2. Sales / revenue over a simple window (today, week, month) or an explicit date range:
{{"action": "sales", "params": {{"period": "today", "start_date": null, "end_date": null}}}}
(period is one of "today", "week", "month", "custom"; only set start_date/end_date, both "YYYY-MM-DD", when period is "custom")

3. Top revenue-generating clients/accounts, ranked:
{{"action": "top_clients", "params": {{"limit": 5}}}}

4. Overdue / past-due open invoices:
{{"action": "aging", "params": {{}}}}

5. General executive summary of recent invoices, no specific filter:
{{"action": "report", "params": {{}}}}

6. General Ledger Expense Reports (Meals, Gas Allowance, Transport, etc.) with optional grouping:
{{"action": "gl_expense_report", "params": {{"expense_type": "gas_allowance", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "group_by": "division"}}}}
- expense_type MUST be a snake_case keyword representing the subject (e.g., "overtime_meals", "training_meals", "gas_allowance", "transport", or "all_expenses").
- group_by is OPTIONAL. If the user asks to group, break down, or view by a dimension, set it to ONE of: "division", "unit", "brand", "vehicle_assignee", "dimension_5", or "project". Otherwise, set to null.

7. Anything else — a keyword search, a specific column set, or a filter the reports above cannot express. Build a query plan against the schema below:
{{"action": "dynamic_query", "query_plan": {{"target_tables": ["<HeaderTable>", "<DetailTable>"], "select_columns": ["<DetailTable>.<TextColumn>", "<DetailTable>.<AmountColumn>"], "filters": ["<HeaderTable>.DocDate >= 'YYYY-MM-DD'", "<HeaderTable>.DocDate <= 'YYYY-MM-DD'"], "search_keywords": ["<variant 1>", "<variant 2>", "..."]}}}}
This shape is illustrative only. Never reuse the placeholder names shown above literally.

CHOOSING target_tables:
- Read every table's description below and match the BUSINESS NATURE of what the user is asking about.
- EMPLOYEE EXPENSE REPORTS, REIMBURSEMENTS, ALLOWANCES, AND CLAIMS belong in the A/P Invoice tables (OPCH/PCH1). 
- CRITICAL: When querying specific types of expenses (like "meals", "travel", or "overtime"), you MUST include BOTH the header table (OPCH) and the detail table (PCH1). Headers often only say generic things like "Liquidation", while the actual item (e.g. "Overtime Meal") lives on the PCH1 detail line. 

KEYWORD GENERATION RULES for search_keywords:
- You must generate variants (synonyms, common abbreviations) of the EXACT SUBJECT the user asked for.
- FATAL ERROR: NEVER include broad, generic accounting words ALONE (e.g., do NOT generate "allowance", "expense", "reimbursement", "transport", "petty cash", or "claim" by themselves). 
- If you use an accounting term, it MUST be paired with the specific subject (e.g., "meal allowance" or "meal claim" is valid; "allowance" alone is forbidden, as it will pull in non-meal data).
- Keep keywords highly targeted so they do not accidentally overlap with unrelated business expenses.

AVAILABLE SAP TABLES & COLUMNS FOR dynamic_query:
{live_schema}

RULES:
- Prefer actions 1-7 whenever the request clearly matches them.
- Use dynamic_query (action 7) ONLY when none of actions 1-6 fit.
- Never guess tables or columns not present in the schema above.
- Every column name in select_columns and filters MUST be copied verbatim from that table's "Real columns" list above.
- Output raw JSON only, matching exactly one of the shapes above.
"""
    
    raw_response = _llm_call(user_text, system_prompt=system_prompt, is_json=True, temperature=config.ROUTER_LLM_TEMPERATURE)
    
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        config.audit_log.error(f"Failed to parse LLM JSON: {raw_response}")
        return {"action": "error", "error": "llm_failure"}

    if not isinstance(parsed, dict):
        config.audit_log.error(f"LLM returned non-dict JSON: {raw_response}")
        return {"action": "error", "error": "llm_failure"}

    return parsed