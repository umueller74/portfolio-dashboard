from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from portfolio import ingest, web
from portfolio.db import connect


def create_app(db_path: str | Path, ingest_token: str, notify=None, history=None, trigger=None) -> FastAPI:
    """`notify` sends a crossover alert's text (see `signals.telegram_sender`); None only records crossovers.
    `history` fetches an ETF's older closes (see `history.justetf`); None skips the backfill.
    `trigger` reaches the fetcher host's listener (see `refresh.trigger_client`); None hides "Fetch now"."""
    app = FastAPI(title="portfolio-dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.conn = connect(db_path)
    app.state.token = ingest_token
    app.state.notify = notify
    app.state.history = history
    app.state.trigger = trigger
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(ingest.router)
    app.include_router(web.router)
    return app
