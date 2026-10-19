import os

from portfolio import history
from portfolio.app import create_app
from portfolio.refresh import trigger_client
from portfolio.signals import telegram_sender

token = os.environ["INGEST_TOKEN"]
app = create_app(os.environ.get("DB_PATH", "/data/portfolio.db"), token,
                 telegram_sender(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")),
                 history.justetf, trigger_client(os.environ.get("FETCH_TRIGGER_URL"), token))
