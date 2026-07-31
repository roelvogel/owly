"""Authenticated JSON API for remote clients."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from owly.config import get_settings
from owly.db import Edition, Run, get_db, list_editions, list_runs

router = APIRouter(prefix="/api", tags=["api"])


class RunRequest(BaseModel):
    edition: Optional[Literal["morning", "evening"]] = None


class RunResponse(BaseModel):
    ok: bool
    status: Literal["started", "busy"]


class RunSummary(BaseModel):
    id: int
    edition_slot: str
    started_at: str
    finished_at: Optional[str]
    status: str
    error: Optional[str]
    input_tokens: int
    output_tokens: int
    duration_ms: int


class StatusResponse(BaseModel):
    run_in_progress: bool
    latest_run: Optional[RunSummary] = None


class EditionSummary(BaseModel):
    id: int
    run_id: int
    edition_key: str
    filename: str
    title: str
    created_at: str


class EditionsResponse(BaseModel):
    editions: list[EditionSummary]


class EditionContentResponse(BaseModel):
    filename: str
    markdown: str


def _edition_to_summary(edition: Edition) -> EditionSummary:
    return EditionSummary(
        id=edition.id,
        run_id=edition.run_id,
        edition_key=edition.edition_key,
        filename=Path(edition.file_path).name,
        title=edition.title,
        created_at=edition.created_at,
    )


def _run_to_summary(run: Run) -> RunSummary:
    return RunSummary(
        id=run.id,
        edition_slot=run.edition_slot,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status,
        error=run.error,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        duration_ms=run.duration_ms,
    )


def _validate_filename(filename: str) -> None:
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")


async def verify_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
    authorization: Optional[str] = Header(None),
) -> None:
    settings = get_settings()
    if not settings.owly_api_key:
        raise HTTPException(status_code=503, detail="OWLY_API_KEY is not configured")
    key = x_api_key
    if not key and authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    if not key or key != settings.owly_api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@router.get("/status", response_model=StatusResponse)
def api_status(_: None = Depends(verify_api_key)) -> StatusResponse:
    from owly.dashboard import _run_in_progress

    with get_db() as conn:
        runs = list_runs(conn, limit=1)
    latest = _run_to_summary(runs[0]) if runs else None
    return StatusResponse(run_in_progress=_run_in_progress, latest_run=latest)


@router.post("/run", response_model=RunResponse)
def api_run(
    body: RunRequest,
    _: None = Depends(verify_api_key),
) -> RunResponse:
    from owly.dashboard import start_run

    status = start_run(body.edition)
    return RunResponse(ok=True, status=status)


@router.get("/editions", response_model=EditionsResponse)
def api_editions(
    _: None = Depends(verify_api_key),
    limit: int = Query(default=50, ge=1, le=200),
) -> EditionsResponse:
    with get_db() as conn:
        editions = list_editions(conn, limit=limit)
    return EditionsResponse(editions=[_edition_to_summary(e) for e in editions])


@router.get("/editions/{filename}", response_model=EditionContentResponse)
def api_edition_content(
    filename: str,
    _: None = Depends(verify_api_key),
) -> EditionContentResponse:
    _validate_filename(filename)
    settings = get_settings()
    path = settings.output_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Edition not found")
    return EditionContentResponse(
        filename=filename,
        markdown=path.read_text(encoding="utf-8"),
    )
