"""Configure Loguru once per process (stderr, readable defaults)."""

import sys

from loguru import logger

_state = {"configured": False}


def configure_logging() -> None:
    if _state["configured"]:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | "
            "{name}:{function}:{line} | {message}"
        ),
    )
    _state["configured"] = True
