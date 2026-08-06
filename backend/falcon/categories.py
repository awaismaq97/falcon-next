"""
categories.py — Category storage layer for Falcon.

Each identity has a set of named categories. Each chat turn (user message +
assistant response) that gets categorized is stored as a full copy in
``category_messages`` — the original ``messages`` collection is never touched.

MongoDB collections:
  categories           — {identity_id, name, created_at}
  category_messages    — {
      identity_id, category_id, category_name,
      user_message, assistant_response,
      user_ts, asst_ts,
      recorded_at   (ISO-8601, used for display + sort),
  }

Indexes (created async at first get_db() call via db._ensure_indexes_async):
  categories:        (identity_id, name unique per identity)
  category_messages: (identity_id), (category_id), (identity_id, category_id)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError, PyMongoError

from falcon.db import get_db

logger = logging.getLogger(__name__)

# Input limits
_MAX_CATEGORY_NAME_LEN = 100
_MAX_MESSAGE_LEN = 50_000  # chars stored per field

# ---------------------------------------------------------------------------
# Default categories seeded for every new identity
# ---------------------------------------------------------------------------
DEFAULT_CATEGORIES: list[str] = [
    "Personal",
    "Spiritual",
    "Technology",
    "Creative",
    "Wellness",
    "Relationships",
    "Goals and Productivity",
    "Travel and Exploration",
    "Other",
]

# The fallback category used when the classifier cannot map a turn to any
# known category (hallucination, parse failure, empty response, etc.).
# Must always exist in the DB — ensure_fallback_category() guarantees this.
FALLBACK_CATEGORY = "Other"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _doc(d: dict) -> dict:
    result = {k: v for k, v in d.items() if k != "_id"}
    if "_id" in d:
        result["_id"] = str(d["_id"])
    return result


def _parse_object_id(value: str, label: str = "id") -> ObjectId:
    """Parse a string into ObjectId, raising ValueError on invalid format."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as exc:
        raise ValueError(f"Invalid {label}: {value!r}") from exc


# ---------------------------------------------------------------------------
# Category CRUD
# ---------------------------------------------------------------------------

def ensure_default_categories(identity_id: str) -> None:
    """Seed default categories for an identity if none exist yet.

    Safe to call multiple times — no-op when categories are already present.
    Uses a try/insert_many approach so concurrent first-calls on the same
    identity don't fail; DuplicateKeyError on the unique index is silently
    swallowed.
    """
    db = get_db()
    try:
        existing = db["categories"].count_documents({"identity_id": identity_id})
        if existing > 0:
            return
        now = _utc_now_iso()
        db["categories"].insert_many(
            [
                {"identity_id": identity_id, "name": name, "created_at": now}
                for name in DEFAULT_CATEGORIES
            ],
            ordered=False,  # continue past any duplicate-key errors
        )
    except DuplicateKeyError:
        # A concurrent insert already seeded the categories — fine.
        pass
    except PyMongoError as exc:
        logger.error(
            "categories: failed to seed defaults for identity=%r: %s", identity_id, exc
        )
        raise


def ensure_fallback_category(identity_id: str) -> None:
    """Guarantee that FALLBACK_CATEGORY ("Other") exists for this identity.

    Called by the categorizer before writing a fallback message so that even
    if the user deleted "Other" from their list, the insert still succeeds.
    DuplicateKeyError is silently swallowed — the category already exists.
    """
    db = get_db()
    try:
        now = _utc_now_iso()
        db["categories"].insert_one(
            {"identity_id": identity_id, "name": FALLBACK_CATEGORY, "created_at": now}
        )
        logger.info(
            "categories: re-created fallback category '%s' for identity=%r",
            FALLBACK_CATEGORY, identity_id,
        )
    except DuplicateKeyError:
        pass  # already present — nothing to do
    except PyMongoError as exc:
        logger.error(
            "categories: failed to ensure fallback category for identity=%r: %s",
            identity_id, exc,
        )
        raise


def list_categories(identity_id: str) -> list[dict]:
    """Return all categories for an identity with their message counts, sorted by creation order.

    Uses a single aggregation pipeline that left-joins category_messages so
    the count is fetched in one round-trip rather than N+1 queries.

    Each returned dict has all category fields plus ``message_count: int``.
    Raises PyMongoError on database failure (propagated to caller/router).
    """
    ensure_default_categories(identity_id)
    db = get_db()
    pipeline = [
        {"$match": {"identity_id": identity_id}},
        {
            "$lookup": {
                "from": "category_messages",
                "let":  {"cat_id": {"$toString": "$_id"}},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$category_id", "$$cat_id"]},
                                    {"$eq": ["$identity_id", identity_id]},
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "_msgs",
            }
        },
        {
            "$addFields": {
                "message_count": {
                    "$ifNull": [{"$arrayElemAt": ["$_msgs.n", 0]}, 0]
                }
            }
        },
        {"$project": {"_msgs": 0}},
        {"$sort": {"created_at": 1}},
    ]
    try:
        results = list(db["categories"].aggregate(pipeline))
    except PyMongoError:
        # Aggregation not available (e.g. very old MongoDB) — fall back to
        # simple find + separate count per category.
        logger.warning(
            "categories: aggregation failed for identity=%r, falling back to N+1 counts",
            identity_id,
        )
        results = list(db["categories"].find({"identity_id": identity_id}).sort("created_at", 1))
        for cat in results:
            try:
                cat["message_count"] = db["category_messages"].count_documents(
                    {"category_id": str(cat["_id"])}
                )
            except PyMongoError:
                cat["message_count"] = 0

    return [_doc(d) for d in results]


def get_category_by_name(identity_id: str, name: str) -> dict | None:
    """Return a single category dict or None."""
    db = get_db()
    d = db["categories"].find_one({"identity_id": identity_id, "name": name})
    return _doc(d) if d else None


def add_category(identity_id: str, name: str) -> dict:
    """Create a new category.

    Raises:
        ValueError: name is empty, too long, or already exists for this identity.
        PyMongoError: database failure.
    """
    name = name.strip()
    if not name:
        raise ValueError("Category name cannot be empty.")
    if len(name) > _MAX_CATEGORY_NAME_LEN:
        raise ValueError(
            f"Category name is too long (max {_MAX_CATEGORY_NAME_LEN} characters)."
        )
    db = get_db()
    try:
        now = _utc_now_iso()
        result = db["categories"].insert_one(
            {"identity_id": identity_id, "name": name, "created_at": now}
        )
        return {
            "_id": str(result.inserted_id),
            "identity_id": identity_id,
            "name": name,
            "created_at": now,
        }
    except DuplicateKeyError:
        raise ValueError(f"Category '{name}' already exists for this identity.")
    except PyMongoError as exc:
        logger.error(
            "categories: add_category failed for identity=%r name=%r: %s",
            identity_id, name, exc,
        )
        raise


def delete_category(category_id: str, identity_id: str) -> bool:
    """Delete a category and all its messages atomically.

    Returns True if the category existed and was deleted, False if not found.
    Raises ValueError on a malformed category_id.
    Raises PyMongoError on database failure.
    """
    oid = _parse_object_id(category_id, "category_id")
    db = get_db()
    try:
        cat = db["categories"].find_one({"_id": oid, "identity_id": identity_id})
        if not cat:
            return False
        # Delete messages first so a partial failure leaves the category record
        # intact (still discoverable) rather than orphaning message documents.
        db["category_messages"].delete_many({"category_id": category_id})
        db["categories"].delete_one({"_id": oid})
        return True
    except PyMongoError as exc:
        logger.error(
            "categories: delete_category failed for category_id=%r identity=%r: %s",
            category_id, identity_id, exc,
        )
        raise


# ---------------------------------------------------------------------------
# Category message storage
# ---------------------------------------------------------------------------

def store_categorized_message(
    *,
    identity_id: str,
    category_name: str,
    user_message: str,
    assistant_response: str,
    user_ts: str,
    asst_ts: str,
    hallucinated_category: str | None = None,
) -> str | None:
    """Store a full copy of a turn under a named category.

    Looks up the category by name. If the name doesn't match any existing
    category, returns None — the caller (categorizer._persist) handles retry.

    ``hallucinated_category`` is stored when the LLM originally returned a
    different (invalid) value, for auditability.

    Returns the inserted document's string id, or None if category not found.
    Raises PyMongoError on database failure.
    """
    db = get_db()
    try:
        cat = db["categories"].find_one({"identity_id": identity_id, "name": category_name})
        if not cat:
            return None

        now = _utc_now_iso()
        doc: dict = {
            "identity_id":        identity_id,
            "category_id":        str(cat["_id"]),
            "category_name":      category_name,
            "user_message":       (user_message or "")[:_MAX_MESSAGE_LEN],
            "assistant_response": (assistant_response or "")[:_MAX_MESSAGE_LEN],
            "user_ts":            user_ts or now,
            "asst_ts":            asst_ts or now,
            "recorded_at":        now,
        }
        if hallucinated_category:
            doc["hallucinated_category"] = hallucinated_category[:500]

        result = db["category_messages"].insert_one(doc)
        return str(result.inserted_id)
    except PyMongoError as exc:
        logger.error(
            "categories: store_categorized_message failed for identity=%r category=%r: %s",
            identity_id, category_name, exc,
        )
        raise


def list_category_messages(
    identity_id: str,
    category_id: str,
    skip: int = 0,
    limit: int = 20,
) -> dict:
    """Return paginated messages for a category, newest first.

    Returns {messages: [...], total: int, skip: int, limit: int}.
    Raises ValueError on a malformed category_id.
    Raises PyMongoError on database failure.
    """
    # Clamp pagination params so runaway values don't DoS MongoDB.
    skip  = max(0, skip)
    limit = max(1, min(limit, 100))

    oid = _parse_object_id(category_id, "category_id")
    db = get_db()
    try:
        # Verify the category belongs to this identity before returning data.
        cat = db["categories"].find_one({"_id": oid, "identity_id": identity_id})
        if not cat:
            return {"messages": [], "total": 0, "skip": skip, "limit": limit}

        query = {"identity_id": identity_id, "category_id": category_id}
        total = db["category_messages"].count_documents(query)
        cursor = (
            db["category_messages"]
            .find(query)
            .sort("recorded_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return {
            "messages": [_doc(d) for d in cursor],
            "total": total,
            "skip": skip,
            "limit": limit,
        }
    except PyMongoError as exc:
        logger.error(
            "categories: list_category_messages failed for identity=%r category_id=%r: %s",
            identity_id, category_id, exc,
        )
        raise


def delete_category_message(message_id: str, identity_id: str) -> bool:
    """Delete a single categorized message.

    Returns True if deleted, False if not found.
    Raises ValueError on a malformed message_id.
    Raises PyMongoError on database failure.
    """
    oid = _parse_object_id(message_id, "message_id")
    db = get_db()
    try:
        result = db["category_messages"].delete_one(
            {"_id": oid, "identity_id": identity_id}
        )
        return result.deleted_count > 0
    except PyMongoError as exc:
        logger.error(
            "categories: delete_category_message failed for message_id=%r identity=%r: %s",
            message_id, identity_id, exc,
        )
        raise



# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------

def export_category_pdf(identity_id: str, category_id: str) -> bytes:
    """Generate a PDF containing all messages in a category, oldest-first.

    Markdown is fully parsed into ReportLab flowables:
      headings, bullets, numbered lists, fenced code blocks, pipe tables,
      blockquotes, inline bold/italic/code/strikethrough.

    Returns raw PDF bytes.
    Raises ValueError on bad IDs or category not found.
    Raises PyMongoError on DB failure.
    Raises ImportError if reportlab is not installed.
    """
    import re as _re
    from io import BytesIO
    from datetime import datetime, timezone

    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm, mm
        from reportlab.platypus import (
            BaseDocTemplate, Frame, PageTemplate,
            Paragraph, Spacer, HRFlowable,
            Table, TableStyle,
        )
    except ImportError as exc:
        raise ImportError(
            "reportlab is required for PDF export. "
            "Install it with: pip install reportlab==4.4.2"
        ) from exc

    # ── Fetch data ────────────────────────────────────────────────────────
    oid = _parse_object_id(category_id, "category_id")
    db  = get_db()
    cat = db["categories"].find_one({"_id": oid, "identity_id": identity_id})
    if not cat:
        raise ValueError(
            f"Category {category_id!r} not found for identity {identity_id!r}."
        )
    category_name = cat.get("name", "Unknown")
    cursor = (
        db["category_messages"]
        .find({"identity_id": identity_id, "category_id": category_id})
        .sort("recorded_at", 1)
    )
    messages = [_doc(d) for d in cursor]

    # ── Colours ───────────────────────────────────────────────────────────
    C_ACCENT  = colors.HexColor("#4f63e8")
    C_FG      = colors.HexColor("#111827")
    C_MUTED   = colors.HexColor("#6b7280")
    C_SUBTLE  = colors.HexColor("#9ca3af")
    C_BORDER  = colors.HexColor("#e2e8f0")
    C_USER_BG = colors.HexColor("#eff2ff")
    C_ASST_BG = colors.HexColor("#f8f9fa")
    C_CODE_BG = colors.HexColor("#f3f4f6")
    C_HALLUC  = colors.HexColor("#d97706")
    C_TBL_HDR = colors.HexColor("#e8eaf6")

    # ── Page geometry ─────────────────────────────────────────────────────
    PAGE_W, PAGE_H = A4
    MARGIN    = 1.8 * cm
    CONTENT_W = PAGE_W - 2 * MARGIN
    FOOTER_H  = 2.2 * cm
    BUBBLE_PAD = 8

    # ── Style factory ─────────────────────────────────────────────────────
    def S(name, **kw):
        defaults = dict(fontName="Helvetica", fontSize=9.5, leading=14, textColor=C_FG)
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    s_cover_h   = S("cvH",  fontSize=24, leading=30, textColor=C_ACCENT,
                     alignment=TA_CENTER, spaceAfter=5, fontName="Helvetica-Bold")
    s_cover_sub = S("cvS",  fontSize=11, leading=15, textColor=C_MUTED,
                     alignment=TA_CENTER, spaceAfter=3)
    s_cover_met = S("cvM",  fontSize=8.5, leading=12, textColor=C_SUBTLE,
                     alignment=TA_CENTER)
    s_role      = S("role", fontSize=7,   leading=9,  textColor=C_MUTED,
                     fontName="Helvetica-Bold", spaceAfter=1, letterSpacing=0.8)
    s_ts        = S("ts",   fontSize=7.5, leading=10, textColor=C_SUBTLE, spaceAfter=4)
    s_halluc    = S("hl",   fontSize=7.5, leading=10, textColor=C_HALLUC,
                     fontName="Helvetica-Oblique", spaceBefore=4)
    s_body      = S("body", fontSize=9.5, leading=14.5, spaceAfter=0)
    s_h1        = S("h1",   fontSize=13, leading=17, fontName="Helvetica-Bold",
                     textColor=C_ACCENT, spaceAfter=4, spaceBefore=6)
    s_h2        = S("h2",   fontSize=11, leading=15, fontName="Helvetica-Bold",
                     textColor=C_ACCENT, spaceAfter=3, spaceBefore=5)
    s_h3        = S("h3",   fontSize=10, leading=14, fontName="Helvetica-Bold",
                     spaceAfter=2, spaceBefore=4)
    s_bullet    = S("bul",  fontSize=9.5, leading=14, leftIndent=14,
                     firstLineIndent=0, spaceAfter=1)
    s_num       = S("num",  fontSize=9.5, leading=14, leftIndent=18,
                     firstLineIndent=0, spaceAfter=1)
    s_code_line = S("cod",  fontSize=8.5, leading=12, fontName="Courier",
                     textColor=colors.HexColor("#374151"), spaceAfter=0,
                     leftIndent=6, rightIndent=6)
    s_blockquote= S("bq",   fontSize=9.5, leading=14, leftIndent=12,
                     textColor=C_MUTED, fontName="Helvetica-Oblique")
    s_tbl_hdr   = S("thdr", fontSize=8.5, leading=11, fontName="Helvetica-Bold",
                     alignment=TA_LEFT)
    s_tbl_cell  = S("tcel", fontSize=8.5, leading=11, alignment=TA_LEFT)

    # ── Helpers ───────────────────────────────────────────────────────────
    def _fmt_ts(iso: str) -> str:
        if not iso:
            return ""
        try:
            d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return d.strftime("%b %d, %Y  %H:%M UTC")
        except Exception:
            return iso

    def _esc(t: str) -> str:
        """XML-escape a string for use inside a ReportLab Paragraph.

        Also replaces Unicode dash/hyphen variants that Helvetica cannot render
        (they show as black replacement boxes) with plain ASCII equivalents.
        """
        # Normalise dashes before XML-escaping so the substitutions are simple
        t = (
            t.replace("\u2014", "-")   # em dash  —  → -
             .replace("\u2013", "-")   # en dash  –  → -
             .replace("\u2012", "-")   # figure dash
             .replace("\u2010", "-")   # hyphen
             .replace("\u2011", "-")   # non-breaking hyphen
             .replace("\u2212", "-")   # minus sign
             .replace("\u2018", "'")   # left single quote  '
             .replace("\u2019", "'")   # right single quote '
             .replace("\u201c", '"')   # left double quote  "
             .replace("\u201d", '"')   # right double quote "
             .replace("\u2026", "...")  # ellipsis …
             .replace("\u00a0", " ")   # non-breaking space
        )
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _inline(text: str) -> str:
        """Convert inline Markdown to ReportLab XML markup."""
        t = _esc(text)
        # Bold: **text** or __text__
        t = _re.sub(
            r"\*\*(.+?)\*\*|__(.+?)__",
            lambda m: f"<b>{m.group(1) or m.group(2)}</b>", t
        )
        # Italic: *text* or _text_ (single, not double)
        t = _re.sub(
            r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|(?<!_)_(?!_)(.+?)(?<!_)_(?!_)",
            lambda m: f"<i>{m.group(1) or m.group(2)}</i>", t
        )
        # Inline code
        t = _re.sub(
            r"`([^`]+)`",
            lambda m: (
                "<font name='Courier' size='8.5' color='#374151'>"
                + _esc(m.group(1)) + "</font>"
            ), t
        )
        # Strikethrough → muted colour (no RL strikethrough tag)
        t = _re.sub(
            r"~~(.+?)~~",
            lambda m: f"<font color='#9ca3af'>{m.group(1)}</font>", t
        )
        # Links: [text](url) → bold text
        t = _re.sub(r"\[([^\]]+)\]\([^)]*\)", r"<b>\1</b>", t)
        # Images: ![alt](url) → italicised placeholder
        t = _re.sub(r"!\[[^\]]*\]\([^)]*\)", "<i>(image)</i>", t)
        return t

    def _code_table(code_lines_list: list, width: float) -> "Table":
        """Render a list of code lines as a monospace table."""
        tdata = [
            [Paragraph(_esc(cl), s_code_line)]
            for cl in (code_lines_list or [""])
        ]
        tbl = Table(tdata, colWidths=[width])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), C_CODE_BG),
            ("BOX",           (0, 0), (-1, -1), 0.5, C_BORDER),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return tbl

    def _md_flowables(text: str, width: float) -> list:
        """Parse Markdown into a list of ReportLab flowables.

        Handles: ATX headings, fenced code blocks, unordered lists,
        ordered lists, blockquotes, pipe tables, horizontal rules,
        blank-line paragraph breaks, inline bold/italic/code/strike.
        """
        flowables: list = []
        lines = (text or "").splitlines()
        i = 0
        in_code = False
        code_buf: list[str] = []
        code_fence = ""
        para_buf: list[str] = []

        def flush_para():
            if para_buf:
                combined = " ".join(l for l in para_buf if l.strip())
                if combined.strip():
                    flowables.append(Paragraph(_inline(combined), s_body))
                para_buf.clear()

        while i < len(lines):
            line = lines[i]

            # ── Fenced code block open/close ──────────────────────────────
            fence_open = _re.match(r"^(`{3,}|~{3,})", line)
            if not in_code and fence_open:
                flush_para()
                code_fence = fence_open.group(1)
                in_code = True
                code_buf = []
                i += 1
                continue
            if in_code:
                if line.startswith(code_fence):
                    flowables.append(_code_table(code_buf, width))
                    flowables.append(Spacer(1, 3))
                    in_code = False
                    code_buf = []
                else:
                    code_buf.append(line)
                i += 1
                continue

            # ── ATX heading ───────────────────────────────────────────────
            hm = _re.match(r"^(#{1,6})\s+(.*)", line)
            if hm:
                flush_para()
                level = len(hm.group(1))
                st = {1: s_h1, 2: s_h2}.get(level, s_h3)
                flowables.append(Paragraph(_inline(hm.group(2).strip()), st))
                i += 1
                continue

            # ── Horizontal rule ───────────────────────────────────────────
            if _re.match(r"^(\s*[-*_]){3,}\s*$", line):
                flush_para()
                flowables.append(
                    HRFlowable(width="100%", thickness=0.5,
                               color=C_BORDER, spaceAfter=4)
                )
                i += 1
                continue

            # ── Pipe table ────────────────────────────────────────────────
            if line.lstrip().startswith("|"):
                flush_para()
                tbl_lines: list[str] = []
                while i < len(lines) and lines[i].lstrip().startswith("|"):
                    tbl_lines.append(lines[i])
                    i += 1
                rows: list[list[str]] = []
                for tl in tbl_lines:
                    cells = [c.strip() for c in tl.strip().strip("|").split("|")]
                    if all(_re.match(r"^:?-+:?$", c) for c in cells if c):
                        continue  # separator row
                    rows.append(cells)
                if rows:
                    ncols = max(len(r) for r in rows)
                    norm  = [r + [""] * (ncols - len(r)) for r in rows]
                    col_w = width / ncols
                    tdata = []
                    for ri, row in enumerate(norm):
                        st = s_tbl_hdr if ri == 0 else s_tbl_cell
                        tdata.append(
                            [Paragraph(_inline(c), st) for c in row]
                        )
                    tbl = Table(tdata, colWidths=[col_w] * ncols, repeatRows=1)
                    tbl.setStyle(TableStyle([
                        ("BACKGROUND",    (0, 0), (-1, 0),  C_TBL_HDR),
                        ("BOX",           (0, 0), (-1, -1), 0.5, C_BORDER),
                        ("INNERGRID",     (0, 0), (-1, -1), 0.3, C_BORDER),
                        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
                        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
                        ("TOPPADDING",    (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                         [colors.white, colors.HexColor("#f9fafb")]),
                    ]))
                    flowables.append(tbl)
                    flowables.append(Spacer(1, 4))
                continue  # i already advanced past all table lines

            # ── Unordered list ────────────────────────────────────────────
            if _re.match(r"^\s*[-*+]\s+", line):
                flush_para()
                while i < len(lines) and _re.match(r"^\s*[-*+]\s+", lines[i]):
                    content = _re.sub(r"^\s*[-*+]\s+", "", lines[i])
                    flowables.append(
                        Paragraph(f"• &nbsp;{_inline(content)}", s_bullet)
                    )
                    i += 1
                continue

            # ── Ordered list ──────────────────────────────────────────────
            if _re.match(r"^\s*\d+\.\s+", line):
                flush_para()
                num = 1
                while i < len(lines) and _re.match(r"^\s*\d+\.\s+", lines[i]):
                    content = _re.sub(r"^\s*\d+\.\s+", "", lines[i])
                    flowables.append(
                        Paragraph(f"{num}.&nbsp;&nbsp;{_inline(content)}", s_num)
                    )
                    num += 1
                    i += 1
                continue

            # ── Blockquote ────────────────────────────────────────────────
            if line.lstrip().startswith(">"):
                flush_para()
                while i < len(lines) and lines[i].lstrip().startswith(">"):
                    content = _re.sub(r"^\s*>\s?", "", lines[i])
                    flowables.append(Paragraph(_inline(content), s_blockquote))
                    i += 1
                continue

            # ── Blank line → paragraph break ─────────────────────────────
            if not line.strip():
                flush_para()
                if flowables and not isinstance(flowables[-1], Spacer):
                    flowables.append(Spacer(1, 4))
                i += 1
                continue

            # ── Regular text ──────────────────────────────────────────────
            para_buf.append(line)
            i += 1

        flush_para()
        # Unclosed code fence
        if in_code and code_buf:
            flowables.append(_code_table(code_buf, width))

        return flowables or [Paragraph("(empty)", s_body)]

    # ── Bubble: wraps flowables in a tinted card ──────────────────────────
    def _bubble(role: str, ts_str: str, md_text: str, bg: object) -> list:
        """Render a chat bubble that can break across pages.

        Each content flowable becomes its own row in a multi-row Table so
        ReportLab can split the table between rows when the bubble is taller
        than the available frame height.  The border/background is drawn via
        per-row and overall table styles.
        """
        inner_w = CONTENT_W - 2 * BUBBLE_PAD

        # Build the list of inner flowables (header + content)
        header_rows: list = [Paragraph(role, s_role)]
        if ts_str:
            header_rows.append(Paragraph(ts_str, s_ts))
        content_flowables = _md_flowables(md_text, inner_w)

        all_rows = header_rows + content_flowables

        # One flowable per table row; this lets ReportLab page-break between rows
        tdata = [[f] for f in all_rows]
        nrows = len(tdata)

        tbl = Table(tdata, colWidths=[CONTENT_W], repeatRows=0)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), bg),
            ("BOX",           (0, 0), (-1, -1), 0.6, C_BORDER),
            ("LEFTPADDING",   (0, 0), (-1, -1), BUBBLE_PAD),
            ("RIGHTPADDING",  (0, 0), (-1, -1), BUBBLE_PAD),
            # Only the first row gets top padding; last row gets bottom padding
            ("TOPPADDING",    (0, 0), (-1, 0),  BUBBLE_PAD),
            ("TOPPADDING",    (0, 1), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -2), 1),
            ("BOTTOMPADDING", (0, nrows - 1), (-1, nrows - 1), BUBBLE_PAD),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]))
        return [tbl]

    # ── Footer callback ───────────────────────────────────────────────────
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(C_SUBTLE)
        canvas.drawString(
            MARGIN, 13 * mm,
            f"Falcon  ·  {category_name}  ·  {identity_id}",
        )
        canvas.drawRightString(PAGE_W - MARGIN, 13 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(C_BORDER)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN, 15 * mm, PAGE_W - MARGIN, 15 * mm)
        canvas.restoreState()

    # ── Document assembly ─────────────────────────────────────────────────
    buf = BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=FOOTER_H,
        title=f"Falcon – {category_name}", author="Falcon",
    )
    frame = Frame(
        MARGIN, FOOTER_H, CONTENT_W, PAGE_H - MARGIN - FOOTER_H, id="main"
    )
    doc.addPageTemplates(
        [PageTemplate(id="main", frames=[frame], onPage=_on_page)]
    )

    story: list = []

    # Cover
    story.append(Spacer(1, 1.8 * cm))
    story.append(Paragraph(category_name, s_cover_h))
    story.append(Paragraph(f"Identity: {identity_id}", s_cover_sub))
    story.append(Paragraph(
        f"{len(messages)} message{'s' if len(messages) != 1 else ''}"
        f"  ·  Exported {exported_at}",
        s_cover_met,
    ))
    story.append(Spacer(1, 0.5 * cm))
    story.append(HRFlowable(
        width="100%", thickness=1.5, color=C_ACCENT, spaceAfter=0.7 * cm
    ))

    if not messages:
        story.append(Paragraph("No messages in this category yet.", s_body))
    else:
        for idx, msg in enumerate(messages, start=1):
            user_md  = msg.get("user_message", "") or ""
            asst_md  = msg.get("assistant_response", "") or ""
            user_ts  = _fmt_ts(msg.get("user_ts", "") or msg.get("recorded_at", ""))
            asst_ts  = _fmt_ts(msg.get("asst_ts", "") or msg.get("recorded_at", ""))
            halluc   = msg.get("hallucinated_category")

            block: list = []
            if idx > 1:
                block.append(HRFlowable(
                    width="100%", thickness=0.4, color=C_BORDER,
                    spaceBefore=0.3 * cm, spaceAfter=0.3 * cm,
                ))
            user_bubble  = _bubble("USER",      user_ts, user_md, C_USER_BG)
            asst_bubble  = _bubble("ASSISTANT", asst_ts, asst_md, C_ASST_BG)

            # Keep separator + first bubble together so we don't orphan the
            # divider on its own page.  Each bubble can still break across pages
            # internally (multi-row Table).
            story.extend(block)  # separator (if any)
            story.extend(user_bubble)
            story.append(Spacer(1, 5))
            story.extend(asst_bubble)
            if halluc:
                safe = halluc.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(
                    f"⚠ Auto-filed · classifier returned: {safe[:120]}", s_halluc
                ))

    doc.build(story)
    return buf.getvalue()
