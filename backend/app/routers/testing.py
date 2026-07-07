"""
testing.py — Continuity experiments (Testing tab).

Thin wrapper over tests/continuity_tests.py: list the registry (with resolved
per-variant settings), run a variant against the live API (slow — 10-30s),
return run history, and serve the generated markdown report.
"""
from __future__ import annotations

import os
import sys

from fastapi import APIRouter, HTTPException

from app.schemas import TestingRunRequest

router = APIRouter(tags=["testing"])

# continuity_tests lives in backend/tests and resolves its own file-relative
# paths, so putting that dir on sys.path lets us import it by bare name (matching
# how the Streamlit app loaded it).
_TESTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tests")
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def _ct():
    try:
        import continuity_tests as CT  # type: ignore

        return CT
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Could not import continuity_tests: {exc}")


@router.get("/testing/registry")
def registry() -> dict:
    CT = _ct()
    tests = CT.load_registry()
    out = []
    for t in tests:
        variants = []
        for i, v in enumerate(t.get("variants", [])):
            try:
                resolved = CT._build_variant_settings(v)
            except Exception:  # noqa: BLE001
                resolved = {}
            variants.append(
                {
                    "index": i,
                    "name": v.get("name", f"Variant {i}"),
                    "description": v.get("description", ""),
                    "resolved_settings": resolved,
                }
            )
        out.append(
            {
                "slug": t.get("slug", ""),
                "name": t.get("name", t.get("slug", "?")),
                "description": (t.get("description", "") or "").strip(),
                "variants": variants,
            }
        )
    return {"tests": out}


@router.get("/testing/{slug}/history")
def history(slug: str) -> dict:
    CT = _ct()
    return {"slug": slug, "runs": CT.load_run_history(slug)}


@router.get("/testing/{slug}/report")
def report(slug: str) -> dict:
    CT = _ct()
    content = CT.read_report(slug)
    return {"slug": slug, "report": content or ""}


@router.post("/testing/{slug}/run")
def run_variant(slug: str, req: TestingRunRequest) -> dict:
    """Run one variant against the live API. Blocking (10-30s); FastAPI runs this
    handler in its threadpool so the event loop stays free."""
    CT = _ct()
    if req.variant is None:
        raise HTTPException(400, "variant index is required")
    try:
        variant_idx = int(req.variant)
    except (TypeError, ValueError):
        raise HTTPException(400, "variant must be an integer index")
    try:
        record = CT.run_test_variant(slug, variant_idx)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Run failed: {exc}")
    return {"slug": slug, "record": record}
