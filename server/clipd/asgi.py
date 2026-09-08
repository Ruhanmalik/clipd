"""Production entrypoint: builds the app from the process environment."""
import logging
import os

from .app import create_app
from .config import Config

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app(Config.from_env(os.environ))
