from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, selectinload, sessionmaker

from src.parser import DocumentReport

load_dotenv()


def _load_env_file() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+pg8000://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+pg8000://", 1)
    if database_url.startswith("postgresql+psycopg2://"):
        return database_url.replace("postgresql+psycopg2://", "postgresql+pg8000://", 1)
    return database_url


_load_env_file()

DEFAULT_DATABASE_URL = "postgresql+pg8000://postgres:password@localhost:5432/ques"
DATABASE_URL = _normalize_database_url(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))


class Base(DeclarativeBase):
    pass


class ImportDocument(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file: Mapped[str] = mapped_column(String(255))
    total_questions: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    questions: Mapped[list["Question"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    question_text: Mapped[str] = mapped_column(Text)
    question_html: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(String(10), nullable=True)
    solution_text: Mapped[str] = mapped_column(Text, default="")
    solution_html: Mapped[str] = mapped_column(Text, default="")
    display_order: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[ImportDocument] = relationship(back_populates="questions")
    options: Mapped[list["QuestionOption"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
    )


class QuestionOption(Base):
    __tablename__ = "options"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(Text)
    html: Mapped[str] = mapped_column(Text)
    display_order: Mapped[int] = mapped_column(Integer)

    question: Mapped[Question] = relationship(back_populates="options")


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(DATABASE_URL, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _session_factory


def init_db() -> None:
    Base.metadata.create_all(bind=get_engine())


@contextmanager
def get_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def save_document_report(session: Session, report: DocumentReport) -> ImportDocument:
    document = ImportDocument(
        source_file=report.source_file,
        total_questions=len(report.questions),
    )
    session.add(document)
    session.flush()

    for question_index, parsed_question in enumerate(report.questions, start=1):
        question = Question(
            document=document,
            number=parsed_question.number,
            question_text=parsed_question.question_text,
            question_html=parsed_question.question_html,
            answer=parsed_question.answer,
            solution_text=parsed_question.solution_text,
            solution_html=parsed_question.solution_html,
            display_order=question_index,
        )
        session.add(question)
        session.flush()

        for option_index, parsed_option in enumerate(parsed_question.options, start=1):
            session.add(
                QuestionOption(
                    question=question,
                    label=parsed_option.label,
                    text=parsed_option.text,
                    html=parsed_option.html,
                    display_order=option_index,
                )
            )

    session.flush()
    session.refresh(document)
    return document


def option_to_dict(option: QuestionOption) -> dict[str, str | int]:
    return {
        "id": option.id,
        "label": option.label,
        "text": option.text,
        "html": option.html,
        "display_order": option.display_order,
    }


def question_to_dict(question: Question) -> dict[str, str | int | None | list[dict[str, str | int]]]:
    return {
        "id": question.id,
        "document_id": question.document_id,
        "number": question.number,
        "question_text": question.question_text,
        "question_html": question.question_html,
        "answer": question.answer,
        "solution_text": question.solution_text,
        "solution_html": question.solution_html,
        "display_order": question.display_order,
        "options": [option_to_dict(option) for option in sorted(question.options, key=lambda item: item.display_order)],
    }


def document_to_dict(document: ImportDocument) -> dict[str, object]:
    return {
        "id": document.id,
        "source_file": document.source_file,
        "total_questions": document.total_questions,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "questions": [question_to_dict(question) for question in sorted(document.questions, key=lambda item: item.display_order)],
    }


def list_saved_documents(session: Session) -> list[dict[str, object]]:
    statement = (
        select(ImportDocument)
        .options(
            selectinload(ImportDocument.questions).selectinload(Question.options),
        )
        .order_by(ImportDocument.id.desc())
    )
    documents = session.execute(statement).scalars().all()
    return [document_to_dict(document) for document in documents]
