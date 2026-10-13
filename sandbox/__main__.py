from __future__ import annotations

import argparse
from urllib.parse import urlparse

import uvicorn

from . import seed
from .config import base_url


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Northwind Traders sandbox.")
    parser.add_argument("--reset", action="store_true", help="re-seed the database and workspace first")
    args = parser.parse_args()
    if args.reset:
        seed.reset()
    url = urlparse(base_url())
    uvicorn.run("sandbox.app:app", host=url.hostname or "127.0.0.1", port=url.port or 8700, log_level="warning")


if __name__ == "__main__":
    main()
