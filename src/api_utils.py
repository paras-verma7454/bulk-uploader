from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from docx.opc.exceptions import PackageNotFoundError
from fastapi import File, HTTPException, UploadFile
from sqlalchemy.exc import SQLAlchemyError

from src.database import get_session, save_document_report
from src.parser import parse_document, report_to_dict

logger = logging.getLogger(__name__)

ALLOWED_DOCX_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/octet-stream",
}

IMG_TAG_SRC_RE = re.compile(r"<img\b[^>]*\bsrc=\"([^\"]+)\"[^>]*/?>", re.IGNORECASE)


def sanitize_filename_stem(file_name: str) -> str:
    """Sanitize filename stem by removing invalid characters."""
    stem = Path(file_name).stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return cleaned or "parsed"


def dedupe_images_in_html(html_fragment: str) -> str:
    """Remove duplicate images from HTML fragment based on src attribute."""
    seen_sources: set[str] = set()

    def replace_match(match: re.Match[str]) -> str:
        src = match.group(1)
        if src in seen_sources:
            return ""
        seen_sources.add(src)
        return match.group(0)

    return IMG_TAG_SRC_RE.sub(replace_match, html_fragment)


def write_report_html(report_data: dict[str, Any], source_file: str) -> str:
    """Write parsed document report as HTML file.
    
    Always writes to output.html (overwrites existing file).
    Returns the path to the generated HTML file.
    """
    output_path = Path("output.html")

    questions = report_data.get("questions", [])
    questions_html_parts: list[str] = []

    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            continue

        question_number = question.get("number") or str(index)
        question_html = str(question.get("question_html") or "").strip()
        question_text = str(question.get("question_text") or "").strip()
        rendered_question = dedupe_images_in_html(question_html) if question_html else html.escape(question_text)

        answer_label = str(question.get("answer") or "").strip().upper()
        options = question.get("options", [])
        option_items: list[str] = []

        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue

                label = str(option.get("label") or "").strip().upper()
                option_html = str(option.get("html") or "").strip()
                option_text = str(option.get("text") or "").strip()
                rendered_option = dedupe_images_in_html(option_html) if option_html else html.escape(option_text)
                option_items.append(
                    "<li class=\"option\"><span class=\"option-content\">"
                    + rendered_option
                    + "</span></li>"
                )

        solution_html = str(question.get("solution_html") or "").strip()
        solution_text = str(question.get("solution_text") or "").strip()
        rendered_solution = dedupe_images_in_html(solution_html) if solution_html else html.escape(solution_text) or "<em>No solution provided.</em>"

        answer_badge = html.escape(answer_label) if answer_label else "N/A"
        questions_html_parts.append(
            "<section class=\"question-card\">"
            + "<div class=\"question-head\">"
            + "<h2>Question "
            + html.escape(str(question_number))
            + "</h2>"
            + "<span class=\"answer-badge\">Answer: "
            + answer_badge
            + "</span></div>"
            + "<div class=\"question-body\">"
            + rendered_question
            + "</div>"
            + "<ul class=\"option-list\">"
            + "".join(option_items)
            + "</ul>"
            + "<div class=\"solution\"><h3>Solution</h3><div>"
            + rendered_solution
            + "</div></div>"
            + "</section>"
        )

    questions_html = "".join(questions_html_parts) or "<p>No MCQ questions found in parsed output.</p>"

    html_content = (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\" />\n"
        f"  <title>Parsed Output - {html.escape(source_file)}</title>\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
        "  <script>window.MathJax = { tex: { inlineMath: [['\\\\(', '\\\\)'], ['$', '$']] }, svg: { fontCache: 'global' } };</script>\n"
        "  <script defer src=\"https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js\"></script>\n"
        "  <style>\n"
        "    body { font-family: 'Segoe UI', 'Cambria Math', 'Times New Roman', Arial, sans-serif; margin: 2rem; background: #f8fafc; color: #0f172a; }\n"
        "    .container { max-width: 1100px; margin: 0 auto; }\n"
        "    h1 { margin: 0 0 0.5rem 0; }\n"
        "    p.meta { color: #475569; margin: 0 0 1.5rem 0; }\n"
        "    .question-card { background: #ffffff; border: 1px solid #dbe5f1; border-radius: 10px; padding: 1rem 1.1rem; margin-bottom: 1rem; }\n"
        "    .question-head { display: flex; justify-content: space-between; gap: 1rem; align-items: center; margin-bottom: 0.8rem; flex-wrap: wrap; }\n"
        "    .question-head h2 { margin: 0; font-size: 1.1rem; }\n"
        "    .answer-badge { background: #dbeafe; color: #1e3a8a; border-radius: 999px; padding: 0.2rem 0.65rem; font-weight: 600; font-size: 0.88rem; }\n"
        "    .question-body { margin-bottom: 0.9rem; line-height: 1.55; font-family: 'Cambria Math', 'Times New Roman', serif; }\n"
        "    .option-list { margin: 0; padding-left: 0; list-style: none; }\n"
        "    .option { margin-bottom: 0.55rem; line-height: 1.45; font-family: 'Cambria Math', 'Times New Roman', serif; }\n"
        "    .option .answer-mark { font-weight: 600; color: #334155; margin-left: 0.5rem; }\n"
        "    .solution { margin-top: 0.9rem; border-top: 1px dashed #cbd5e1; padding-top: 0.7rem; font-family: 'Cambria Math', 'Times New Roman', serif; }\n"
        "    .solution h3 { margin: 0 0 0.45rem 0; font-size: 0.95rem; color: #334155; }\n"
        "    .embedded-image { max-width: 100%; height: auto; border-radius: 6px; }\n"
        "    .equation { font-family: 'Cambria Math', 'Times New Roman', serif; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <div class=\"container\">\n"
        f"    <h1>MCQ Preview for {html.escape(source_file)}</h1>\n"
        f"    <p class=\"meta\">Generated at {datetime.utcnow().isoformat()}Z</p>\n"
        f"    <p class=\"meta\">Total questions: {html.escape(str(report_data.get('total_questions', 0)))}</p>\n"
        f"    {questions_html}\n"
        "  </div>\n"
        "</body>\n"
        "</html>\n"
    )
    output_path.write_text(html_content, encoding="utf-8")
    return output_path.as_posix()


def validate_docx_upload(file: UploadFile) -> None:
    """Validate uploaded DOCX file."""
    if file is None:
        raise HTTPException(status_code=400, detail="A DOCX file upload is required")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file must include a filename")

    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are supported")

    if file.content_type not in ALLOWED_DOCX_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported content type for DOCX upload")


def parse_and_store_upload(file: UploadFile) -> dict[str, Any]:
    """Parse a DOCX upload, store in database, and generate HTML report."""
    validate_docx_upload(file)

    try:
        file.file.seek(0)
        report = parse_document(file.file, file.filename)
    except (BadZipFile, PackageNotFoundError) as exc:
        logger.warning("Rejected invalid DOCX upload: %s", file.filename)
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid DOCX document") from exc
    except RuntimeError as exc:
        logger.exception("Failed to process embedded media for document: %s", file.filename)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  
        logger.exception("Failed to parse document: %s", file.filename)
        raise HTTPException(status_code=500, detail="Failed to parse document") from exc

    try:
        with get_session() as session:
            saved_document = save_document_report(session, report)
    except SQLAlchemyError as exc:
        logger.exception("Failed to store parsed document: %s", file.filename)
        raise HTTPException(status_code=500, detail="Failed to store parsed document") from exc

    response = report_to_dict(report)
    response["import_id"] = saved_document.id
    try:
        response["output_html_file"] = write_report_html(response, file.filename)
    except OSError as exc:
        logger.exception("Failed to write output HTML file: %s", file.filename)
        raise HTTPException(status_code=500, detail="Parsed file but failed to write output HTML") from exc
    return response
