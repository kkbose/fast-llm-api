#!/usr/bin/env python3
"""CLI — convert any CI/CD pipeline file (or folder of files) to Harness YAML."""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

import json

from converter import HarnessYamlValidationError, convert, convert_step
from converter.azure_credentials import azure_credentials_help_text, missing_azure_openai_env_vars
from converter.constants import DEFAULT_AZURE_OPENAI_DEPLOYMENT
from converter.detector import UNKNOWN_PIPELINE_FORMAT_LABEL, detect_format

# Extensions / filenames worth even opening when scanning a folder.
_PIPELINE_EXTS = {".yml", ".yaml", ".groovy", ".json"}
_PIPELINE_NAMES = {"jenkinsfile"}


def _check_llm_credentials() -> None:
    """Abort early with a clear message if Azure OpenAI creds are missing."""
    missing = missing_azure_openai_env_vars()
    if not missing:
        return

    sys.stderr.write(
        "Missing Azure OpenAI credential(s): " + ", ".join(missing) + "\n\n"
        + azure_credentials_help_text()
        + "\n\n"
        "Examples (shell):\n"
        "  PowerShell : $env:AZURE_OPENAI_API_KEY = \"...\"\n"
        "  bash/zsh   : export AZURE_OPENAI_API_KEY=\"...\"\n"
        "  Docker     : docker run -e AZURE_OPENAI_API_KEY=\"...\" ...\n\n"
        "Do not rely on .env files in production; inject secrets via your orchestrator.\n"
        "For local development, copy .env.example to .env.\n"
    )
    sys.exit(2)


def _is_candidate_file(path: Path) -> bool:
    """Quick pre-filter so we don't read every random file in a folder."""
    if not path.is_file():
        return False
    if path.suffix.lower() in _PIPELINE_EXTS:
        return True
    if path.name.lower() in _PIPELINE_NAMES:
        return True
    return False


def _resolved_output_path(out_dir: Path, relative: Path) -> Path | None:
    """Resolve output path and ensure it stays under *out_dir* (path traversal hardening)."""
    try:
        base = out_dir.resolve()
        out_file = (base / relative).resolve()
        out_file.relative_to(base)
        return out_file
    except (OSError, ValueError):
        logger.warning("Skipping unsafe output path (outside output folder): {}", relative)
        return None


def _convert_one(in_path: Path, out_path: Path | None, deployment: str,
                 retries: int, encrypt: bool) -> bool:
    """Convert a single file. Returns True if processed, False if skipped."""
    logger.info("Reading input file: {}", in_path)
    try:
        content = in_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        logger.warning("Skipping {} (could not read as text: {})", in_path, e)
        return False

    fmt = detect_format(content)
    if fmt == UNKNOWN_PIPELINE_FORMAT_LABEL:
        logger.info("Skipping {} (does not look like a known CI/CD pipeline)", in_path)
        return False

    try:
        result = convert(content, deployment=deployment, max_retries=retries, encrypt=encrypt)
    except HarnessYamlValidationError:
        logger.error(
            "Conversion failed for {} — LLM output did not validate as Harness YAML.",
            in_path,
        )
        raise

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result + "\n", encoding="utf-8")
        logger.info("Wrote Harness YAML to {}", out_path)
    else:
        print(result)
    return True


_USAGE_HINT = """\
No input provided. Please specify a pipeline file, a folder, or raw text.

  File mode:
    python main.py <pipeline-file> [-o <output-file>]
    e.g.  python main.py SamplePipeline.yml          →  SamplePipeline_output.yml (same folder)
          python main.py SamplePipeline.yml -o out/harness.yml

  Folder mode:
    python main.py <input-folder> [-o <output-folder>]
    e.g.  python main.py ./pipelines                 →  ./pipelines/output/
          python main.py ./pipelines -o ./harness-out

  Text mode (inline — use \n for newlines):
    python main.py --text "trigger:\n  branches:\n    include:\n      - main\npool:\n  vmImage: ubuntu-latest"

  Text mode (stdin pipe — recommended for multi-line):
    cat SamplePipeline.yml | python main.py -
    Get-Content SamplePipeline.yml | python main.py -   # PowerShell

Run `python main.py --help` for the full list of options.
"""


def _default_file_output(in_path: Path) -> Path:
    """Return <parent>/<stem>_output.yml next to the input file."""
    return in_path.parent / f"{in_path.stem}_output.yml"


def _parse_ndjson(text: str) -> list[dict]:
    """Parse a sequence of whitespace-separated JSON objects (NDJSON / JSON-seq).

    Handles input where multiple bare JSON objects are written one after
    another — not wrapped in an array — as produced by migration-analysis tools.
    Also accepts a proper JSON array ``[{...}, {...}]``.
    """
    text = text.strip()
    if text.startswith("["):
        return json.loads(text)

    objects: list[dict] = []
    depth = 0
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buf.append(line)
        depth += stripped.count("{") - stripped.count("}")
        if depth == 0 and buf:
            try:
                objects.append(json.loads("\n".join(buf)))
            except json.JSONDecodeError as exc:
                logger.warning("Skipping malformed JSON object: {}", exc)
            buf = []
    return objects


def _run_steps_mode(
    text: str,
    retries: int,
    encrypt: bool,
) -> None:
    """Convert each step in an NDJSON steps list and print a JSON array to stdout.

    Output format (printed to stdout so JS / any caller can capture it):

        [
          {"index": 0, "displayName": "...", "taskName": "...", "yaml": "step:\\n  ..."},
          {"index": 1, "displayName": "...", "taskName": "...", "yaml": "step:\\n  ..."},
          ...
        ]

    Steps with ``enabled: false`` are still converted (the LLM adds
    ``when: condition: "false"``). Conversion errors are captured per-step
    and surfaced as ``{"index": N, "error": "<reason>"}`` in the output list
    so a single failure never aborts the whole batch.
    """
    steps = _parse_ndjson(text)
    if not steps:
        sys.stderr.write("Error: no valid JSON step objects found in input.\n")
        sys.exit(1)

    logger.info("Steps mode — {} step(s) to convert", len(steps))
    results: list[dict] = []

    for step in steps:
        idx = step.get("index", "?")
        display = step.get("displayName", "")
        task = step.get("taskName", "")
        logger.info("Converting step {} — {} ({})", idx, display or task, task)

        step_json = json.dumps(step, indent=2)
        try:
            yaml_snippet = convert_step(step_json, max_retries=retries, encrypt=encrypt)
            results.append({
                "index": idx,
                "displayName": display,
                "taskName": task,
                "yaml": yaml_snippet,
            })
        except HarnessYamlValidationError as exc:
            logger.error("Step {} failed validation: {}", idx, exc)
            results.append({
                "index": idx,
                "displayName": display,
                "taskName": task,
                "error": str(exc),
            })

    print(json.dumps(results, indent=2))


def _timestamped_output() -> Path:
    """Return out_<YYYYmmdd_HHMMSS>.yml in the current working directory."""
    return Path(f"out_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yml")


def _run_text_mode(
    text: str,
    output: str | None,
    deployment: str,
    retries: int,
    encrypt: bool,
    stdout: bool = False,
) -> None:
    if detect_format(text) == UNKNOWN_PIPELINE_FORMAT_LABEL:
        sys.stderr.write(
            "Error: Could not detect a CI/CD pipeline in the supplied text.\n"
            "Ensure the text looks like Azure DevOps, Jenkins, GitHub Actions, GitLab CI, etc.\n"
        )
        sys.exit(1)

    logger.info("Pipeline format will be auto-detected by the LLM")
    try:
        result = convert(text, deployment=deployment, max_retries=retries, encrypt=encrypt)
    except HarnessYamlValidationError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.exit(1)

    if stdout:
        print(result)
    else:
        out_path = Path(output) if output else _timestamped_output()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result + "\n", encoding="utf-8")
        logger.info("Wrote Harness YAML to {}", out_path)


def _run_file_mode(
    in_path: Path,
    output: str | None,
    deployment: str,
    retries: int,
    encrypt: bool,
    stdout: bool = False,
) -> None:
    if stdout:
        # Read → convert → print, bypassing the file-writing path
        _run_text_mode(
            in_path.read_text(encoding="utf-8"),
            output=None,
            deployment=deployment,
            retries=retries,
            encrypt=encrypt,
            stdout=True,
        )
        return
    out_path = Path(output) if output else _default_file_output(in_path)
    logger.info("Output will be written to: {}", out_path)
    try:
        _convert_one(in_path, out_path, deployment, retries, encrypt)
    except HarnessYamlValidationError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.exit(1)


def _run_folder_mode(
    in_path: Path,
    output: str | None,
    deployment: str,
    retries: int,
    encrypt: bool,
) -> None:
    out_dir = Path(output) if output else in_path / "output"
    logger.info("Folder mode — input: {} | output: {}", in_path, out_dir)

    processed = 0
    skipped = 0
    failed = 0
    for path in sorted(in_path.rglob("*")):
        if not _is_candidate_file(path):
            continue
        src_rel = path.relative_to(in_path)
        if src_rel.suffix.lower() in (".yml", ".yaml"):
            rel = src_rel.with_suffix(".yml")
        else:
            # Avoid collision when a same-named .yml and .json (etc.) sit in the
            # same folder — embed the original extension in the output stem.
            rel = src_rel.with_name(src_rel.stem + "_" + src_rel.suffix.lstrip(".") + ".yml")
        out_file = _resolved_output_path(out_dir, rel)
        if out_file is None:
            skipped += 1
            continue
        try:
            if _convert_one(path, out_file, deployment, retries, encrypt):
                processed += 1
            else:
                skipped += 1
        except HarnessYamlValidationError:
            failed += 1

    logger.info(
        "Folder conversion complete — {} processed, {} skipped, {} failed (validation)",
        processed,
        skipped,
        failed,
    )
    if failed:
        sys.stderr.write(
            f"Error: {failed} file(s) failed LLM validation; no Harness YAML was written for those inputs.\n"
        )
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert any CI/CD pipeline (Azure, Jenkins, GitHub Actions, …) to Harness YAML."
    )
    parser.add_argument("input", nargs="?",
                        help="Source pipeline file, a folder of pipeline files, "
                             "or '-' to read raw pipeline text from stdin")
    parser.add_argument("--text", metavar="PIPELINE_TEXT",
                        help="Raw pipeline text to convert directly (skips file I/O). "
                             "Use \\n for newlines when passing inline on the shell. "
                             "Output defaults to out_<timestamp>.yml in the current directory.")
    parser.add_argument("-o", "--output",
                        help="Output file (when input is a file) or output folder (when input is a folder). "
                             "Defaults: <inputdir>/<stem>_output.yml for a file, "
                             "or <inputfolder>/output/ for a folder.")
    parser.add_argument("--retries", default=3, type=int,
                        help="Max retry attempts (default: 3)")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="Disable value encryption (send pipeline content as-is to the LLM)")
    parser.add_argument("--steps", action="store_true",
                        help="Steps mode: input is a list of JSON step objects (NDJSON). "
                             "Each step is converted individually and the results are printed "
                             "as a JSON array to stdout.")
    parser.add_argument("--stdout", action="store_true",
                        help="Print converted YAML to stdout instead of writing a file "
                             "(applies to file and text modes).")
    args = parser.parse_args()

    if not args.input and not args.text:
        sys.stderr.write(_USAGE_HINT)
        sys.exit(2)

    load_dotenv()
    _check_llm_credentials()

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", DEFAULT_AZURE_OPENAI_DEPLOYMENT)
    encrypt = not args.no_encrypt

    # ── Resolve raw text from --text or stdin ─────────────────────────────────
    raw_text: str | None = None
    if args.text:
        raw_text = args.text.replace("\\n", "\n").replace("\\t", "\t").strip()
    elif args.input == "-":
        logger.info("Reading from stdin …")
        raw_text = sys.stdin.read().strip()
        if not raw_text:
            sys.stderr.write("Error: stdin was empty — nothing to convert.\n")
            sys.exit(2)

    # ── Steps mode ────────────────────────────────────────────────────────────
    if args.steps:
        if raw_text is not None:
            _run_steps_mode(raw_text, args.retries, encrypt)
        elif args.input:
            in_path = Path(args.input)
            if not in_path.exists():
                parser.error(f"Input path does not exist: {in_path}")
            _run_steps_mode(in_path.read_text(encoding="utf-8"), args.retries, encrypt)
        else:
            sys.stderr.write("Error: --steps requires --text, '-' (stdin), or a file path.\n")
            sys.exit(2)
        return

    # ── Pipeline conversion modes ─────────────────────────────────────────────
    if raw_text is not None:
        _run_text_mode(raw_text, args.output, deployment, args.retries, encrypt,
                       stdout=args.stdout)
        return

    in_path = Path(args.input)
    if not in_path.exists():
        parser.error(f"Input path does not exist: {in_path}")

    if in_path.is_file():
        _run_file_mode(in_path, args.output, deployment, args.retries, encrypt,
                       stdout=args.stdout)
        return

    _run_folder_mode(in_path, args.output, deployment, args.retries, encrypt)


if __name__ == "__main__":
    main()
