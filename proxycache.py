"""proxycache launcher: python proxycache.py"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import uvicorn

from proxycache.app import app
from proxycache import config

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config.SERVER_HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
    )
