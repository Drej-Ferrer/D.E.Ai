---
name: prefix-message-router
description: Routes !Sap, !Group, !Lark chat commands to router.py and returns its real output
version: 1.1.1
metadata:
  hermes:
    tags: [sap, lark, integration]
    category: integration
---

# Prefix Message Router

## When to Use
Use this skill whenever an incoming chat message starts with one of these exact prefixes: `!Sap`, `!Group`, or `!Lark`. Apply it to every such message. Do not use this skill for messages without one of these prefixes, even if they mention sales, customers, or inventory in passing — `!Group` routes to Lark group chat handling, `!Lark` routes to Lark Base, and `!Sap` routes to SAP data; keeping the prefix required keeps these three clearly separated. Run the router and relay its returned output; do not invent results or replace a real result with a generic acknowledgment.

## Quick reference
- Script: `C:\Users\estrada_alyssa\.hermes\skills\prefix-message-router\scripts\router.py`
- Command (run exactly as one line, do not split into cd + separate run steps, do not search for the file first — the path above is correct and final):
  `cd /d "C:\Users\estrada_alyssa\.hermes\skills\prefix-message-router\scripts" && python router.py <argument>`
- `<argument>` is the message text with the leading prefix removed.

## Procedure
1. Detect the prefix at the start of the message (`!Sap`, `!Group`, or `!Lark`). If none of these prefixes is present, do not use this skill.
2. Strip the prefix and any leading whitespace from the message, leaving only the remainder as `<argument>`. Example: `!Sap OCRD` becomes `OCRD`. `!Sap who are our top customers?` becomes `who are our top customers?`. The remainder is passed through as-is — `router.py` recognizes both short aliases (`OCRD`, `OITM`, etc.) and natural-language phrasing on its own.
3. Using your terminal tool, run exactly this command, substituting only `<argument>`:
   `cd /d "C:\Users\estrada_alyssa\.hermes\skills\prefix-message-router\scripts" && python router.py <argument>`
   Do not use search_files, do not guess an alternate path, and do not omit the `cd /d "..." &&` portion — the terminal tool does not start in this directory by default.
4. Wait for the command to finish and capture its complete stdout.
5. Relay the router output. SAP records are returned as a Markdown executive report with a table, record count, numeric metrics when available, and an actionable note.
6. For temporary SAP or Lark connection failures, allow the router's bounded exponential-backoff retries to complete. If all attempts fail, relay the router's concise user-safe message rather than raw driver, HTTP, or stack-trace details.

## SAP Requests
Short aliases are supported: `OCRD`, `OCRD_SEARCH <name>`, `OITM`, and `SALES_SUMMARY`.

Natural-language requests are translated to fixed, read-only queries. Supported data is limited to:
- `OCRD`: `CardCode`, `CardName`, `CardType`, `Balance`
- `OITM`: `ItemCode`, `ItemName`, `OnHand`, `ItsmGrpCod`
- `OINV`: `DocNum`, `CardCode`, `DocDate`, `DocTotal`

Generated queries use the configured schema and `TOP` row cap, which defaults to 50. Examples include `!Sap Give me the summary of sales for the month`, `!Sap Show me our top customers`, and `!Sap List our inventory items`.

## Pitfalls
- Do not reply with a "processing" or acknowledgment message and stop there — always follow through with the real command output in the same turn.
- Do not include the prefix (`!Sap`, `!Group`, `!Lark`) in the argument passed to `router.py` — it expects only the remainder text.
- Do not expose credentials, raw SQL errors, HTTP response bodies, or stack traces in chat.
- Do not claim a query succeeded when the router returns a failure message.
- Do not bypass the approved natural-language/table/column restrictions. Raw `SELECT` is available only when `ALLOW_RAW_SAP_SQL=true` and is still passed through the router's safety filter.
- Do not send a second command manually while the router is retrying a temporary failure.

## Proactive Conversational Follow-ups
When relaying SAP records, you must act as a proactive data analyst. After presenting the Markdown table and metrics, do not just end the message. You must dynamically suggest 1 or 2 relevant follow-up tasks the user could do next based on the data presented. For example, if displaying sales invoices, ask: *'Would you like me to pull the top customers for this period, or check the inventory status for specific items?'* Keep the tone natural, helpful, and conversational.

## Verification
A correct reply contains the router's real output: a Markdown executive report, a group listing, a Lark response, a usage message, or a concise user-safe failure message. The host UI may show a `Thinking...` state while the router's retries are in progress; that presentation state is managed outside this skill.