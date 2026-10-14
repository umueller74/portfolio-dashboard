from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from portfolio import ingest, web
from portfolio.db import connect


def create_app(db_path: str | Path, ingest_token: str, notify=None) -> FastAPI:
    """`notify` sends a crossover alert's text (see `signals.telegram_sender`); None only records crossovers."""
    app = FastAPI(title="portfolio-dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.conn = connect(db_path)
    app.state.token = ingest_token
    app.state.notify = notify
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(ingest.router)
    app.include_router(web.router)
    return app
