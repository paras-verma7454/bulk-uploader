from __future__ import annotations
import logging
from typing import Annotated
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.openapi.utils import get_openapi

from src.api_utils import parse_and_store_upload
from src.database import get_session, init_db, list_saved_documents
from sqlalchemy.exc import SQLAlchemyError

app = FastAPI(title="DOCX Parser API", version="1.0.0")
logger = logging.getLogger(__name__)


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )

    request_schema = schema["paths"]["/api/v1/parse"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    ref_name = request_schema["$ref"].split("/")[-1]
    files_schema = schema["components"]["schemas"][ref_name]["properties"]["files"]
    files_schema["items"] = {
        "type": "string",
        "format": "binary",
    }

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def read_root() -> dict[str, str]:
    return {
        "service": "docx-parser",
        "message": "Use POST /api/v1/parse with multipart/form-data to upload and parse one or more DOCX files.",
    }


@app.get("/api/v1/questions")
def get_saved_questions() -> dict[str, object]:
    try:
        with get_session() as session:
            documents = list_saved_documents(session)
    except SQLAlchemyError as exc:
        logger.exception("Failed to fetch saved questions")
        raise HTTPException(status_code=500, detail="Failed to fetch saved questions") from exc

    return {
        "total_documents": len(documents),
        "total_questions": sum(int(document["total_questions"]) for document in documents),
        "documents": documents,
    }


@app.post("/api/v1/parse")
def parse_docx(
    files: Annotated[list[UploadFile], File(description="Upload one or more DOCX files")],
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for upload in files:
        try:
            documents.append(parse_and_store_upload(upload))
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
