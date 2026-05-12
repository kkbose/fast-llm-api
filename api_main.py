#!/usr/bin/env python3
"""
HTTP API entrypoint (separate from CLI ``main.py``).

  python api_main.py

Or:

  uvicorn api.app:app --host 0.0.0.0 --port 8000

Configure Azure OpenAI via environment variables (same as the CLI).
"""

import os

import uvicorn


def main() -> None:
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run(
        "api.app:app",
        host=host,
        port=port,
        factory=False,
        reload=os.environ.get("API_RELOAD", "").lower() in ("1", "true", "yes"),
    )


if __name__ == "__main__":
    main()
