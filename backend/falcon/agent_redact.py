"""
agent_redact.py — the hard boundary between the tool layer and the reader.

The watcher works by having the model write its commands *into the reply*:

    [AGENT: library_store]
    Title: Chapter Three
    ---
    <body>
    [/AGENT]

and by writing each tool's answer back into the conversation as another
message. Both halves are machine traffic. Left alone they reach the user
verbatim — command blocks mid-sentence, then result dumps carrying storage
tables, process ids, database names, latencies and probe output. The reader is
not the audience for any of it.

This module is the single place that decides what the reader sees, and it is
applied unconditionally on the way out. Nothing here is optional and nothing
here reads a setting: the persona is written by a model that can be talked into
anything, so a filter it could ask to skip is not a filter. The tool layer still
runs exactly as emitted and the model still receives the full result — only the
outbound surface is cut down.

The contract, in full:

* every ``[AGENT: ...]...[/AGENT]`` block is removed from the reply
* a write that verified gets **one line** — ``Proof: <title> - id: <doc_...>``
* a failure gets **one plain sentence** — ``Storage failed.``
* everything else from the tool layer is silent

Two entry points, deliberately sharing one implementation so a live stream and a
stored message can never disagree about what a marker is:

``StreamRedactor``        incremental, for tokens as they arrive
``strip_command_blocks``  whole strings — stored messages, history on read
"""
from __future__ import annotations

import re

# Mirrors falcon.watcher's own parser. Kept as its own copy rather than imported
# so redaction has no import-time dependency on the watcher: the filter must
# still work on a message whose watcher never ran.
_OPEN_RE = re.compile(r"\[(?:AGENT|ACTION)\s*:\s*([^\]]+)\]", re.IGNORECASE)
_CLOSE_RE = re.compile(r"\[/(?:AGENT|ACTION)\]", re.IGNORECASE)

# The wrapper the watcher puts around an injected result.
_RESULT_DELIM_RE = re.compile(r"\[/?AGENT RESULT\]", re.IGNORECASE)

# Every spelling a tag can start with. Used to decide whether a dangling "["
# at the end of the buffer might still grow into a marker. The result
# delimiters are here so the streaming and whole-string paths agree on every
# input — a model that writes "[AGENT RESULT]" into its own reply must not have
# it survive live and then vanish on reload.
_TAG_HEADS = (
    "[agent", "[action", "[/agent", "[/action",
    "[agent result]", "[/agent result]",
)

# A tag that never closes its bracket would otherwise hold the stream forever.
# Past this many characters we conclude it is ordinary prose and release it.
_MAX_HOLD = 200


def _viable_partial(text: str) -> bool:
    """True if ``text`` — which starts at a '[' — could still become a tag.

    Two ways it can: the keyword is still being spelled out (``[age``), or the
    keyword is complete but the closing bracket has not arrived (``[AGENT: lib``).
    """
    if len(text) > _MAX_HOLD:
        return False
    low = text.lower()
    if any(head.startswith(low) for head in _TAG_HEADS):
        return True
    return any(low.startswith(head) for head in _TAG_HEADS) and "]" not in low


class StreamRedactor:
    """Removes command blocks from a token stream without ever emitting a partial tag.

    Text is released as soon as it is provably outside a block. When the tail of
    the buffer could still turn into ``[AGENT:`` it is held back until the next
    token settles the question — which is the whole point: a redactor deciding
    per token would emit ``[AGE`` and only then notice.

    Usage::

        r = StreamRedactor()
        for tok in stream:
            visible = r.feed(tok)
            if visible:
                emit(visible)
        emit(r.flush())
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_block = False

    def feed(self, chunk: str) -> str:
        """Absorb a chunk; return the portion that is safe to show now."""
        if chunk:
            self._buf += chunk
        return self._drain(final=False)

    def flush(self) -> str:
        """Release whatever is left at end of stream.

        An unterminated block stays suppressed: the model opened a command and
        never closed it, and the watcher will run it as a legacy open-only
        marker, so it is machine traffic either way.
        """
        out = self._drain(final=True)
        self._buf = ""
        return out

    # -- internals ---------------------------------------------------------

    def _tail_partial(self) -> str:
        """The trailing fragment worth keeping while inside a block."""
        i = self._buf.rfind("[")
        if i == -1:
            return ""
        tail = self._buf[i:]
        return tail if _viable_partial(tail) else ""

    def _drain(self, final: bool) -> str:
        out: list[str] = []

        while self._buf:
            if self._in_block:
                close = _CLOSE_RE.search(self._buf)
                nxt = _OPEN_RE.search(self._buf)

                if close and (nxt is None or close.start() < nxt.start()):
                    self._buf = self._buf[close.end():]
                    self._in_block = False
                    continue
                if nxt:
                    # Legacy open-only block, ended by the next opener.
                    self._buf = self._buf[nxt.start():]
                    self._in_block = False
                    continue

                # Undecided. Everything seen so far is block body — drop it, but
                # keep a tail that might be the start of the closing tag.
                self._buf = "" if final else self._tail_partial()
                return "".join(out)

            # Outside a block.
            i = self._buf.find("[")
            if i == -1:
                out.append(self._buf)
                self._buf = ""
                break

            out.append(self._buf[:i])
            self._buf = self._buf[i:]

            m = _OPEN_RE.match(self._buf)
            if m:
                self._buf = self._buf[m.end():]
                self._in_block = True
                continue

            m = _CLOSE_RE.match(self._buf) or _RESULT_DELIM_RE.match(self._buf)
            if m:
                # A stray closer with no opener, or a result delimiter. Both are
                # wrapper rather than content — drop them rather than show them.
                self._buf = self._buf[m.end():]
                continue

            if not final and _viable_partial(self._buf):
                return "".join(out)   # hold; the next token decides

            # Ordinary bracket. Release it and keep scanning after it.
            out.append("[")
            self._buf = self._buf[1:]

        return "".join(out)


def _tidy(text: str) -> str:
    """Close the gap a removed block leaves behind."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_command_blocks(text: str) -> str:
    """Remove every command block from a complete string.

    Shares ``StreamRedactor``'s state machine so a message reads the same
    whether it was filtered live or on the way out of the database — the two are
    the same function, differing only in whether the whole string is available
    at once.
    """
    if not text:
        return text or ""
    r = StreamRedactor()
    return _tidy(r.feed(text) + r.flush())


# ---------------------------------------------------------------------------
# Result condensation
# ---------------------------------------------------------------------------

# A staged tweet is not tool output — it is the user's own words waiting for
# their approval, and the marker is what the chat UI turns into Post / Reject
# buttons. It passes through whole; removing it would remove the only control
# the human has over posting.
_TWEET_CONFIRM_RE = re.compile(r"\[\[TWEET_CONFIRM:[0-9a-f]{4,8}\]\]", re.IGNORECASE)

# Storage ids as documents_store mints them.
_DOC_ID_RE = re.compile(r"\bdoc_[0-9a-f]{8,32}\b", re.IGNORECASE)

# A write that has been read back and verified says so. Only that earns a proof
# line — a read or a listing also contains ids, and reporting one of those as
# proof of a write would be the single lie the user cannot check for themselves.
_STORED_RE = re.compile(r"\*\*(?:ALREADY\s+)?STORED\*\*", re.IGNORECASE)

_TITLE_RE = re.compile(r"^\s*[-*]?\s*\*\*Title:\*\*\s*(.+?)\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)

# Delimiters, duplicated from falcon.watcher for the same reason the marker
# regexes above are: this module must not depend on the watcher to do its job.
_RESULT_OPEN = "[AGENT RESULT]"
_RESULT_CLOSE = "[/AGENT RESULT]"

# ---------------------------------------------------------------------------
# Commands whose result belongs to the reader rather than to the model
# ---------------------------------------------------------------------------
# Every other tool answers a question the model then acts on, so its result goes
# into the payload and nothing goes on screen. These are the reverse: what comes
# back is a document, which the person asked to see and which the model has no
# reason to hold.
#
# The deciding constraint is size. A stored document can be far larger than the
# context window, so putting one in the payload does not merely waste tokens — it
# pushes the conversation out of the window, or the request over the model's
# limit and the turn fails outright. The user is the one who wanted the document;
# giving it to them directly costs nothing and cannot overflow anything.
#
# The model is told, in as many words, that it has not read what it delivered.
# That matters more than the tokens saved: a model that knows it ran
# read_document but holds no text is in the perfect position to invent some.
READER_FACING_COMMANDS = frozenset({"read_document"})


def is_reader_facing(command: str) -> bool:
    """True if this command's result is shown to the user and withheld from the model."""
    return (command or "").strip().lower() in READER_FACING_COMMANDS

# How a tool says it failed.
_FAILURE_PREFIXES = ("[ERROR]", "[NOT CONFIGURED]")
_FAILURE_MARKERS = ("NOT STORED", "NOT DELETED")

# One sentence per command, in the user's terms. No exception type, no stack, no
# payload echo, no id — a failure hands back nothing that could be mistaken for
# a receipt.
_FAILURE_SENTENCES = {
    "library_store": "Storage failed.",
    "delete_doc": "Delete failed.",
    "read_document": "Could not read that document.",
    "list_documents": "Could not list the library.",
    "memory_bridge": "Memory check failed.",
    "persistent_memory_access_bridge": "Memory check failed.",
    "memory_status": "Memory check failed.",
    "post_tweet": "Could not stage the post.",
    "fetch_replies": "Could not fetch replies.",
    "research": "Research failed.",
    "http_get": "The request failed.",
    "spawn_agent": "Could not create that agent.",
}
_GENERIC_FAILURE = "That action failed."


def _strip_markdown(text: str) -> str:
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def failure_sentence(command: str) -> str:
    """The single sentence shown when ``command`` failed."""
    return _FAILURE_SENTENCES.get((command or "").strip().lower(), _GENERIC_FAILURE)


def is_failure(result_text: str) -> bool:
    text = (result_text or "").lstrip()
    if text.startswith(_FAILURE_PREFIXES):
        return True
    head = text[:200].upper()
    return any(marker in head for marker in _FAILURE_MARKERS)


def condense_result(command: str, result_text: str) -> str:
    """Reduce one tool result to what the reader is allowed to see.

    Returns the whole user-visible text for this result — a proof line, a
    failure sentence, an approval card, or the empty string, which means the
    result is not shown at all. The full text is kept elsewhere for the model;
    this decides the surface only.
    """
    text = (result_text or "").strip()
    if not text:
        return ""

    # Approval card — a human decision, not tool output.
    if _TWEET_CONFIRM_RE.search(text):
        return text

    if is_failure(text):
        return failure_sentence(command)

    # A document the user asked to see. It is the one result shown in full —
    # they requested it, and it is going to them instead of into the payload.
    if is_reader_facing(command):
        return text

    # A verified write is the one success worth a line, because the user has no
    # other way to confirm their text is really retrievable.
    if _STORED_RE.search(text):
        doc = _DOC_ID_RE.search(text)
        if doc:
            title_m = _TITLE_RE.search(text)
            title = _strip_markdown(title_m.group(1)) if title_m else ""
            return (
                f"Proof: {title} — id: {doc.group(0)}" if title
                else f"Proof: id: {doc.group(0)}"
            )

    # Everything else — listings, status probes, pings — is plumbing.
    return ""


def model_result_view(command: str, raw_block: str) -> str:
    """What the model is given for one stored result block.

    Normally the block whole: it asked, and it has to reason over the answer.

    For a reader-facing command the body is replaced by a note saying the
    document went to the user. This is the only place a result is withheld from
    the model, and the note is written to be read as an instruction rather than
    as an absence — it says the text was never provided and that the model has
    not read it, because the alternative failure is a model that remembers
    running the command, finds no text, and fills the gap from imagination.

    The title and id are kept. They are two short lines, they let the model refer
    to the document it just delivered by name, and having them is what makes
    "ask the user for the passage you need" a usable instruction rather than a
    shrug.
    """
    if not is_reader_facing(command) or not raw_block:
        return raw_block

    heading = _HEADING_RE.search(raw_block)
    doc = _DOC_ID_RE.search(raw_block)
    identified = ""
    if heading:
        identified = f" — {_strip_markdown(heading.group(1))}"
    if doc:
        identified += f" ({doc.group(0)})"

    return (
        f"{_RESULT_OPEN}\n"
        f"(from: {command.strip()})\n"
        f"The document{identified} was delivered straight to the user. Its text was "
        "not included here, because a document can be larger than your context.\n"
        "You have not read it. Do not summarise, quote, describe or characterise "
        "its contents, and do not say what it says. If you need a passage to "
        "answer something, ask the user to paste the part that matters.\n"
        f"{_RESULT_CLOSE}"
    )
