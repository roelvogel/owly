"""Local FastAPI dashboard for managing sources and reading editions."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import markdown
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from owly.api import router as api_router
from owly.config import get_settings
from owly.db import (
    add_source,
    delete_source,
    get_db,
    init_db,
    list_editions,
    list_runs,
    list_sources,
    toggle_source,
)
from owly.run_state import is_run_in_progress, start_run

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    get_settings().ensure_dirs()
    yield


app = FastAPI(title="Owly Dashboard", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["basename"] = lambda p: Path(p).name

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _md_to_html(text: str) -> str:
    return markdown.markdown(
        text,
        extensions=["extra", "sane_lists"],
        output_format="html5",
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with get_db() as conn:
        editions = list_editions(conn, limit=30)
        runs = list_runs(conn, limit=10)
        sources = list_sources(conn)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "editions": editions,
            "runs": runs,
            "sources": sources,
            "run_in_progress": is_run_in_progress(),
        },
    )


@app.get("/editions/{filename}", response_class=HTMLResponse)
def read_edition(request: Request, filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    settings = get_settings()
    path = settings.output_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Edition not found")
    content = path.read_text(encoding="utf-8")
    html = _md_to_html(content)
    return templates.TemplateResponse(
        "edition.html",
        {"request": request, "filename": filename, "content_html": html},
    )


@app.post("/sources/add")
def add_source_route(
    source_type: str = Form(...),
    value: str = Form(...),
    label: Optional[str] = Form(None),
    use_morss: Optional[str] = Form(None),
):
    if source_type not in ("rss", "stock", "topic"):
        raise HTTPException(status_code=400, detail="Invalid source type")
    with get_db() as conn:
        add_source(
            conn,
            source_type,
            value,
            label=label,
            use_morss=bool(use_morss),
        )
    return RedirectResponse(url="/", status_code=303)


@app.post("/sources/{source_id}/delete")
def delete_source_route(source_id: int):
    with get_db() as conn:
        delete_source(conn, source_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/sources/{source_id}/toggle")
def toggle_source_route(source_id: int, enabled: str = Form(...)):
    with get_db() as conn:
        toggle_source(conn, source_id, enabled == "1")
    return RedirectResponse(url="/", status_code=303)


@app.post("/run")
def trigger_run(edition: Optional[str] = Form(None)):
    status = start_run(edition)
    msg = "run_busy" if status == "busy" else "run_started"
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    uvicorn.run(
        "owly.dashboard:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
