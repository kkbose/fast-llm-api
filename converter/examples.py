"""
Pipeline example library for few-shot prompting.

Add new examples with ``add_example()`` — it sanitizes the input file
automatically before storing it.  The prompt picks them up at startup
via ``build_examples_prompt()``.

Quick usage::

    from converter.examples import add_example

    add_example(
        pipeline_type="CI",
        input_path="raw_input.yml",
        output_path="expected_harness_output.yml",
        title="My New Pipeline Type",
    )
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from loguru import logger

_EXAMPLES_DIR = Path(__file__).parent / "examples"

# ── Sanitiser ─────────────────────────────────────────────────────────────────
# Replaces UUIDs, URLs, and sensitive-field values with generic placeholders.
# Used to scrub raw pipeline files before they are stored as examples.

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s'\",>\]\}]+")

_YAML_KV_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z][A-Za-z0-9_\-]*)(?P<sep>[ \t]*:[ \t]*)(?P<value>.+)$",
    re.MULTILINE,
)
_JSON_KV_RE = re.compile(
    r'"(?P<key>[A-Za-z][A-Za-z0-9_\-]*)"[ \t]*:[ \t]*"(?P<value>[^"\\]{3,})"',
)

_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "password", "passwd", "secret", "token", "apikey", "apitoken", "authtoken",
    "credential", "credentials", "privatekey", "accesskey", "secretkey",
    "securefile", "securefilepath", "connectionstring",
    "dockerregistryendpoint", "registryendpoint", "endpoint",
})


def _norm(key: str) -> str:
    return key.lower().replace("-", "").replace("_", "")


def sanitize(content: str) -> str:
    """
    Replace UUIDs, URLs, and sensitive YAML/JSON field values with safe
    generic placeholders so the file can be stored in the examples library.
    """
    seen_uuids: dict[str, str] = {}
    seen_urls: dict[str, str] = {}

    def _uuid(m: re.Match) -> str:
        v = m.group(0).lower()
        if v not in seen_uuids:
            seen_uuids[v] = f"<uuid-{len(seen_uuids) + 1}>"
        return seen_uuids[v]

    def _url(m: re.Match) -> str:
        v = m.group(0)
        if v not in seen_urls:
            seen_urls[v] = f"<url-{len(seen_urls) + 1}>"
        return seen_urls[v]

    result = _UUID_RE.sub(_uuid, content)
    result = _URL_RE.sub(_url, result)

    def _yaml_kv(m: re.Match) -> str:
        val = m.group("value").strip()
        if (
            _norm(m.group("key")) not in _SENSITIVE_KEYS
            or not val
            or val in ("true", "false", "null", "~")
            or val[0] in ("<", "[", "*", "&")
        ):
            return m.group(0)
        return m.group("indent") + m.group("key") + m.group("sep") + "<masked>"

    result = _YAML_KV_RE.sub(_yaml_kv, result)

    def _json_kv(m: re.Match) -> str:
        val = m.group("value")
        if _norm(m.group("key")) not in _SENSITIVE_KEYS or val.startswith("<"):
            return m.group(0)
        return m.group(0).replace(f'"{val}"', '"<masked>"', 1)

    result = _JSON_KV_RE.sub(_json_kv, result)
    return result


# ── add_example ───────────────────────────────────────────────────────────────

def add_example(
    pipeline_type: str,
    input_path: str | Path,
    output_path: str | Path,
    title: str = "",
) -> Path:
    """
    Sanitize *input_path* and store it alongside *output_path* in the
    examples library so the prompt picks it up automatically.

    Parameters
    ----------
    pipeline_type : ``"CI"`` or ``"CD"``
    input_path    : raw source pipeline file — **sanitized before storing**
    output_path   : expected Harness YAML — stored as-is
    title         : human-readable label shown in the prompt (defaults to
                    the input file stem)

    Returns the folder where the example was saved.
    """
    ptype = pipeline_type.strip().upper()
    if ptype not in ("CI", "CD"):
        raise ValueError(f"pipeline_type must be 'CI' or 'CD', got {pipeline_type!r}")

    src = Path(input_path).resolve()
    out = Path(output_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")
    if not out.is_file():
        raise FileNotFoundError(f"Output file not found: {out}")

    _EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    # Auto-number based on existing folders
    existing = sorted(
        d for d in _EXAMPLES_DIR.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    )
    next_n = len(existing) + 1

    slug = re.sub(r"[^a-z0-9]+", "_", (title or src.stem).lower()).strip("_")
    folder = _EXAMPLES_DIR / f"{next_n:02d}_{ptype}_{slug}"
    folder.mkdir(parents=True, exist_ok=True)

    # Sanitize and write input
    clean = sanitize(src.read_text(encoding="utf-8"))
    (folder / f"input{src.suffix or '.yml'}").write_text(clean, encoding="utf-8")

    # Write output as-is
    (folder / "output.yml").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")

    # Write metadata
    meta = {"type": ptype, "title": title or src.stem}
    (folder / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    logger.info("Example '{}' saved to {}", folder.name, folder)
    return folder


# ── build_examples_prompt ─────────────────────────────────────────────────────

def build_examples_prompt() -> str:
    """
    Read every example pair from ``converter/examples/`` (sorted by folder
    name) and return a ``## Few-shot examples`` section string ready to be
    appended to the system prompt.

    Returns an empty string if the examples directory is empty or missing.
    """
    if not _EXAMPLES_DIR.is_dir():
        return ""

    parts: list[str] = []
    n = 0

    for folder in sorted(_EXAMPLES_DIR.iterdir()):
        if not folder.is_dir() or folder.name.startswith((".", "_")):
            continue

        out_file = folder / "output.yml"
        if not out_file.is_file():
            continue

        in_file: Path | None = None
        for candidate in sorted(folder.iterdir()):
            if candidate.stem == "input" and candidate.suffix in {
                ".yml", ".yaml", ".json", ".groovy", ".txt"
            }:
                in_file = candidate
                break
        if in_file is None:
            continue

        title = folder.name
        ptype = ""
        meta_file = folder / "meta.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                title = meta.get("title", title)
                ptype = meta.get("type", "")
            except (json.JSONDecodeError, OSError):
                pass

        n += 1
        label = f"{ptype} — {title}" if ptype else title
        lang = "json" if in_file.suffix == ".json" else "yaml"
        in_text = in_file.read_text(encoding="utf-8").strip()
        out_text = out_file.read_text(encoding="utf-8").strip()

        parts.append(f"### INPUT {n} ({label})\n\n```{lang}\n{in_text}\n```")
        parts.append(f"### OUTPUT {n} (Harness)\n\n```yaml\n{out_text}\n```")

    if not parts:
        return ""

    return "## Few-shot examples\n\n" + "\n\n".join(parts) + "\n"
