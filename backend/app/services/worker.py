"""One in-process draft worker (SPEC §3.1, §13.1).

Claims the oldest queued draft and runs per-image classify and extract.
Each finished image is checkpointed before the next vision call. A later
image failure keeps earlier pages. The next run resumes at the first image
that still lacks a checkpoint. It does not copy loans into the book.
There is no broker.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.logging import log_event
from app.models.database import AnalysisRow, run_in_session
from app.models.enums import AnalysisStatus
from app.persistence.analyses import utc_timestamp
from app.services.classification import run_analysis
from app.services.vision.base import VisionProvider

# A dead process can leave a draft here. Startup puts it back on the queue.
_INTERRUPTED_STATUSES = (
    AnalysisStatus.RUNNING.value,
    AnalysisStatus.PREPROCESSING.value,
    AnalysisStatus.CLASSIFYING.value,
    AnalysisStatus.EXTRACTING.value,
    AnalysisStatus.GROUPING.value,
    AnalysisStatus.VALIDATING.value,
)

_IDLE_POLL_SECONDS = 0.25


def requeue_interrupted_drafts(session: Session) -> int:
    """Move stuck pipeline drafts back to `queued`.

    `queued_at`, `pages_done`, `pages_total`, and `result_json` stay so a
    later run keeps FIFO order and resumes at the first image without a
    checkpoint. This does not copy loans into the book.
    """

    rows = list(
        session.scalars(
            select(AnalysisRow).where(AnalysisRow.status.in_(_INTERRUPTED_STATUSES))
        )
    )
    if not rows:
        return 0
    stamped = utc_timestamp()
    for row in rows:
        row.status = AnalysisStatus.QUEUED.value
        if row.queued_at is None:
            row.queued_at = stamped
    session.flush()
    return len(rows)


def _claim_oldest_queued(session: Session) -> str | None:
    """Mark the oldest queued draft running. Caller commits and closes."""

    row = session.scalars(
        select(AnalysisRow)
        .where(AnalysisRow.status == AnalysisStatus.QUEUED.value)
        .order_by(
            AnalysisRow.queued_at.asc(),
            AnalysisRow.created_at.asc(),
            AnalysisRow.id.asc(),
        )
        .limit(1)
    ).first()
    if row is None:
        return None
    row.status = AnalysisStatus.RUNNING.value
    session.flush()
    return row.id


async def run_one_queued_draft(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings,
    provider: VisionProvider,
) -> bool:
    """Claim one draft and run per-image checkpoints.

    Returns False when the queue is empty. The claim session is closed
    before `run_analysis`. That pipeline closes the database before each
    vision call and commits the image checkpoint after it. A later failure
    keeps earlier pages. This function does not copy loans into the book.
    """

    analysis_id = run_in_session(session_factory, _claim_oldest_queued)
    if analysis_id is None:
        return False
    await run_analysis(
        session_factory,
        analysis_id,
        settings=settings,
        provider=provider,
    )
    return True


async def worker_loop(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings,
    provider: VisionProvider,
    stop: asyncio.Event,
) -> None:
    """Re-queue drafts left mid-pipeline, then run one queued draft at a time."""

    run_in_session(session_factory, requeue_interrupted_drafts)
    while not stop.is_set():
        try:
            worked = await run_one_queued_draft(
                session_factory,
                settings=settings,
                provider=provider,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                "worker_draft_failed",
                level=logging.ERROR,
                error_code="INTERNAL_ERROR",
            )
            worked = False
        if stop.is_set():
            return
        if not worked:
            await _wait_for_stop(stop)


async def _wait_for_stop(stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=_IDLE_POLL_SECONDS)
    except TimeoutError:
        return
