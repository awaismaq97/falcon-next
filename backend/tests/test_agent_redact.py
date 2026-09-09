"""
tests/test_agent_redact.py — the outbound filter between the tool layer and the reader.

The property that matters is not "usually strips blocks" but "cannot emit one",
so the streaming tests below re-run every case at several chunk sizes, including
one character at a time. A redactor that only works when a whole block arrives in
a single token is a redactor that leaks in production, where it never does.

Run with:
    conda run -n falcon pytest tests/test_agent_redact.py -v

No database and no network — this module is pure text.
"""
from __future__ import annotations

import os
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from falcon.agent_redact import (  # noqa: E402
    StreamRedactor,
    condense_result,
    failure_sentence,
    strip_command_blocks,
)

# Chunk sizes a stream is replayed at. 1 is the real adversary: it splits every
# tag across as many feeds as it has characters.
CHUNK_SIZES = (1, 2, 3, 5, 13, 10_000)


def stream(text: str, chunk: int) -> str:
    r = StreamRedactor()
    out = [r.feed(text[i:i + chunk]) for i in range(0, len(text), chunk)]
    out.append(r.flush())
    return "".join(out)


def streamed_alike(text: str) -> str:
    """Stream `text` at every chunk size; assert they agree; return the result."""
    results = {n: stream(text, n) for n in CHUNK_SIZES}
    distinct = set(results.values())
    assert len(distinct) == 1, f"chunk size changed the output: {results!r}"
    return distinct.pop()


# ---------------------------------------------------------------------------
# Command blocks never reach the reader
# ---------------------------------------------------------------------------

BLOCK_CASES = [
    # (reply as the model wrote it, what the reader may see)
    (
        "Saving that now.\n\n[AGENT: library_store]\nTitle: Ch3\n---\nbody\n[/AGENT]\n\nDone.",
        "Saving that now.\n\nDone.",
    ),
    ("Before [AGENT: ping][/AGENT] after", "Before  after"),
    ("x [ACTION: echo]hi[/ACTION] y", "x  y"),
    ("two [AGENT: a]p[/AGENT] and [AGENT: b]q[/AGENT] done", "two  and  done"),
    # Legacy open-only form: payload runs to the end, exactly as the watcher reads it.
    ("legacy [AGENT: ping]\npayload runs on", "legacy"),
    # A block that never closes is still suppressed — the watcher will run it.
    ("text [AGENT: library_store]\nunterminated", "text"),
    # Result delimiters are wrapper, not content, on both paths.
    ("[AGENT RESULT]\npong\n[/AGENT RESULT]", "pong"),
]


@pytest.mark.parametrize("raw,visible", BLOCK_CASES)
def test_blocks_are_stripped_whole(raw, visible):
    assert strip_command_blocks(raw) == visible


@pytest.mark.parametrize("raw,_visible", BLOCK_CASES)
def test_blocks_are_stripped_at_every_chunk_size(raw, _visible):
    assert "[AGENT" not in streamed_alike(raw).upper()


@pytest.mark.parametrize(
    "text",
    [
        "array[0] and list[1] are fine",
        "a [AGE that is not a tag",
        "markdown [link](http://x.test) survives",
        "[not a command] at all",
        "[agentic] design is a different word",
    ],
)
def test_ordinary_brackets_survive(text):
    """The filter must not eat prose. A '[' is not a marker."""
    assert streamed_alike(text) == text
    assert strip_command_blocks(text) == text


def test_no_partial_tag_is_ever_emitted():
    """Feeding one character at a time must never show a half-written tag.

    This is the failure the buffering exists to prevent: a redactor that decided
    per token would emit '[', then 'A', then 'G' before it could possibly know.
    """
    raw = "ok [AGENT: library_store]\npayload\n[/AGENT] end"
    r = StreamRedactor()
    seen = ""
    for ch in raw:
        seen += r.feed(ch)
        assert "[A" not in seen.upper()
        assert "[/" not in seen
    seen += r.flush()
    assert seen == "ok  end"


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300))
def test_output_never_contains_a_marker(text):
    """Whatever the model writes, no marker survives to the reader."""
    out = strip_command_blocks(text).upper()
    assert "[AGENT:" not in out.replace(" ", "")
    assert "[/AGENT]" not in out
    assert "[/ACTION]" not in out


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=200), st.integers(min_value=1, max_value=17))
def test_streaming_matches_whole_string(text, chunk):
    """A live stream and a stored message must agree on what a marker is.

    They are filtered by different entry points; if they disagreed, a reply
    would change under the reader on reload.
    """
    from falcon.agent_redact import _tidy

    assert _tidy(stream(text, chunk)) == strip_command_blocks(text)


# ---------------------------------------------------------------------------
# Results are condensed to one line, one sentence, or nothing
# ---------------------------------------------------------------------------

STORED = """**STORED** — written and read back, verified.

- **Storage id:** `doc_a1b2c3d4e5f6`
- **Title:** Chapter Three — The Descent
- **Tags:** manuscript, draft
- **Size:** 12,043 chars
- **Saved:** 2026-09-09 14:02 UTC

Read it back at any time with `read_document doc_a1b2c3d4e5f6`."""

PROBE_DUMP = """**MEMORY BRIDGE** — OK
- db: falcon (cluster atlas-x9k2, pid 4417)
- collections: 14 | messages: 8,201
- probe write+read: 41ms"""


def test_verified_write_gets_one_proof_line():
    out = condense_result("library_store", STORED)
    assert out == "Proof: Chapter Three — The Descent — id: doc_a1b2c3d4e5f6"
    assert "\n" not in out


def test_proof_line_carries_no_metadata():
    out = condense_result("library_store", STORED)
    for leaked in ("Tags", "chars", "UTC", "Saved", "read_document", "**"):
        assert leaked not in out


@pytest.mark.parametrize(
    "command,result,expected",
    [
        ("library_store", "[ERROR] NOT STORED — write failed: ServerSelectionTimeoutError: no primary reachable", "Storage failed."),
        ("delete_doc", "[ERROR] NOT DELETED — no storage id in 'all'.", "Delete failed."),
        ("post_tweet", "[NOT CONFIGURED] post_tweet requires TWITTER_API_KEY.", "Could not stage the post."),
        ("research", "[ERROR] research: provider returned 503", "Research failed."),
        ("nosuchtool", "[ERROR] Unknown command: 'nosuchtool'. Available: ping, echo", "That action failed."),
    ],
)
def test_failure_is_one_plain_sentence(command, result, expected):
    out = condense_result(command, result)
    assert out == expected
    assert "\n" not in out
    # No stack, no payload, no exception type, no id.
    for leaked in ("Error", "[", "doc_", "TIMEOUT", "503", "TWITTER"):
        assert leaked not in out


@pytest.mark.parametrize(
    "command,result",
    [
        ("ping", "pong — watcher alive at 2026-09-09T14:00:00Z"),
        ("memory_status", PROBE_DUMP),
        ("list_documents", "1. doc_aaaaaaaaaaaa — Notes\n2. doc_bbbbbbbbbbbb — Draft"),
        ("fetch_replies", "3 replies found:\n- @a: nice\n- @b: thanks"),
    ],
)
def test_everything_else_is_silent(command, result):
    """Listings, probes and lookups show the reader nothing at all.

    read_document is deliberately not in this list — see the reader-facing
    section at the end of this module.
    """
    assert condense_result(command, result) == ""


def test_a_read_never_masquerades_as_a_proof_of_writing():
    """A listing contains ids too. Only a verified write earns a proof line.

    Reporting a lookup as proof of storage is the one failure the user cannot
    check for themselves, which is why the marker — not the id — is the trigger.
    """
    assert condense_result("list_documents", "doc_a1b2c3d4e5f6 — Notes") == ""


def test_staged_tweet_passes_through_whole():
    """A staged tweet is the user's own words awaiting their decision, and the
    marker is what the UI turns into Post / Reject. Condensing it away would
    remove the only control the human has over posting."""
    staged = "NOT POSTED — waiting for your approval.\n\nhello world\n\n[[TWEET_CONFIRM:a1b2]]"
    assert condense_result("post_tweet", staged) == staged


def test_failure_sentence_falls_back_for_unknown_commands():
    assert failure_sentence("something_spawned_at_runtime") == "That action failed."
    assert failure_sentence("") == "That action failed."


# ---------------------------------------------------------------------------
# The history split: what the model gets back vs what the reader sees
# ---------------------------------------------------------------------------
# `_apply_agent_redaction` is pure, so these need no database.

from falcon.identity import _apply_agent_redaction  # noqa: E402
from falcon.watcher import format_result  # noqa: E402


def conversation():
    """One turn that stored a document and read another."""
    return [
        {"role": "user", "content": "save chapter three, then read my notes"},
        {
            "role": "assistant",
            "content": "On it.",
            "raw_content": (
                "On it.\n\n[AGENT: library_store]\nTitle: Chapter Three\n---\nbody\n[/AGENT]"
                "\n\n[AGENT: read_document]\ndoc_beefbeefbeef\n[/AGENT]"
            ),
        },
        {
            "role": "assistant",
            "_watcher": True,
            "content": "Proof: Chapter Three — id: doc_a1b2c3d4e5f6",
            "raw_content": format_result(
                "**STORED** — verified.\n- **Storage id:** doc_a1b2c3d4e5f6\n"
                "- **Title:** Chapter Three",
                "library_store",
            ),
        },
        {
            "role": "assistant",
            "_watcher": True,
            "content": "",
            "raw_content": format_result("**Notes**\n\nBuy milk. Call Sam.", "read_document"),
        },
    ]


def test_model_history_carries_no_command_blocks():
    """The model must not be handed its own past commands.

    They already ran. Replaying them is a worked example in the exact syntax the
    persona asks for, and copying it is what filled conversations with agent
    output — so a repeat is the only thing they can cause.
    """
    rows = _apply_agent_redaction(conversation(), for_model=True)
    joined = " ".join(r["content"] for r in rows).upper()
    assert "[AGENT:" not in joined
    assert "LIBRARY_STORE]" not in joined
    # And no trace of the old `(ran: ...)` half-measure either.
    assert "(RAN:" not in joined


def test_model_history_keeps_results_whole():
    """It still has to reason over what came back."""
    rows = _apply_agent_redaction(conversation(), for_model=True)
    joined = "\n".join(r["content"] for r in rows)
    assert "Buy milk. Call Sam." in joined      # the document it read
    assert "doc_a1b2c3d4e5f6" in joined         # the id it stored


def test_model_can_tell_which_command_answered():
    """With the command block gone, the result has to name its own command —
    otherwise a turn that ran two tools returns two results and no way to pair
    them with what was asked."""
    rows = _apply_agent_redaction(conversation(), for_model=True)
    joined = "\n".join(r["content"] for r in rows)
    assert "(from: library_store)" in joined
    assert "(from: read_document)" in joined


def test_reader_sees_only_the_proof_line():
    rows = _apply_agent_redaction(conversation(), for_model=False)
    assert [r["content"] for r in rows] == [
        "save chapter three, then read my notes",
        "On it.",
        "Proof: Chapter Three — id: doc_a1b2c3d4e5f6",
    ]


def test_reader_never_sees_a_result_body():
    rows = _apply_agent_redaction(conversation(), for_model=False)
    joined = " ".join(r["content"] for r in rows)
    for leaked in ("AGENT RESULT", "Buy milk", "STORED", "from:", "[AGENT"):
        assert leaked not in joined


def test_legacy_rows_are_filtered_on_read_with_no_backfill():
    """Rows written before the split hold the raw block in `content` itself."""
    legacy = [
        {"role": "assistant", "content": "Sure.\n\n[AGENT: memory_status][/AGENT]"},
        {
            "role": "assistant",
            "_watcher": True,
            "content": "[AGENT RESULT]\n**MEMORY BRIDGE** — OK\n- db: falcon (pid 4417)\n[/AGENT RESULT]",
        },
    ]
    rows = _apply_agent_redaction([dict(r) for r in legacy], for_model=False)
    # The command is gone from the reply, and the probe dump is dropped entirely.
    assert [r["content"] for r in rows] == ["Sure."]

    # The model still gets the legacy result, since that row has no `raw_content`
    # to fall back to and its `content` is the block.
    model_rows = _apply_agent_redaction([dict(r) for r in legacy], for_model=True)
    assert "MEMORY BRIDGE" in model_rows[1]["content"]
    assert "[AGENT:" not in model_rows[0]["content"]


def test_format_result_without_a_command_is_unchanged():
    """Research delivers through the same path without naming a command."""
    assert format_result("body") == "[AGENT RESULT]\nbody\n[/AGENT RESULT]"


# ---------------------------------------------------------------------------
# read_document — the one result that goes to the reader and not to the model
# ---------------------------------------------------------------------------

from falcon.agent_redact import is_reader_facing, model_result_view  # noqa: E402

# Stands in for a document far larger than the context window.
BIG_BODY = "Lorem ipsum dolor sit amet. " * 4000

DOCUMENT = (
    "### Quarterly Report.pdf\n\n"
    "- **Storage id:** `doc_a1b2c3d4e5f6`\n"
    "- **Saved:** 2026-09-09 14:02 UTC\n"
    "- **File:** application/pdf, 812 KB\n"
    "- **Download:** [Quarterly Report.pdf](/api/documents/doc_a1b2c3d4e5f6/file)\n\n"
    f"**Full text** — {len(BIG_BODY):,} characters, extracted from the file\n\n"
    f"```text\n{BIG_BODY}\n```"
)


def document_turn():
    return [
        {"role": "user", "content": "show me the quarterly report"},
        {
            "role": "assistant",
            "_watcher": True,
            "content": condense_result("read_document", DOCUMENT),
            "raw_content": format_result(DOCUMENT, "read_document"),
            "_watcher_command": "read_document",
        },
    ]


def test_reader_gets_the_document_whole():
    """It is what they asked for, so it is not condensed away."""
    assert condense_result("read_document", DOCUMENT) == DOCUMENT
    rows = _apply_agent_redaction(document_turn(), for_model=False)
    assert BIG_BODY[:60] in rows[1]["content"]
    assert "/api/documents/doc_a1b2c3d4e5f6/file" in rows[1]["content"]


def test_model_never_receives_the_document_text():
    """The whole point: a document can be bigger than the context window."""
    rows = _apply_agent_redaction(document_turn(), for_model=True)
    payload = "\n".join(r["content"] for r in rows)
    assert "Lorem ipsum" not in payload
    assert len(payload) < 1000, "the body leaked into the payload"


def test_model_is_told_it_has_not_read_it():
    """An absence invites invention. The note has to be an instruction."""
    rows = _apply_agent_redaction(document_turn(), for_model=True)
    note = rows[1]["content"]
    assert "have not read it" in note
    assert "delivered straight to the user" in note
    # Still able to name what it delivered.
    assert "Quarterly Report.pdf" in note
    assert "doc_a1b2c3d4e5f6" in note


def test_other_results_are_unaffected():
    """Only read_document inverts. Everything else still reaches the model whole."""
    assert not is_reader_facing("library_store")
    block = format_result("**Notes**\n\nBuy milk.", "fetch_replies")
    assert model_result_view("fetch_replies", block) == block


def test_a_failed_read_is_still_one_sentence():
    """Reader-facing does not mean an error page is shown to the reader."""
    out = condense_result("read_document", "[ERROR] No stored document with id 'doc_x'.")
    assert out == "Could not read that document."
