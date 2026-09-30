"""python -m control   (reads ENGINES, TENANTS, OVERFLOW_* and friends from the environment)"""

import logging
import os

import uvicorn

from .serve import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per scrape is noise
uvicorn.run(
    create_app(),
    host=os.environ.get("SERVE_HOST", "127.0.0.1"),
    port=int(os.environ.get("SERVE_PORT", "8080")),
    log_level="warning",
)
