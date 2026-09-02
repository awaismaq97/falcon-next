"""
watcher_persona.py — the watcher's system-prompt block, stored in MongoDB.

The persona has two halves with opposite requirements, and separating them is
the whole point of this module:

  AVAILABLE COMMANDS   Derived, never stored. Rebuilt from the live tool
                       registry every time the persona is read, so it cannot
                       drift — it can never advertise a deleted tool or miss a
                       newly spawned one, because there is no cached copy to go
                       stale.

  Preamble and rules   Authored. Stored in Mongo so they survive restarts,
                       redeploys and scaling, and can be edited from the UI
                       without a code change.

Why not config.yaml, which is what this replaced: DigitalOcean App Platform
rebuilds the container image from git on every deploy, so a persona written to
config.yaml at runtime is silently reset on the next push. The agents survived
in Mongo but the text telling the model they existed did not — which is why the
watcher appeared to "disappear" after each deploy. A file also cannot be shared
between instances, so the same bug would return at instance_count > 1.

config.yaml keeps only the seed: on a database with no persona document yet,
the defaults below are written once and become editable from then on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from falcon.db import get_db

logger = logging.getLogger("falcon.watcher_persona")

COLL = "watcher_persona"
_SINGLETON_ID = "singleton"


# ---------------------------------------------------------------------------
# Authored defaults — seeded once, then owned by whoever edits them in the UI
# ---------------------------------------------------------------------------

DEFAULT_PREAMBLE = (
    "You also have access to an external watcher agent that executes commands on your behalf. "
    "When a task requires a tool, emit a command block in your response using this exact format:\n\n"
    "[AGENT: <command>]\n"
    "<payload>\n"
    "[/AGENT]\n\n"
    "The opening tag names the command. Everything between the opening and closing [/AGENT] tag "
    "is the payload passed to the tool. The closing tag is required — it tells the agent exactly "
    "where your command ends and your normal response continues."
)

DEFAULT_RULES = (
    "- The [/AGENT] closing tag is mandatory. Never omit it.\n"
    "- Place the entire block on its own lines, separate from your explanation text.\n"
    "- You may write normal text before and after the block.\n"
    "- For multiple commands, emit multiple blocks in sequence.\n"
    "- Do not describe what the command will do inside the block — just the payload.\n"
    "- These tools are real and act on the outside world. Never invent placeholder "
    "inputs such as example.com — if you do not have the information a command needs, "
    "ask the user for it instead of guessing.\n"
    "- Never claim something was remembered, saved, stored or recalled without having "
    "run persistent_memory_access_bridge and seen it report success. Report what the "
    "bridge actually returns. If it reports a failure, say storage is not working; if "
    "you have not run it, say the status is unverified. An assumed memory claim is the "
    "one failure the user cannot detect for themselves.\n"
    "- Saving text means running library_store and quoting the storage id it returns. "
    "Nothing you merely read stays available to you later, so if the user asks you to "
    "keep, save or remember something, store it before you say you have.\n"
    "- Never paste a document's text into a command payload. Uploaded files are already "
    "stored and carry a storage id; commands take ids, not contents. A payload containing "
    "a whole document is always a mistake, and the user sees every character of it.\n"
    "- Never print a document's contents back to the user just because you have them. "
    "They uploaded that file; they do not need it read aloud. Answer the question, quote "
    "only the passages that matter, and when they want the document itself give them its "
    "download link. Reproduce a document in full only if asked to.\n"
    "- Deleting a stored document is permanent and cannot be undone. Run delete_doc only "
    "when the user has asked for that particular document to be removed, never on your own "
    "initiative and never to tidy up or replace something. If you are not certain which "
    "document they mean, ask before deleting rather than after."
)

# Hand-written descriptions for built-in tools. A tool absent from this map —
# anything spawned at runtime — is described from its spawn prompt instead.
BUILTIN_DESCRIPTIONS: dict[str, dict] = {
    "echo": {
        "use_when": "user asks you to relay or confirm a piece of text.",
        "payload": "the text to echo.",
        "example": "This message was relayed successfully.",
    },
    "ping": {
        "use_when": "user asks if the watcher is alive or wants a timestamp.",
        "payload": "none.",
        "example": "",
    },
    "http_get": {
        "use_when": "user asks you to fetch a URL, check a page, or retrieve API data.",
        "payload": "the full URL (must start with http:// or https://).",
        "example": "https://httpbin.org/get",
    },
    "post_tweet": {
        "use_when": "user asks you to post a tweet or message to X/Twitter.",
        # Left empty so no "Payload:" line is rendered — the example carries it.
        "payload": "",
        "example": "Hello from Falcon.",
    },
    "fetch_replies": {
        "use_when": (
            "user asks what people said in reply to a post on X. Reads only — it never "
            "posts. Only reaches replies from the last 7 days, so report an empty result "
            "as 'none found in the last 7 days' rather than 'nobody replied'."
        ),
        "payload": "the post URL or its numeric id, optionally followed by 'limit N'.",
        "example": "https://x.com/user/status/1234567890",
    },
    "persistent_memory_access_bridge": {
        "use_when": (
            "BEFORE you say anything about remembering, saving, storing or recalling — "
            "and whenever the user asks whether memory or persistence is working. Run it "
            "first, then report exactly what it returns. It performs a real write, read-back "
            "and update against the live database, so its verdict is evidence rather than "
            "assumption. Never state that something was remembered or saved on the strength "
            "of your own impression: if this reports FAILED, say storage is not working, and "
            "if you have not run it, say the status is unverified."
        ),
        "payload": "none.",
        "example": "",
    },
    "memory_status": {
        "use_when": (
            "user asks whether memory, storage or saving is working, or you need the "
            "current state before answering such a question. Reports three things: "
            "configured, writable (verified by a real round-trip, not assumed), and "
            "when the last successful write happened."
        ),
        "payload": "none.",
        "example": "",
    },
    "library_store": {
        "use_when": (
            "you need to keep text that exists only in this conversation — notes you "
            "wrote, a draft, research findings, something the user typed and wants kept. "
            "It writes to the external database, reads the write back to verify it, and "
            "returns a permanent storage id. Report that id; if it returns NOT STORED, "
            "say the text was not saved rather than implying it was.\n"
            "NOT for uploaded files. An attached document is already saved the moment it "
            "is uploaded and its envelope states its storage id — if the user says 'save "
            "this doc', tell them it is already stored and give the id. Never copy a "
            "document's text into this command: it would store a second copy and print "
            "the whole file into the chat on the way."
        ),
        "payload": (
            "a Title line, an optional Tags line, then a line of dashes, then the full "
            "body text. Tags are comma-separated. With no headers the whole payload is "
            "stored and the first line becomes the title."
        ),
        "example": (
            "Title: Chapter Three — The Descent\n"
            "Tags: manuscript, draft, act-two\n"
            "---\n"
            "The lift had not moved in eleven years, which was why she chose it..."
        ),
    },
    "list_documents": {
        "use_when": (
            "user refers to a document, manuscript, outline, note or file from earlier — "
            "check what is actually stored before answering. Lists storage ids, titles "
            "and tags for everything uploaded or saved with library_store. Search by a "
            "term to match titles, tags or body text."
        ),
        "payload": "empty to list everything recent, or a search term or tag.",
        "example": "chapter three",
    },
    "read_document": {
        "use_when": (
            "the user wants a stored document back, or you need what is in it. Get the id "
            "from list_documents first.\n"
            "An uploaded file (PDF, Word, spreadsheet) comes back as a download link plus its "
            "opening lines. Pass the link straight through — the file is what they uploaded "
            "and what they want back, and nobody wants a PDF retyped into a chat window. Add "
            "'full' after the id ONLY when you must analyse, search or quote the contents to "
            "answer something, and even then answer from it rather than reprinting it.\n"
            "Free text saved with library_store has no file to hand over, so it comes back "
            "whole and needs no 'full'.\n"
            "This is also how you recall something from an earlier session: read it back "
            "rather than claiming to remember it."
        ),
        "payload": (
            "the storage id; add 'full' only to pull the entire text of an uploaded file."
        ),
        "example": "doc_a1b2c3d4e5f6",
    },
    "delete_doc": {
        "use_when": (
            "the user asks you to delete, remove or get rid of a stored document. It "
            "erases the record, the original file and the list_documents entry, and there "
            "is no undo — so run it only when the user has actually asked for that "
            "specific document to go. Never delete to tidy up, to make room, to replace a "
            "document with a newer version, or because something looks like a duplicate.\n"
            "Take the id from list_documents and make sure it is the one they mean; if "
            "more than one document could match what they said, ask which before deleting. "
            "It reports back the title of what it removed — quote that, so the user can "
            "see the right thing went. If it reports NOT DELETED or FAILED, say the "
            "document is still stored."
        ),
        "payload": (
            "the storage id to delete. Ids only — never a title, a filename or a word "
            "like 'all'. Several ids separated by spaces delete several documents."
        ),
        "example": "doc_a1b2c3d4e5f6",
    },
    "spawn_agent": {
        "use_when": "you need to create a new tool/agent that doesn't exist yet.",
        "payload": "free-text description of the capability you need.",
        "example": "Create a tool that sends an email via SMTP.",
    },
    "research": {
        "use_when": (
            "a question needs real investigation rather than a single page fetch — "
            "comparing sources, gathering current facts, or anything you cannot answer "
            "from memory. Also use it to check on work already running: 'status' for "
            "progress, 'result' for the finished report, 'list' for recent jobs, "
            "'cancel <id>' to stop one. The job runs in the background for minutes and "
            "survives restarts, so start it, tell the user its id, and carry on — the "
            "report is posted into the conversation by itself when ready, even days later."
        ),
        "payload": (
            "the research question, or one of: status [id] / result [id] / list / cancel <id>."
        ),
        "example": "What are the current EU rules on AI model transparency, and when do they take effect?",
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coll():
    return get_db()[COLL]


# ---------------------------------------------------------------------------
# Stored half
# ---------------------------------------------------------------------------

def get_parts() -> dict[str, Any]:
    """The authored preamble and rules, seeding defaults on first use."""
    doc = _coll().find_one({"_id": _SINGLETON_ID}, {"_id": 0})
    if doc and doc.get("preamble") is not None and doc.get("rules") is not None:
        return doc

    seed = {
        "preamble": DEFAULT_PREAMBLE,
        "rules": DEFAULT_RULES,
        "updated_at": _now(),
        "updated_by": "system (seeded)",
    }
    # upsert rather than insert: two workers can boot simultaneously and both
    # find the collection empty.
    _coll().update_one({"_id": _SINGLETON_ID}, {"$setOnInsert": seed}, upsert=True)
    logger.info("watcher_persona: seeded defaults into %s", COLL)
    return dict(seed)


def save_parts(preamble: str, rules: str, updated_by: str = "") -> dict[str, Any]:
    """Replace the authored halves. The commands block is untouched — it is
    derived on read and is not the user's to edit."""
    preamble = (preamble or "").strip()
    rules = (rules or "").strip()
    if not preamble:
        raise ValueError("The preamble cannot be empty — it defines the command format.")

    update = {
        "preamble": preamble,
        "rules": rules,
        "updated_at": _now(),
        "updated_by": updated_by or "unknown",
    }
    _coll().update_one({"_id": _SINGLETON_ID}, {"$set": update}, upsert=True)
    logger.info("watcher_persona: updated by %r (%d chars)", updated_by, len(preamble) + len(rules))
    return update


def reset_parts(updated_by: str = "") -> dict[str, Any]:
    """Restore the shipped defaults."""
    return save_parts(DEFAULT_PREAMBLE, DEFAULT_RULES, updated_by=f"{updated_by} (reset)".strip())


# ---------------------------------------------------------------------------
# Derived half
# ---------------------------------------------------------------------------

def render_commands() -> str:
    """Build the numbered AVAILABLE COMMANDS block from the live registry.

    Deliberately recomputed on every call. A stored copy is exactly what used to
    go stale — advertising tools that had been deleted, or omitting ones spawned
    since it was written.
    """
    import falcon.watcher_generated as Generated
    import falcon.watcher_tools as WatcherTools

    tool_list = WatcherTools.list_tools()
    generated_contexts = {d["name"]: (d.get("context") or "") for d in Generated.list_all()}

    blocks = []
    for i, name in enumerate(tool_list, start=1):
        info = BUILTIN_DESCRIPTIONS.get(name)
        if info:
            use_when, payload_desc, example = info["use_when"], info["payload"], info["example"]
        else:
            # Spawned at runtime — describe it from the prompt that created it.
            context = generated_contexts.get(name, "").strip()
            use_when = (
                context[:120].rstrip() + ("..." if len(context) > 120 else "")
                if context
                else f"user asks you to use the {name} capability."
            )
            payload_desc = "tool-specific input (see tool documentation)."
            example = f"<your {name} input here>"

        block = f"{i}. {name}\nUse when: {use_when}"
        # Omitted entirely when blank, for tools whose example is self-explanatory.
        if payload_desc:
            block += f"\nPayload: {payload_desc}"
        block += (
            f"\nExample:\n[AGENT: {name}]\n{example}\n[/AGENT]"
            if example
            else f"\nExample:\n[AGENT: {name}][/AGENT]"
        )
        blocks.append(block)

    return "\n\n".join(blocks)


def assemble() -> str:
    """The full persona the model sees: stored halves around a live command list."""
    parts = get_parts()
    rules = parts.get("rules", "").strip()
    text = (
        f"{parts['preamble'].strip()}\n\n"
        f"AVAILABLE COMMANDS:\n\n{render_commands()}"
    )
    if rules:
        text += f"\n\nRULES:\n{rules}"
    return text


def describe() -> dict[str, Any]:
    """Everything the editor UI needs: both halves, the derived block, the result."""
    parts = get_parts()
    commands = render_commands()
    return {
        "preamble": parts["preamble"],
        "rules": parts.get("rules", ""),
        "commands": commands,
        "assembled": assemble(),
        "updated_at": parts.get("updated_at"),
        "updated_by": parts.get("updated_by", ""),
        "is_default": (
            parts["preamble"].strip() == DEFAULT_PREAMBLE.strip()
            and parts.get("rules", "").strip() == DEFAULT_RULES.strip()
        ),
    }
