"""Start Ledger Terminal: python run.py  ->  http://127.0.0.1:3002"""

import os
import sys

import uvicorn
from dotenv import load_dotenv

from app.config import ROOT
from app.main import create_app

load_dotenv(ROOT / ".env")

if __name__ == "__main__":
    host = os.getenv("LEDGER_HOST", "127.0.0.1")
    # Cloud hosts (Render, Railway, Hugging Face, Fly) inject PORT.
    port = int(os.getenv("PORT") or os.getenv("LEDGER_PORT", "3002"))
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not os.getenv("LEDGER_PASSWORD"):
        sys.exit(
            f"Refusing to bind to {host} without a login: anyone who can reach it could stage and push orders.\n"
            "Set LEDGER_PASSWORD (and optionally LEDGER_USERNAME) in .env, or use LEDGER_HOST=127.0.0.1."
        )
    print(f"Ledger Terminal -> http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="info", proxy_headers=True)
