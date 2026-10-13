"""SQLite schema for books, drafts, and images (SPEC §15.1)."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

from fastapi import FastAPI, Request
from sqlalchemy import ForeignKey, Integer, Text, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.core.config import Settings

_T = TypeVar("_T")


class Base(DeclarativeBase):
    pass


class BookRow(Base):
    __tablename__ = "books"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class AnalysisRow(Base):
    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    book_id: Mapped[str] = mapped_column(Text, ForeignKey("books.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    pages_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    pages_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ImageRow(Base):
    __tablename__ = "images"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    analysis_id: Mapped[str] = mapped_column(Text, ForeignKey("analyses.id"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


def resolve_sqlite_url(database_url: str) -> str:
    """Place relative SQLite files under the repository root."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return database_url
    location = database_url[len(prefix) :]
    if location in {":memory:", ""} or location.startswith("file:"):
        return database_url
    path = Path(location)
    if not path.is_absolute():
        repo_root = Path(__file__).resolve().parents[3]
        path = (repo_root / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return prefix + path.as_posix()


def _enable_sqlite_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_sqlite_engine(database_url: str) -> Engine:
    engine = create_engine(
        resolve_sqlite_url(database_url),
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _ensure_analysis_queue_columns(engine: Engine) -> None:
    """Add queue columns when the analyses table was created before V1.1."""
    inspector = inspect(engine)
    if not inspector.has_table("analyses"):
        return
    present = {column["name"] for column in inspector.get_columns("analyses")}
    statements: list[str] = []
    if "queued_at" not in present:
        statements.append("ALTER TABLE analyses ADD COLUMN queued_at TEXT")
    if "pages_done" not in present:
        statements.append("ALTER TABLE analyses ADD COLUMN pages_done INTEGER NOT NULL DEFAULT 0")
    if "pages_total" not in present:
        statements.append("ALTER TABLE analyses ADD COLUMN pages_total INTEGER NOT NULL DEFAULT 0")
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def run_in_session(session_factory: sessionmaker[Session], work: Callable[[Session], _T]) -> _T:
    """Commit and close before the caller does anything else.

    A vision call must run only after this returns, using plain data rather
    than a live SQLAlchemy session.
    """
    session = session_factory()
    try:
        result = work(session)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database(application: FastAPI, settings: Settings) -> None:
    engine = create_sqlite_engine(settings.loanlens_database_url)
    Base.metadata.create_all(engine)
    _ensure_analysis_queue_columns(engine)
    application.state.engine = engine
    application.state.session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )


def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
