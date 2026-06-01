from __future__ import annotations
import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from src.routers import api, auth

app = FastAPI(title="DOCX Parser API", version="1.0.0")
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    from src.database import init_db
    init_db()


@app.get("/")
def read_root() -> dict[str, str]:
    return {
        "service": "docx-parser",
        "message": "Use POST /api/v1/parse with multipart/form-data to upload and parse one or more DOCX files.",
    }


app.include_router(auth.router)
app.include_router(api.router)
