"""
research.py — long-running research jobs: search → browse → summarize.

Backs the ``research`` watcher tool. A job is started with a question and then
runs on its own for minutes, working in rounds:

    search  →  fetch the top pages  →  extract what matters  →  decide whether
    another round is needed  →  finally, write a report

Every round is written to MongoDB before the next one starts, so a job is never
held in memory alone. That is what makes it survive: a redeploy, a crash, or a
laptop closing mid-job costs at most the round in flight. When a process dies
mid-job its heartbeat goes stale, and the next process to look — the same one
after a restart, or another instance — picks the job up and resumes from the
round it had reached.

Sessions do not own jobs. A job outlives the conversation that started it; days
later, ``[AGENT: research] result <id>`` still returns the report, and a
finished job also posts itself into the identity's chat so the answer arrives
without anyone having to remember to ask.

Retention: jobs are kept indefinitely. There is no TTL index on
``research_jobs``, no age-based sweep and no cap on how many are stored — the
only thing that removes a job is an explicit ``delete_job`` call, reachable from
the DELETE endpoint and the Reports panel. Cancelling a job stops the work but
keeps what it had already found.

Search providers, in order of preference:
    TAVILY_API_KEY   — best fit: returns extracted page content, not just links
    BRAVE_API_KEY    — standard web results
    (none)           — falls back to scraping DuckDuckGo's lite endpoint, which
                       is unauthenticated, rate-limited and liable to break.
                       Fine for a smoke test, not for real use.

Configuration (all optional):
    RESEARCH_ENABLED       "false" to stop the worker starting.  Default: on
    RESEARCH_MAX_ROUNDS    Search rounds per job.                Default: 4
    RESEARCH_MODEL         Model for extraction + report.  Default: openai/gpt-4o-mini
    TAVILY_API_KEY / BRAVE_API_KEY   Search provider credentials.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from falcon.db import get_db

logger = logging.getLogger("falcon.research")

COLL = "research_jobs"

# Round budget. Each round is one search, a few fetches and one model call, so
# this bounds both cost and wall-clock without needing a token accountant.
_DEFAULT_MAX_ROUNDS = 4
_RESULTS_PER_SEARCH = 6
_FETCH_PER_ROUND = 3

_FETCH_TIMEOUT = 15
_MAX_PAGE_CHARS = 6000
_POLITE_DELAY = 1.0

# A job that somehow runs this long is stuck; it is failed with whatever it has
# rather than left holding a claim forever.
_JOB_WALL_CLOCK = 45 * 60
# How long a claim survives without a heartbeat before another worker resumes it.
_HEARTBEAT_STALE = 5 * 60
_POLL_SECONDS = 5

_UA = "Mozilla/5.0 (compatible; FalconResearch/1.0; +https://github.com/falcon)"

_worker: threading.Thread | None = None
_stop = threading.Event()
_worker_lock = threading.Lock()


def _now() -> str:
    """UTC, ISO-8601. Fixed format, so string comparison is time comparison."""
    return datetime.now(timezone.utc).isoformat()


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def enabled() -> bool:
    return os.environ.get("RESEARCH_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def _max_rounds() -> int:
    raw = os.environ.get("RESEARCH_MAX_ROUNDS", "").strip()
    if not raw:
        return _DEFAULT_MAX_ROUNDS
    try:
        return max(1, min(10, int(raw)))
    except ValueError:
        return _DEFAULT_MAX_ROUNDS


def _model() -> str:
    return os.environ.get("RESEARCH_MODEL", "").strip() or "openai/gpt-4o-mini"


def _coll():
    return get_db()[COLL]


# ── Search ─────────────────────────────────────────────────────────────────

# Real keys from these providers are 30+ characters. Anything much shorter is a
# placeholder someone left behind ("dummy", "tvly-xxx", "REPLACE_ME") — treating
# it as configured would turn every search into a 401 with a confusing error,
# which is worse than falling back.
_MIN_KEY_LENGTH = 12


def _provider_key(name: str) -> str:
    raw = os.environ.get(name, "").strip()
    if raw and len(raw) < _MIN_KEY_LENGTH:
        logger.warning("research: %s looks like a placeholder (%d chars) — ignoring it", name, len(raw))
        return ""
    return raw


def search_provider() -> str:
    """Which provider a search would use right now — reported in job status."""
    if _provider_key("TAVILY_API_KEY"):
        return "tavily"
    if _provider_key("BRAVE_API_KEY"):
        return "brave"
    return "duckduckgo-lite"


def _search_tavily(query: str, limit: int) -> list[dict]:
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": _provider_key("TAVILY_API_KEY"),
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in (resp.json().get("results") or [])
        if r.get("url")
    ]


def _search_brave(query: str, limit: int) -> list[dict]:
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": limit},
        headers={
            "X-Subscription-Token": _provider_key("BRAVE_API_KEY"),
            "Accept": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
        for r in ((resp.json().get("web") or {}).get("results") or [])
        if r.get("url")
    ]


_DDG_LINK = re.compile(r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_DDG_ANY = re.compile(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.I | re.S)


def _search_ddg(query: str, limit: int) -> list[dict]:
    """Unauthenticated fallback. Best-effort by construction — no stable contract."""
    resp = requests.post(
        "https://lite.duckduckgo.com/lite/",
        data={"q": query},
        headers={"User-Agent": _UA},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.text

    matches = _DDG_LINK.findall(body) or _DDG_ANY.findall(body)
    out: list[dict] = []
    seen: set[str] = set()
    for href, label in matches:
        url = html.unescape(href)
        # Results are wrapped in a redirector; the real target is in ?uddg=
        m = re.search(r"[?&]uddg=([^&]+)", url)
        if m:
            from urllib.parse import unquote
            url = unquote(m.group(1))
        if not url.startswith("http") or "duckduckgo.com" in url or url in seen:
            continue
        seen.add(url)
        out.append({"title": _strip_html(label)[:200], "url": url, "snippet": ""})
        if len(out) >= limit:
            break
    return out


def _search_with(provider: str, query: str, limit: int) -> list[dict]:
    if provider == "tavily":
        return _search_tavily(query, limit)
    if provider == "brave":
        return _search_brave(query, limit)
    return _search_ddg(query, limit)


def _provider_chain() -> list[str]:
    """Every provider that could answer, best first.

    DuckDuckGo is always last and always present: it needs no key, so there is
    no configuration under which a job has nothing left to try.
    """
    chain = []
    if _provider_key("TAVILY_API_KEY"):
        chain.append("tavily")
    if _provider_key("BRAVE_API_KEY"):
        chain.append("brave")
    chain.append("duckduckgo-lite")
    return chain


def _search(query: str, limit: int = _RESULTS_PER_SEARCH) -> list[dict]:
    """Search, falling through to the next provider when one lets us down.

    A single provider is a single point of failure for the whole job, and the
    failure is silent in the worst way: the round finds no pages, every later
    round has nothing to build on, and the finished report says no findings
    could be gathered — as if the question had no answers, rather than that one
    HTTP request timed out. Tavily being slow for thirty seconds should not
    decide the outcome when an unauthenticated fallback is sitting right there.

    A provider that answers with an empty list is treated the same as one that
    raised: it did not help, so try the next.
    """
    attempted: list[str] = []
    for provider in _provider_chain():
        try:
            results = _search_with(provider, query, limit)
        except Exception as exc:  # noqa: BLE001 — a dead provider must not kill the job
            logger.warning("research: search via %s failed for %r: %s", provider, query[:60], exc)
            attempted.append(f"{provider} ({type(exc).__name__})")
            continue

        if results:
            if attempted:
                logger.info(
                    "research: %s returned %d results for %r after %s",
                    provider, len(results), query[:60], ", ".join(attempted),
                )
            return results

        logger.info("research: %s returned nothing for %r", provider, query[:60])
        attempted.append(f"{provider} (empty)")

    logger.warning(
        "research: every provider failed for %r — tried %s", query[:60], ", ".join(attempted),
    )
    return []


# ── Fetch + text extraction ────────────────────────────────────────────────

_TAG_STRIP = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def _strip_html(raw: str) -> str:
    text = _TAG_STRIP.sub(" ", raw)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    return _BLANKS.sub("\n\n", text).strip()


def _fetch_text(url: str) -> str:
    """Fetch a page and reduce it to plain text. Empty string on any failure."""
    try:
        resp = requests.get(
            url,
            timeout=_FETCH_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,text/plain"},
            stream=True,
        )
        ctype = resp.headers.get("Content-Type", "")
        if not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml", "application/json")):
            resp.close()
            return ""
        # Read a bounded amount: a stray 200MB file must not become the
        # container's memory problem.
        raw = resp.raw.read(2_000_000, decode_content=True) or b""
        resp.close()
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
        return _strip_html(text)[:_MAX_PAGE_CHARS]
    except Exception as exc:  # noqa: BLE001
        logger.info("research: fetch failed for %s: %s", url[:100], exc)
        return ""


# ── Model calls ────────────────────────────────────────────────────────────

def _chat(system: str, user: str, max_tokens: int = 1200) -> str:
    import falcon.config as Config
    from falcon.engine import get_client

    client = get_client(Config.OPENROUTER_API_KEY, title="Falcon-Research")
    resp = client.chat.completions.create(
        model=_model(),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def _chat_json(system: str, user: str, max_tokens: int = 1200) -> dict:
    """Model call expecting JSON back, tolerant of fenced or chatty output."""
    raw = _chat(system, user, max_tokens)
    raw = re.sub(r"^```(?:json)?\s*", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        # Last resort: the outermost {...} span.
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        logger.warning("research: could not parse model JSON: %s", raw[:200])
        return {}


_ROUND_SYSTEM = (
    "You are a research analyst. You are given a research question and the text of several web "
    "pages retrieved for it. Extract only what genuinely helps answer the question.\n\n"
    "Return ONLY a JSON object with this shape:\n"
    '{"findings": [{"url": "<source url>", "note": "<one specific fact or claim, with any figures '
    'or dates>"}], "next_query": "<a search query that would fill the biggest remaining gap, or '
    'empty string>", "done": <true if the question can now be answered well, false otherwise>}\n\n'
    "Rules: a finding must be supported by the page you attribute it to. Prefer specifics over "
    "generalities. If a page is irrelevant, contribute no finding for it. Never invent a URL."
)

_REPORT_SYSTEM = (
    "You are a research analyst writing up findings. Given a question and a list of findings with "
    "their sources, write a clear, well-organised answer in markdown.\n\n"
    "Rules: lead with a direct answer to the question. Support claims with the findings given, "
    "citing sources inline as markdown links. State plainly where the evidence is thin or "
    "conflicting. Do not add facts that are not in the findings. End with a '## Sources' list."
)


# ── Job lifecycle ──────────────────────────────────────────────────────────

def _new_job_id() -> str:
    """A short id the user can quote back. Retried against the unique index.

    Six hex chars is comfortable to read and repeat, but small enough that a
    birthday collision becomes plausible over thousands of jobs — so take the
    first id that is not already taken rather than letting an insert fail.
    """
    for _ in range(5):
        candidate = secrets.token_hex(3)
        if _coll().count_documents({"job_id": candidate}, limit=1) == 0:
            return candidate
    return secrets.token_hex(6)


def start_job(question: str, identity_id: str = "", max_rounds: int | None = None) -> dict:
    """Queue a research job. Returns the stored job document."""
    question = (question or "").strip()
    if not question:
        raise ValueError("A research question is required.")

    job = {
        "job_id": _new_job_id(),
        "identity_id": identity_id or "",
        "question": question,
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "finished_at": None,
        "rounds_done": 0,
        "max_rounds": max_rounds or _max_rounds(),
        "queries_run": [],
        "sources": [],
        "findings": [],
        "report": "",
        "error": "",
        "provider": search_provider(),
        "heartbeat_at": None,
        "worker_pid": None,
    }
    _coll().insert_one(dict(job))
    logger.info("research: queued job %s for identity=%r: %s", job["job_id"], identity_id, question[:80])
    # A job queued while the worker is idle should not wait for the poll tick.
    start_worker()
    return job


def get_job(job_id: str, identity_id: str = "") -> dict | None:
    q: dict[str, Any] = {"job_id": job_id.strip().lower()}
    if identity_id:
        q["identity_id"] = identity_id
    return _coll().find_one(q, {"_id": 0})


def list_jobs(identity_id: str = "", limit: int = 10) -> list[dict]:
    q = {"identity_id": identity_id} if identity_id else {}
    return list(
        _coll()
        .find(q, {"_id": 0, "findings": 0, "report": 0})
        .sort("created_at", -1)
        .limit(limit)
    )


def delete_job(job_id: str, identity_id: str = "") -> bool:
    """Permanently remove one job and everything it gathered.

    The only code path in this module that destroys a job. Nothing else prunes,
    expires or ages out ``research_jobs``: there is no TTL index on the
    collection and no retention sweep anywhere, by design. A job — its findings,
    its sources and its report — stays in Atlas until this is called.
    Cancelling only stops the work; the findings gathered up to that point are
    kept and remain readable.
    """
    q: dict[str, Any] = {"job_id": job_id.strip().lower()}
    if identity_id:
        q["identity_id"] = identity_id
    deleted = _coll().delete_one(q).deleted_count > 0
    if deleted:
        logger.info("research: job %s permanently deleted", job_id)
    return deleted


def cancel_job(job_id: str, identity_id: str = "") -> bool:
    q: dict[str, Any] = {"job_id": job_id.strip().lower(), "status": {"$in": ["queued", "running"]}}
    if identity_id:
        q["identity_id"] = identity_id
    res = _coll().update_one(
        q, {"$set": {"status": "cancelled", "updated_at": _now(), "finished_at": _now()}}
    )
    return res.modified_count > 0


def _claim() -> dict | None:
    """Take the oldest workable job: never started, or abandoned by a dead worker."""
    return _coll().find_one_and_update(
        {
            "$or": [
                {"status": "queued"},
                {"status": "running", "heartbeat_at": {"$lt": _ago(_HEARTBEAT_STALE)}},
            ]
        },
        {
            "$set": {
                "status": "running",
                "heartbeat_at": _now(),
                "worker_pid": os.getpid(),
                "updated_at": _now(),
            }
        },
        sort=[("created_at", 1)],
        return_document=True,
        projection={"_id": 0},
    )


def _beat(job_id: str, **fields: Any) -> None:
    fields.update(heartbeat_at=_now(), updated_at=_now())
    _coll().update_one({"job_id": job_id}, {"$set": fields})


def _is_cancelled(job_id: str) -> bool:
    doc = _coll().find_one({"job_id": job_id}, {"_id": 0, "status": 1})
    return bool(doc) and doc.get("status") == "cancelled"


def _run_round(job: dict, query: str) -> dict:
    """One search → fetch → extract cycle. Returns the model's round verdict."""
    job_id = job["job_id"]
    results = _search(query)
    if not results:
        return {"findings": [], "next_query": "", "done": False, "pages": 0}

    seen = set(job.get("sources") or [])
    pages: list[dict] = []
    for r in results:
        if len(pages) >= _FETCH_PER_ROUND:
            break
        if r["url"] in seen:
            continue
        # Tavily already returns extracted content; skip the round trip when
        # its snippet is substantial enough to reason over.
        text = r.get("snippet", "") if len(r.get("snippet", "")) > 400 else _fetch_text(r["url"])
        if not text:
            continue
        seen.add(r["url"])
        pages.append({"url": r["url"], "title": r.get("title", ""), "text": text})
        time.sleep(_POLITE_DELAY)

    if not pages:
        return {"findings": [], "next_query": "", "done": False, "pages": 0}

    corpus = "\n\n".join(
        f"--- SOURCE: {p['url']}\nTITLE: {p['title']}\n{p['text']}" for p in pages
    )
    verdict = _chat_json(
        _ROUND_SYSTEM,
        f"RESEARCH QUESTION:\n{job['question']}\n\n"
        f"FINDINGS SO FAR: {len(job.get('findings') or [])}\n\n"
        f"PAGES RETRIEVED THIS ROUND:\n{corpus[:24000]}",
    )
    verdict["pages"] = len(pages)
    verdict["fetched_urls"] = [p["url"] for p in pages]
    return verdict


def _write_report(job: dict) -> str:
    findings = job.get("findings") or []
    if not findings:
        return (
            "No usable findings were gathered for this question. Every configured search "
            "provider was tried and none returned results, or the pages they returned could "
            "not be fetched — check the provider credentials and whether this server can "
            "reach the internet. The server log names each provider it tried and why it "
            "gave up."
        )
    listed = "\n".join(f"- [{f['url']}] {f['note']}" for f in findings)
    return _chat(
        _REPORT_SYSTEM,
        f"QUESTION:\n{job['question']}\n\nFINDINGS:\n{listed[:24000]}",
        max_tokens=2000,
    )


def _work(job: dict) -> None:
    """Drive one claimed job to completion, persisting after every round."""
    job_id = job["job_id"]
    started = time.monotonic()
    query = job["question"]

    # Resuming: continue from whatever the last round asked for next.
    if job.get("rounds_done", 0) > 0 and job.get("queries_run"):
        query = job["queries_run"][-1]
        logger.info("research: resuming job %s at round %d", job_id, job["rounds_done"] + 1)

    try:
        while job["rounds_done"] < job["max_rounds"]:
            if _is_cancelled(job_id):
                logger.info("research: job %s cancelled mid-flight", job_id)
                return
            if time.monotonic() - started > _JOB_WALL_CLOCK:
                logger.warning("research: job %s exceeded its wall-clock budget", job_id)
                break

            verdict = _run_round(job, query)

            new_findings = [
                {"round": job["rounds_done"] + 1, "url": f.get("url", ""), "note": f.get("note", "")}
                for f in (verdict.get("findings") or [])
                if f.get("note")
            ]
            job["findings"] = (job.get("findings") or []) + new_findings
            job["sources"] = sorted(set((job.get("sources") or []) + (verdict.get("fetched_urls") or [])))
            job["queries_run"] = (job.get("queries_run") or []) + [query]
            job["rounds_done"] += 1

            # Persisted here, every round, before anything else can go wrong.
            _beat(
                job_id,
                findings=job["findings"],
                sources=job["sources"],
                queries_run=job["queries_run"],
                rounds_done=job["rounds_done"],
            )
            logger.info(
                "research: job %s round %d/%d — %d pages, %d findings (%d total)",
                job_id, job["rounds_done"], job["max_rounds"],
                verdict.get("pages", 0), len(new_findings), len(job["findings"]),
            )

            if verdict.get("done") and job["findings"]:
                break
            nxt = (verdict.get("next_query") or "").strip()
            if not nxt:
                break
            query = nxt

        report = _write_report(job)
        if _is_cancelled(job_id):
            return
        _beat(job_id, status="done", report=report, finished_at=_now())
        logger.info("research: job %s done — %d findings from %d sources",
                    job_id, len(job["findings"]), len(job["sources"]))
        _deliver(job_id)

    except Exception as exc:  # noqa: BLE001
        logger.error("research: job %s failed: %s", job_id, exc, exc_info=True)
        _beat(job_id, status="failed", error=str(exc)[:500], finished_at=_now())
        _deliver(job_id)


def _deliver(job_id: str) -> None:
    """Post the outcome into the identity's chat, if the job belongs to one.

    A job can outlast the conversation that started it by days, so the result is
    pushed rather than left waiting to be asked for. Reuses the watcher's own
    injection path, which the change-stream broadcaster then fans out to any
    connected client.
    """
    job = _coll().find_one({"job_id": job_id}, {"_id": 0})
    if not job or not job.get("identity_id"):
        return
    try:
        from falcon.watcher import _inject_result

        if job["status"] == "done":
            body = (
                f"Research complete — job `{job_id}`\n"
                f"**Question:** {job['question']}\n\n{job.get('report', '')}"
            )
        else:
            body = (
                f"Research job `{job_id}` ended with status **{job['status']}**.\n"
                f"**Question:** {job['question']}\n\n{job.get('error') or 'No further detail.'}"
            )
        _inject_result(job["identity_id"], body)
    except Exception as exc:  # noqa: BLE001 — delivery must not fail the job
        logger.warning("research: could not deliver job %s: %s", job_id, exc)


# ── Worker ─────────────────────────────────────────────────────────────────

def _loop() -> None:
    logger.info("research: worker started (pid %s, provider %s)", os.getpid(), search_provider())
    while not _stop.is_set():
        try:
            job = _claim()
            if job:
                _work(job)
                continue  # look for the next job immediately
        except Exception as exc:  # noqa: BLE001 — the worker must outlive any single job
            logger.error("research: worker loop error: %s", exc, exc_info=True)
        if _stop.wait(_POLL_SECONDS):
            return


def start_worker() -> None:
    """Start the background research worker. Idempotent."""
    global _worker
    if not enabled():
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        _worker = threading.Thread(target=_loop, name="falcon-research", daemon=True)
        _worker.start()


def stop_worker() -> None:
    """Signal the worker to exit. In-flight jobs resume elsewhere via heartbeat."""
    global _worker
    _stop.set()
    with _worker_lock:
        t = _worker
        _worker = None
    if t is not None and t.is_alive():
        t.join(timeout=5)


def bootstrap() -> None:
    """Start the worker at boot and report what it inherited.

    Nothing needs resetting here: ``_claim`` treats a ``running`` job with a
    stale heartbeat as available, so jobs abandoned by the previous process are
    picked up on their own.
    """
    if not enabled():
        logger.info("research: disabled via RESEARCH_ENABLED")
        return
    try:
        pending = _coll().count_documents({"status": {"$in": ["queued", "running"]}})
        if pending:
            logger.info("research: %d unfinished job(s) will be resumed", pending)
    except Exception as exc:  # noqa: BLE001
        logger.warning("research: could not count unfinished jobs: %s", exc)
    start_worker()
