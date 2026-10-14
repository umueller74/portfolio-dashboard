import os

from portfolio.app import create_app
from portfolio.signals import telegram_sender

app = create_app(os.environ.get("DB_PATH", "/data/portfolio.db"), os.environ["INGEST_TOKEN"],
                 telegram_sender(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")))
