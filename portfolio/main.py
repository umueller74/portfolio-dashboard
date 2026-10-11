import os

from portfolio.app import create_app

app = create_app(os.environ.get("DB_PATH", "/data/portfolio.db"), os.environ["INGEST_TOKEN"])
