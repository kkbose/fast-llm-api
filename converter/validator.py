"""Parse and validate the LLM response into clean Harness YAML."""

import re

import yaml
from loguru import logger


def _flatten_llm_content(raw: object) -> str:
    """
    LangChain model responses may use ``content`` as a str or as structured blocks.

    Gemini often returns ``[{"type": "text", "text": "```yaml\\n...```"}]``;
    using ``str(list)`` breaks fenced-YAML extraction — flatten first.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(_flatten_llm_content(item) for item in raw)
    if isinstance(raw, dict):
        text = raw.get("text")
        if isinstance(text, str):
            return text
        nested = raw.get("content")
        if nested is not None:
            return _flatten_llm_content(nested)
        return ""
    return str(raw)


def extract_yaml(raw: object) -> str:
    """Pull YAML out of a ```yaml … ``` fence, or return the raw text."""
    text = _flatten_llm_content(raw)
    match = re.search(r"```(?:yaml|yml)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        logger.debug("extract_yaml: found fenced code block")
        return match.group(1).strip()
    logger.debug("extract_yaml: no fence; using trimmed raw text")
    return text.strip()


def validate_harness_yaml(content: str) -> tuple[bool, str]:
    """
    Return (True, "") if *content* is YAML with ``pipeline.stages`` shape.

    Otherwise return (False, human-readable reason for logs / errors).
    """
    stripped = (content or "").strip()
    if not stripped:
        return False, "empty YAML text after extracting from the LLM response"

    try:
        data = yaml.safe_load(stripped)
    except yaml.YAMLError as exc:
        return False, f"YAML parse error: {exc}"

    if data is None:
        return False, "YAML document is null"

    if not isinstance(data, dict):
        return False, f"root must be a mapping, got {type(data).__name__}"

    if "pipeline" not in data:
        return False, "missing top-level key 'pipeline'"

    pipe = data["pipeline"]
    if not isinstance(pipe, dict):
        return False, f"'pipeline' must be a mapping, got {type(pipe).__name__}"

    if "stages" not in pipe:
        return False, "missing 'pipeline.stages' (required Harness CI shape)"

    return True, ""


def is_valid(content: str) -> bool:
    """Return True if content parses as YAML and has the required pipeline.stages shape."""
    ok, _ = validate_harness_yaml(content)
    return ok


def validate_harness_step(content: str) -> tuple[bool, str]:
    """
    Return (True, "") if *content* is YAML with a top-level ``step`` key
    containing at least a ``type`` and ``spec`` sub-key.
    """
    stripped = (content or "").strip()
    if not stripped:
        return False, "empty YAML text after extracting from the LLM response"

    try:
        data = yaml.safe_load(stripped)
    except yaml.YAMLError as exc:
        return False, f"YAML parse error: {exc}"

    if data is None:
        return False, "YAML document is null"

    if not isinstance(data, dict):
        return False, f"root must be a mapping, got {type(data).__name__}"

    if "step" not in data:
        return False, "missing top-level key 'step'"

    step = data["step"]
    if not isinstance(step, dict):
        return False, f"'step' must be a mapping, got {type(step).__name__}"

    if "spec" not in step:
        return False, "missing 'step.spec'"

    return True, ""
