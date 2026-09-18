"""
doc_summary.py — reduce a document to the few things worth carrying away.

The watcher's Drive tools never put a document's text into the chat. What goes
back is this: compact bullets — the ideas if it argues something, the plot points
if it tells a story, the decisions and figures if it reports. A summary is small
enough to read in the conversation and small enough to sit in the model's context
without pushing the rest of the turn out of the window, which a manuscript is not.

Long documents are summarised map-reduce: each chunk is reduced on its own, then
the chunk summaries are reduced together. One pass over a whole novel would not
fit in a single request, and truncating to whatever did fit would silently
summarise the first chapter and call it the book.

Chunk boundaries are placed at paragraph breaks where one is available nearby,
so a chunk does not begin mid-sentence — a summariser handed a fragment starting
"…and therefore refused" will confidently invent what came before it.

Model: DRIVE_SUMMARY_MODEL, default openai/gpt-4o-mini through OpenRouter — the
same pooled client and the same routing as the research worker, so this shares
its connection pool rather than opening its own.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("falcon.doc_summary")

# ~30k characters is roughly 7–8k tokens: comfortably inside the model's window
# with room for the instructions and the answer, and large enough that a typical
# report or chapter is one chunk rather than three.
CHUNK_CHARS = 30_000

# Look this far back from a chunk boundary for a paragraph break before giving up
# and cutting mid-paragraph.
_BOUNDARY_WINDOW = 2_000

# Bounds cost and latency on something novel-length. At 30k per chunk this covers
# 240k characters, slightly more than documents_store keeps anyway.
MAX_CHUNKS = 8

_SYSTEM = (
    "You reduce a document to the few things a reader would actually want to carry "
    "away from it. You are given the document's text; you return bullets and nothing "
    "else.\n\n"
    "Match the bullets to what the document is:\n"
    "- Narrative or script — the plot points, in order: what happens, to whom, and "
    "what changes as a result.\n"
    "- Argument, essay or report — the claims it makes and what it rests them on, "
    "plus any figures, dates or commitments that matter.\n"
    "- Notes, minutes or a plan — the decisions, the open questions and who owns what.\n"
    "- Reference or data — what it covers, how it is organised, and anything that "
    "stands out in it.\n\n"
    "Rules:\n"
    "- 5 to 12 bullets. One line each, a full sentence, specific.\n"
    "- Name names, quote figures and give dates when the document does. A bullet that "
    "would fit any document is worthless — 'discusses various challenges' says nothing.\n"
    "- Write only what is in the text. Do not infer intent, significance or background "
    "the document does not state, and never fill a gap with something plausible.\n"
    "- If the text is truncated, partial or unreadable, say so as the last bullet "
    "instead of pretending the summary is complete.\n"
    "- No preamble, no closing remark, no heading. Bullets starting with '- ' only."
)

_REDUCE_SYSTEM = (
    "You are given bullet summaries of consecutive sections of one document, in order. "
    "Merge them into a single set of bullets covering the whole document.\n\n"
    "Rules:\n"
    "- 6 to 14 bullets. One line each, a full sentence, specific.\n"
    "- Keep the document's own order, so a narrative still reads as a sequence.\n"
    "- Merge repetition across sections into one bullet; drop anything minor rather "
    "than listing everything.\n"
    "- Keep the specifics — names, figures, dates. Do not generalise them away.\n"
    "- Add nothing that is not in the section summaries.\n"
    "- No preamble, no closing remark, no heading. Bullets starting with '- ' only."
)


def _model() -> str:
    from falcon.google_drive import summary_model

    return summary_model()


def _chat(system: str, user: str, max_tokens: int = 900) -> str:
    import falcon.config as Config
    from falcon.engine import get_client

    client = get_client(Config.OPENROUTER_API_KEY, title="Falcon-DocSummary")
    resp = client.chat.completions.create(
        model=_model(),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # Low but not zero: summarisation at 0.0 tends to lift sentences verbatim
        # rather than condense them.
        temperature=0.2,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def chunk(text: str, size: int = CHUNK_CHARS, limit: int = MAX_CHUNKS) -> list[str]:
    """Split text into at most ``limit`` chunks, preferring paragraph boundaries."""
    text = text or ""
    if len(text) <= size:
        return [text] if text.strip() else []

    chunks: list[str] = []
    start = 0
    while start < len(text) and len(chunks) < limit:
        end = min(start + size, len(text))
        if end < len(text):
            window_start = max(start + size - _BOUNDARY_WINDOW, start + 1)
            split = text.rfind("\n\n", window_start, end)
            if split == -1:
                split = text.rfind("\n", window_start, end)
            if split != -1:
                end = split
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end
    return chunks


# A line the model meant as a list item: a dash, a bullet character, or a number.
_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")


def _tidy(bullets: str) -> str:
    """Normalise whatever the model returned into plain '- ' bullet lines.

    Models drift into numbered lists, headings, "Here are the key points:" and a
    closing offer to elaborate, however firmly the prompt says not to. Left alone
    those become bullets, and the summary opens with a bullet that says nothing.

    The rule: **if the model marked any line as a list item, only marked lines
    survive.** That drops a lead-in and a sign-off in one pass without having to
    recognise either, and it cannot eat a real bullet, because a real bullet is
    marked. When nothing is marked at all the model answered in plain lines, and
    every non-empty line is taken as a bullet instead.

    The cost is a bullet that wrapped onto a second line losing its tail. That is
    rare — the prompt asks for one line each — and a truncated bullet is a smaller
    problem than prose silently presented as findings.
    """
    raw_lines = [ln.strip() for ln in (bullets or "").splitlines()]
    raw_lines = [ln for ln in raw_lines if ln and not ln.startswith("#")]
    if not raw_lines:
        return ""

    marked = [ln for ln in raw_lines if _MARKER_RE.match(ln)]
    keep = marked if marked else raw_lines

    out: list[str] = []
    for line in keep:
        line = _MARKER_RE.sub("", line).strip()
        if line:
            out.append(f"- {line}")
    return "\n".join(out)


def summarize(text: str, *, title: str = "", focus: str = "") -> dict:
    """Reduce a document to bullets. Returns ``{bullets, chunks, truncated, model}``.

    ``focus`` narrows what the summary attends to when the caller asked for
    something particular ("the argument about pricing", "what happens to Mara").
    It steers emphasis and never licenses going beyond the text.

    Raises RuntimeError if the model produced nothing usable, so the caller
    reports a failure rather than an empty summary that reads like a verdict on
    the document.
    """
    text = (text or "").strip()
    if not text:
        raise RuntimeError("there was no text to summarise")

    pieces = chunk(text)
    if not pieces:
        raise RuntimeError("there was no text to summarise")

    # More text than the chunk budget covers. Said out loud rather than quietly
    # summarising the front of the document as though it were the whole thing.
    consumed = sum(len(p) for p in pieces)
    truncated = consumed < len(text) * 0.98

    label = f"DOCUMENT: {title}\n\n" if title else ""
    steer = f"\n\nFOCUS: the reader specifically wants {focus.strip()}.\n" if focus.strip() else ""

    if len(pieces) == 1:
        bullets = _tidy(_chat(_SYSTEM, f"{label}{steer}TEXT:\n{pieces[0]}"))
    else:
        partials: list[str] = []
        for i, piece in enumerate(pieces, start=1):
            part = _tidy(
                _chat(
                    _SYSTEM,
                    f"{label}SECTION {i} of {len(pieces)}{steer}\n\nTEXT:\n{piece}",
                    max_tokens=700,
                )
            )
            if part:
                partials.append(f"SECTION {i}:\n{part}")
            logger.info(
                "doc_summary: section %d/%d of %r summarised", i, len(pieces), title or "document",
            )
        if not partials:
            raise RuntimeError("the summariser returned nothing for any section")
        bullets = _tidy(
            _chat(
                _REDUCE_SYSTEM,
                f"{label}{steer}\n\nSECTION SUMMARIES:\n\n" + "\n\n".join(partials),
                max_tokens=1100,
            )
        )

    if not bullets.strip():
        raise RuntimeError("the summariser returned nothing")

    if truncated:
        bullets += (
            f"\n- **Note:** only the first {consumed:,} of {len(text):,} characters were "
            "summarised — the document is longer than one pass covers."
        )

    return {
        "bullets": bullets,
        "chunks": len(pieces),
        "truncated": truncated,
        "model": _model(),
    }
