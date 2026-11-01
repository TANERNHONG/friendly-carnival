"""Start CARNIVAL from any working directory: python path/to/run.py"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

import uvicorn  # noqa: E402

from app.config import settings  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")
