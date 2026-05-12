"""Credential checks for CLI/API."""

import os

from .constants import DEFAULT_AZURE_OPENAI_DEPLOYMENT

REQUIRED_AZURE_ENV_VARS: tuple[str, ...] = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "OPENAI_API_VERSION",
)


def missing_azure_openai_env_vars() -> list[str]:
    """Return env var names that are unset or blank."""
    return [
        name for name in REQUIRED_AZURE_ENV_VARS
        if not os.environ.get(name, "").strip()
    ]


def azure_credentials_help_text() -> str:
    """Human-readable hint when credentials are missing (CLI stderr / API detail)."""
    return (
        "Required environment variables:\n"
        "  AZURE_OPENAI_API_KEY\n"
        "  AZURE_OPENAI_ENDPOINT\n"
        "  OPENAI_API_VERSION\n"
        f"Optional: AZURE_OPENAI_DEPLOYMENT (default {DEFAULT_AZURE_OPENAI_DEPLOYMENT}).\n"
        "For local dev only, you may use a .env file (see .env.example)."
    )
