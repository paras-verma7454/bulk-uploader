from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.exc import SQLAlchemyError

from src.auth import CurrentUser
from src.api_utils import parse_and_store_upload
from src.database import delete_document, get_session, list_saved_documents


router = APIRouter(prefix="/api/v1", tags=["api"])
logger = logging.getLogger(__name__)


@router.get("/questions")
def get_saved_questions(current_user: CurrentUser) -> dict[str, object]:
    try:
        with get_session() as session:
            documents = list_saved_documents(session, user_id=current_user.id)
    except SQLAlchemyError as exc:
        logger.exception("Failed to fetch saved questions")
        raise HTTPException(status_code=500, detail="Failed to fetch saved questions") from exc

    return {
        "total_documents": len(documents),
        "total_questions": sum(int(document["total_questions"]) for document in documents),
        "documents": documents,
    }


@router.delete("/documents/{document_id}")
def delete_saved_document(document_id: int, current_user: CurrentUser) -> dict[str, bool]:
    try:
        with get_session() as session:
            removed = delete_document(session, document_id=document_id, user_id=current_user.id)
    except SQLAlchemyError as exc:
        logger.exception("Failed to delete document")
        raise HTTPException(status_code=500, detail="Failed to delete document") from exc

    if not removed:
        raise HTTPException(status_code=404, detail="Document not found")

    return {"ok": True}


@router.post("/parse")
def parse_docx(
    current_user: CurrentUser,
    files: Annotated[list[UploadFile], File(description="Upload one or more DOCX files")],
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for upload in files:
        try:
            documents.append(parse_and_store_upload(upload, user_id=current_user.id))
        except HTTPException as exc:
            filename = upload.filename or "unknown"
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            errors.append({"source_file": filename, "error": detail})

    return {
        "total_files": len(files),
        "successful_files": len(documents),
        "failed_files": len(errors),
        "total_questions": sum(int(document["total_questions"]) for document in documents),
        "documents": documents,
        "errors": errors,
    }
