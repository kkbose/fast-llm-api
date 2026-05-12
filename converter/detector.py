"""
Pipeline content gatekeeper.

Format identification is fully delegated to the LLM — no keyword patterns
are maintained here.  This module only decides whether a file is worth
sending to the LLM at all (i.e. does it look like *any* pipeline?).
"""

import re

from loguru import logger

UNKNOWN_PIPELINE_FORMAT_LABEL = "Unknown / Generic"

# JSON files must contain at least one of these field names to be
# considered a pipeline.  This avoids spending LLM tokens on random
# JSON config / data files that happen to have a .json extension.
_JSON_PIPELINE_KEYWORDS = re.compile(
    r'"(?:stages?|steps?|jobs?|tasks?|pipeline|environments?'
    r'|build|deploy|triggers?|workflow|phases?|deployPhases?|releaseNameFormat)"',
)


def _is_json_pipeline(content: str) -> bool:
    """True if *content* is JSON/JSON-array that contains pipeline-related keys."""
    if not content.lstrip().startswith(("{", "[")):
        return False
    return bool(_JSON_PIPELINE_KEYWORDS.search(content))


def is_pipeline_content(content: str) -> bool:
    """
    Return True if *content* is worth sending to the LLM for conversion.

    - Non-JSON text (YAML, Groovy, Jenkinsfile) → always True; the LLM
      will reject it gracefully if it turns out not to be a pipeline.
    - JSON → only True when at least one pipeline keyword is present,
      to avoid wasting tokens on config/data files.
    """
    stripped = content.lstrip()
    if stripped.startswith(("{", "[")):
        return _is_json_pipeline(content)
    return True


def detect_format(content: str) -> str:
    """
    Gate-keep non-pipeline files.

    Returns UNKNOWN_PIPELINE_FORMAT_LABEL for content that is clearly not
    a pipeline (e.g. JSON without any pipeline keywords), which causes the
    caller to skip the file.  All other content returns a pass-through
    constant — the LLM identifies the exact format from the content itself.
    """
    if not is_pipeline_content(content):
        logger.debug("Content does not look like a pipeline — skipping")
        return UNKNOWN_PIPELINE_FORMAT_LABEL

    logger.debug("Pipeline content detected — format identification delegated to LLM")
    return "Auto-detect"
