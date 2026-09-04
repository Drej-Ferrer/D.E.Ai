"""
Everything that turns a Markdown reply string into a Lark interactive
card (tables, VChart bar charts, plain text) and sends it back to the user.
"""

import json
import re

import lark_oapi as lark

from modules import config

# --- Lark Message Formatting Helpers ---
_TABLE_ROW_RE = re.compile(r'^\s*\|(.+)\|\s*$')
_TABLE_SEP_CELL_RE = re.compile(r'^:?-{1,}:?$')

# A cell counts as "numeric" only if, once currency symbols/commas/% are
# stripped, what's left is a plain number — NOT just "contains a digit
# somewhere". This is what keeps something like an item name ("15-inch
# Laptop Pro") from being mistaken for a value column.
_NUMERIC_CELL_RE = re.compile(r'^[\s$€£¥₱]*-?[\d,]+\.?\d*\s*%?\s*$')

# Column-name tokens used to rank which numeric column is the real
# chartable value vs. an identifier that merely happens to be all-digits.
# See _build_vchart_spec's value-column selection.
_VALUE_LIKE_TOKENS = {"total", "amount", "revenue", "value", "price", "sum",
                       "linetotal", "outstanding", "cost", "balance", "count"}
_ID_LIKE_TOKENS = {"docnum", "docentry", "itemcode", "cardcode", "id", "code",
                    "number", "no", "num", "entry"}


def _split_row(line):
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def _is_separator_row(cells):
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells)


def _is_numeric_cell(val):
    return bool(_NUMERIC_CELL_RE.match((val or "").strip()))


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
            if joined.strip():
                segments.append(("text", _md_inline_to_lark(joined)))
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


def _build_vchart_spec(columns, rows):
    """
    Automatically converts an extracted Markdown table into a Lark VChart JSON spec.
    Returns None when charting wouldn't add value: fewer than 2 rows (a single-bar
    chart says nothing a table doesn't already), or no genuinely numeric column found.
    """
    if len(columns) < 2 or len(rows) < 2:
        return None

    # 1. Pick the X-Axis (Label): First text column, skipping "Rank" or "ID"
    label_col_idx = 0
    for idx, col in enumerate(columns):
        if col.lower().strip() not in ["rank", "id", "no.", "#"]:
            label_col_idx = idx
            break

    label_col_key = f"col_{label_col_idx}"
    label_col_name = columns[label_col_idx][:20]  # VChart prefers shorter field names

    # 2. Pick the Y-Axis (Value): among columns after the label whose cells
    # are ACTUALLY numeric (not just "contains a digit somewhere" — that
    # misfires on things like "15-inch Laptop Pro"). Sample a few rows, not
    # just row 0, in case the first row happens to have a blank/odd cell.
    #
    # Being numeric isn't enough on its own — document numbers, item
    # codes, and other identifiers are ALSO all-digits and would satisfy a
    # pure numeric check. Picking "first numeric column left-to-right"
    # previously grabbed something like DocNum before ever reaching the
    # real amount column (e.g. LineTotal), plotting document numbers as
    # if they were peso amounts. So candidates are ranked by what their
    # column NAME suggests: an amount/total-sounding name wins; a plain
    # numeric column with no such hint is used only if nothing better
    # exists; an identifier-sounding name is never charted at all, even
    # as a last resort — a chart of DocNum values is worse than no chart.
    sample_rows = rows[: min(5, len(rows))]
    numeric_candidates = []
    for idx in range(label_col_idx + 1, len(columns)):
        col_key = f"col_{idx}"
        if all(_is_numeric_cell(r.get(col_key, "")) for r in sample_rows if r.get(col_key, "").strip()):
            numeric_candidates.append(idx)

    if not numeric_candidates:
        return None  # nothing genuinely numeric to plot — don't guess and mislabel

    def _name_tokens(col_name):
        return set(re.findall(r'[a-z0-9]+', col_name.lower()))

    def _rank(idx):
        tokens = _name_tokens(columns[idx])
        if tokens & _VALUE_LIKE_TOKENS:
            return 0  # looks like an amount/total — best candidate
        if tokens & _ID_LIKE_TOKENS:
            return 2  # looks like an identifier — never chart this
        return 1  # ambiguous numeric column — usable, but not preferred

    best_idx = min(numeric_candidates, key=lambda i: (_rank(i), i))
    if _rank(best_idx) == 2:
        return None  # only identifier-shaped numeric columns available — nothing meaningful to plot

    value_col_idx = best_idx

    value_col_key = f"col_{value_col_idx}"
    value_col_name = columns[value_col_idx][:20]

    # 3. Clean the data and SUM by label. Raw query results are often
    # line-level (e.g. several PCH1/INV1 lines sharing one DocDate or one
    # invoice), so plotting one bar per row produces a cluster of
    # near-duplicate bars per date instead of one meaningful total -- that
    # jagged, hard-to-read shape is what a naive per-row chart looks like
    # even on entirely correct data. Summing every row that shares a label
    # gives one bar per distinct label (e.g. one bar per date) with its
    # true total, and is a no-op for already-aggregated queries (e.g.
    # top-clients, where every CardName is already unique) since a group
    # of one just sums to itself.
    totals_by_label = {}
    label_order = []
    for row in rows:
        label = row.get(label_col_key, "Unknown")
        val_str = row.get(value_col_key, "0")

        cleaned = re.sub(r'[^\d.-]', '', val_str)
        try:
            numeric_val = float(cleaned) if cleaned else 0
        except ValueError:
            numeric_val = 0

        if label not in totals_by_label:
            totals_by_label[label] = 0.0
            label_order.append(label)
        totals_by_label[label] += numeric_val

    if len(label_order) < 2:
        return None  # every row shared one label -- a single-bar chart says nothing a table doesn't

    values = []
    for label in label_order:
        display_label = label[:13] + ".." if len(label) > 15 else label
        values.append({label_col_name: display_label, value_col_name: totals_by_label[label]})

    # 4. Return the VChart JSON structure
    return {
        "type": "bar",
        "data": [{"id": "barData", "values": values}],
        "xField": label_col_name,
        "yField": value_col_name
    }


def build_card(reply_text, title="SAP HANA Bot"):
    segments = _parse_markdown_segments(reply_text)
    elements = []

    for segment in segments:
        if segment[0] == "text":
            elements.append({"tag": "markdown", "content": segment[1]})
        else:
            _, columns, rows = segment

            # --- 1. Chart injection (Uses ALL rows for an accurate chart) ---
            chart_spec = _build_vchart_spec(columns, rows)
            if chart_spec:
                elements.append({
                    "tag": "chart",
                    "aspect_ratio": "16:9",
                    "chart_spec": chart_spec
                })

            # --- 2. Table injection (Caps at 100 rows to prevent Lark crash) ---
            MAX_UI_ROWS = 100
            display_rows = rows[:MAX_UI_ROWS]
            
            elements.append({
                "tag": "table", "page_size": 10, "row_height": "low",
                "columns": [{"name": f"col_{idx}", "display_name": name, "data_type": "text", "width": "auto"} for idx, name in enumerate(columns)],
                "rows": display_rows
            })
            
            if len(rows) > MAX_UI_ROWS:
                elements.append({
                    "tag": "markdown", 
                    "content": f"_* Note: Table visually truncated to {MAX_UI_ROWS} rows to fit chat limits. The total above and chart reflect all {len(rows)} records._"
                })

    if not elements:
        elements = [{"tag": "markdown", "content": reply_text or " "}]

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "body": {"elements": elements}
    }


# --- Lark Send Helpers ---
def _client():
    return lark.Client.builder().app_id(config.APP_ID).app_secret(config.APP_SECRET).domain(config.LARK_DOMAIN).build()


def send_lark_reply(message_id, reply_text, title="SAP HANA Bot"):
    """Replies in-thread to the triggering message_id (shows as 'N replies' in Lark)."""
    client = _client()
    card = build_card(reply_text, title=title)
    req = lark.im.v1.ReplyMessageRequest.builder().message_id(message_id).request_body(
        lark.im.v1.ReplyMessageRequestBody.builder().content(json.dumps(card)).msg_type("interactive").build()
    ).build()
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        print(f"!!! Failed to send Lark reply: code={resp.code} msg={resp.msg} log_id={resp.get_log_id()} !!!")
    else:
        print("Reply sent to Lark successfully!")


def send_lark_text(message_id, text_content):
    """Sends a simple text reply to act as a loading indicator, threaded under the triggering message."""
    client = _client()
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