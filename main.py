from __future__ import annotations
import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from src.routers import api, auth
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.database import init_db
    init_db()
    yield


app = FastAPI(title="DOCX Parser API", version="1.0.0", lifespan=lifespan)

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


@app.get("/")
def read_root() -> dict[str, str]:
    return {
        "service": "docx-parser",
        "message": "Use POST /api/v1/parse with multipart/form-data to upload and parse one or more DOCX files.",
    }


app.include_router(auth.router)
app.include_router(api.router)
