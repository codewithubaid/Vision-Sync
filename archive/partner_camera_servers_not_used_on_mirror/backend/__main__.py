import logging
import os

import uvicorn

from .config import Settings
from .main import create_app

logging.basicConfig(level=logging.INFO)
settings = Settings.from_env()
uvicorn.run(create_app(settings), host=os.getenv("BIND_HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8000")), workers=1,
            ssl_certfile=os.getenv("TLS_CERT_FILE") or None,
            ssl_keyfile=os.getenv("TLS_KEY_FILE") or None)
