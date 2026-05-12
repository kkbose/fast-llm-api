"""Parse and validate the LLM response into clean Harness YAML."""

import re

import yaml
from loguru import logger

# ── LLM output repairs (run before yaml.safe_load) ───────────────────────────

_SCALAR_LINE_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:\s*)(.*)$")

_SHELL_LINE_RE = re.compile(r"^(\s*)shell:\s+(Bash|Powershell|PowerShell)\s*$")
_IMAGE_LINE_RE = re.compile(r"^(\s*)image:\s+\S.*$")
_CONNECTOR_REF_LINE_RE = re.compile(r"^(\s*)connectorRef:\s+\S.*$")
_SPEC_KEY_LINE_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:")

# Legitimate next sibling keys under Run step ``spec:`` — do not wrap these after an anchor line.
_ALLOWED_AFTER_CONNECTOR = frozenset({
    "image",
    "shell",
    "command",
    "envVariables",
    "privileged",
    "resources",
    "outputVariables",
    "reports",
    "timeout",
    "when",
    "description",
})
_ALLOWED_AFTER_IMAGE = frozenset({
    "shell",
    "command",
    "envVariables",
    "privileged",
    "resources",
    "outputVariables",
    "reports",
    "timeout",
    "when",
    "description",
})
_ALLOWED_AFTER_SHELL = frozenset({
    "command",
    "envVariables",
    "privileged",
    "resources",
    "outputVariables",
    "reports",
    "timeout",
    "when",
    "description",
})
_ANCHOR_ALLOWED = {
    "connectorRef": _ALLOWED_AFTER_CONNECTOR,
    "image": _ALLOWED_AFTER_IMAGE,
    "shell": _ALLOWED_AFTER_SHELL,
}


def repair_yaml_colon_scalars(text: str) -> str:
    """
    Quote scalar values that contain ``\": \"`` — otherwise PyYAML treats the second ``:``
    as a nested mapping (e.g. ``name: Copy Files to: $(dest)``).
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        m = _SCALAR_LINE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        indent, key, sep, value = m.groups()
        stripped = value.strip()
        if not stripped:
            out_lines.append(line)
            continue
        lead = stripped[0]
        if lead in "\"'{[&*|>!":
            out_lines.append(line)
            continue
        if ": " not in value:
            out_lines.append(line)
            continue
        inner = stripped.replace("\\", "\\\\").replace('"', '\\"')
        out_lines.append(f'{indent}{key}{sep}"{inner}"')
    return "\n".join(out_lines)


def _spec_key_from_line(line: str) -> str | None:
    m = _SPEC_KEY_LINE_RE.match(line)
    return m.group(2) if m else None


def repair_run_step_spec_orphan_commands(text: str) -> str:
    """
    Wrap loose shell lines under Run ``step.spec`` in ``command: |-``.

    Models often emit ``npm``/``docker``/``mvn`` **before** ``shell:`` or **without** ``shell:``,
    so anchoring only on ``shell:`` misses those cases. We also anchor after ``connectorRef:``
    and ``image:`` when the next sibling is not a normal spec key.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        anchor_kind: str | None = None
        m = _SHELL_LINE_RE.match(line)
        if m:
            anchor_kind = "shell"
        else:
            m = _IMAGE_LINE_RE.match(line)
            if m:
                anchor_kind = "image"
            else:
                m = _CONNECTOR_REF_LINE_RE.match(line)
                if m:
                    anchor_kind = "connectorRef"

        if anchor_kind is None or m is None:
            out.append(line)
            i += 1
            continue

        base_indent = len(m.group(1))
        out.append(line)
        i += 1

        blanks_between: list[str] = []
        while i < len(lines) and not lines[i].strip():
            blanks_between.append(lines[i])
            i += 1

        if i >= len(lines):
            out.extend(blanks_between)
            break

        nxt = lines[i]
        if re.match(r"^\s*command:\s*", nxt):
            out.extend(blanks_between)
            continue

        nxt_indent = len(nxt) - len(nxt.lstrip())
        nxt_key = _spec_key_from_line(nxt)
        allowed = _ANCHOR_ALLOWED[anchor_kind]
        if (
            nxt_key is not None
            and nxt_indent == base_indent
            and nxt_key in allowed
        ):
            out.extend(blanks_between)
            continue

        orphans: list[str] = []
        j = i
        if blanks_between:
            orphans.extend(blanks_between)

        while j < len(lines):
            ln = lines[j]
            if not ln.strip():
                orphans.append(ln)
                j += 1
                continue
            ind = len(ln) - len(ln.lstrip())
            if ind < base_indent:
                break
            keyname = _spec_key_from_line(ln)
            if ind == base_indent and keyname is not None:
                break
            if ind == base_indent and keyname is None:
                orphans.append(ln)
                j += 1
                continue
            orphans.append(ln)
            j += 1

        while orphans and not orphans[-1].strip():
            orphans.pop()

        if not orphans:
            out.extend(blanks_between)
            continue

        logger.debug(
            "repair_run_step_spec_orphan_commands (after {}): wrapped {} line(s) under command: |-",
            anchor_kind,
            len(orphans),
        )
        cmd_indent = " " * base_indent
        body_indent = " " * (base_indent + 2)
        out.append(f"{cmd_indent}command: |-")
        for ol in orphans:
            if not ol.strip():
                out.append("")
                continue
            out.append(body_indent + ol.strip())
        i = j

    return "\n".join(out)


def repair_llm_yaml(text: str) -> str:
    """Apply structural fixes to LLM-produced YAML before parsing."""
    text = repair_yaml_colon_scalars(text)
    text = repair_run_step_spec_orphan_commands(text)
    return text


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