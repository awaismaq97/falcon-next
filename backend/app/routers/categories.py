"""
categories.py router — REST API for categories and categorized messages.

Endpoints:
  GET    /identities/{id}/categories                                   list categories
  POST   /identities/{id}/categories                                   add category
  DELETE /identities/{id}/categories/{category_id}                     delete category + all its messages

  GET    /identities/{id}/categories/{category_id}/messages            list messages (paginated)
  DELETE /identities/{id}/categories/{category_id}/messages/{msg_id}  delete single message

Error mapping:
  ValueError  → 400 Bad Request  (bad input, duplicate name, malformed ObjectId)
  PyMongoError → 503 Service Unavailable (DB unreachable / transient failure)
  Everything else bubbles to the global 500 handler in main.py
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pymongo.errors import PyMongoError
from pydantic import BaseModel, field_validator

import falcon.categories as Categories

logger = logging.getLogger(__name__)

router = APIRouter(tags=["categories"])


class AddCategoryRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Category name cannot be empty.")
        return v


# ---------------------------------------------------------------------------
# Internal helper — converts layer exceptions to HTTP responses consistently
# ---------------------------------------------------------------------------

def _handle_db_error(exc: Exception, *, context: str) -> None:
    """Log and re-raise as an appropriate HTTPException."""
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, PyMongoError):
        logger.error("categories router: DB error in %s: %s", context, exc)
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable. Please try again.",
        )
    # Unexpected error — let the global handler turn it into a 500.
    logger.exception("categories router: unexpected error in %s", context)
    raise HTTPException(status_code=500, detail="Unexpected server error.")


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@router.get("/identities/{identity_id}/categories")
def list_categories(identity_id: str) -> dict:
    try:
        cats = Categories.list_categories(identity_id)
    except Exception as exc:
        _handle_db_error(exc, context="list_categories")
    return {"identity_id": identity_id, "categories": cats, "count": len(cats)}


@router.post("/identities/{identity_id}/categories", status_code=201)
def add_category(identity_id: str, req: AddCategoryRequest) -> dict:
    try:
        cat = Categories.add_category(identity_id, req.name)
    except Exception as exc:
        _handle_db_error(exc, context="add_category")
    return cat


@router.delete("/identities/{identity_id}/categories/{category_id}")
def delete_category(identity_id: str, category_id: str) -> dict:
    try:
        deleted = Categories.delete_category(category_id, identity_id)
    except Exception as exc:
        _handle_db_error(exc, context="delete_category")
    if not deleted:
        raise HTTPException(status_code=404, detail="Category not found.")
    return {"deleted": category_id}


# ---------------------------------------------------------------------------
# Category messages
# ---------------------------------------------------------------------------

@router.get("/identities/{identity_id}/categories/{category_id}/messages")
def list_category_messages(
    identity_id: str,
    category_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    try:
        result = Categories.list_category_messages(
            identity_id, category_id, skip=skip, limit=limit
        )
    except Exception as exc:
        _handle_db_error(exc, context="list_category_messages")
    return result


@router.delete(
    "/identities/{identity_id}/categories/{category_id}/messages/{message_id}"
)
def delete_category_message(
    identity_id: str, category_id: str, message_id: str
) -> dict:
    try:
        deleted = Categories.delete_category_message(message_id, identity_id)
    except Exception as exc:
        _handle_db_error(exc, context="delete_category_message")
    if not deleted:
        raise HTTPException(status_code=404, detail="Message not found.")
    return {"deleted": message_id}


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------

@router.get("/identities/{identity_id}/categories/{category_id}/export.pdf")
def export_category_pdf(identity_id: str, category_id: str):
    """Stream all messages in a category as a formatted PDF download."""
    from fastapi.responses import Response

    try:
        pdf_bytes = Categories.export_category_pdf(identity_id, category_id)
    except ImportError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except Exception as exc:
        _handle_db_error(exc, context="export_category_pdf")

    # Sanitise category name for use in the Content-Disposition filename.
    import re
    try:
        cats = Categories.list_categories(identity_id)
        cat_name = next(
            (c["name"] for c in cats if c["_id"] == category_id), "category"
        )
    except Exception:
        cat_name = "category"

    safe_name = re.sub(r"[^\w\-. ]", "_", cat_name).strip().replace(" ", "_")
    filename = f"falcon_{identity_id}_{safe_name}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
