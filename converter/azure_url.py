"""Build the REST URL used for Azure OpenAI chat completions (for logging / IT allowlists)."""

from urllib.parse import quote, urlencode


def azure_chat_completions_url(endpoint: str, deployment: str, api_version: str) -> str:
    """
    Return the HTTPS URL POSTed by the OpenAI SDK for chat completions.

    Matches the standard Azure OpenAI path:
    ``{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...``
    """
    base = (endpoint or "").strip().rstrip("/")
    enc_dep = quote(deployment or "", safe="")
    query = urlencode({"api-version": api_version or ""})
    return f"{base}/openai/deployments/{enc_dep}/chat/completions?{query}"
