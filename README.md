# Harness Link

Convert any CI/CD pipeline file (Azure DevOps, Jenkins, GitHub Actions, GitLab CI, CircleCI, Bitbucket, Drone, Travis, …) into a **Harness YAML** pipeline using an LLM, with sensitive values masked before they ever leave your machine.

---

## Features

- **LLM-driven format detection** — the source CI/CD tool is identified automatically by the LLM itself; no keyword patterns to maintain.
- **Broad input support** — accepts `.yml`, `.yaml`, `.groovy`, `.json`, and `Jenkinsfile` in all modes.
- **Single-file, folder, text, and steps modes** — convert one file, batch-convert a whole folder, pass raw text inline, or convert individual pipeline step objects.
- **Smart skipping** — JSON files without pipeline-related keywords are silently skipped; everything else is attempted and validated.
- **Collision-safe output naming** — when a folder contains both `foo.yml` and `foo.json`, the JSON output is written as `foo_json.yml` so files are never overwritten.
- **Value masking** — proprietary URLs, image paths, secret/token fields, and variable references are encrypted to short placeholder tokens (`[[SECURE_0]]`, …) before being sent to the LLM, then restored in the final output.
- **Validation + retries** — full-pipeline output is checked for `pipeline.stages` shape; step output is checked for a `step.spec` shape. Failed attempts are retried up to `--retries` times.
- **Structured logging** via [loguru](https://github.com/Delgan/loguru).
- **REST API** — optional FastAPI service (`api_main.py`) with the same conversion behaviour as the CLI; see [HTTP API](#http-api-optional) below.

---

## Requirements

- Python **3.10+**
- An LLM backend — currently **Google Gemini** (`gemini-2.0-flash`) for testing; Azure OpenAI is the production target (commented out, ready to re-enable).

Python packages (see `requirements.txt`):

```text
langchain-google-genai
langchain-openai
langchain-core
pyyaml
cryptography
loguru
python-dotenv
fastapi
uvicorn[standard]
python-multipart
```

---

## Setup

```bash
git clone <this-repo>
cd "harness link"

python -m venv .venv
# Windows
.\.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your credentials.

**Gemini (current testing backend):**

```env
GOOGLE_API_KEY=your-google-api-key
```

**Azure OpenAI (production backend — uncomment in `controller.py` when ready):**

```env
AZURE_OPENAI_API_KEY=your-azure-openai-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
OPENAI_API_VERSION=2024-02-01
AZURE_OPENAI_DEPLOYMENT=gpt-5.4
```

---

## Usage

```text
python main.py <input> [-o <output>] [--retries N] [--no-encrypt] [--stdout] [--steps]
```

| Argument        | Description |
| --------------- | ----------- |
| `input`         | Path to a pipeline **file**, a **folder** of pipeline files, or `-` to read from stdin. |
| `-o, --output`  | Output **file** (file mode) or output **folder** (folder mode). See defaults below. |
| `--retries`     | Max LLM retry attempts when validation fails. Default: `3`. |
| `--no-encrypt`  | Disable value masking — sends content as-is to the LLM. Useful for debugging prompts. |
| `--stdout`      | Print converted YAML to stdout instead of writing a file (file and text modes). |
| `--steps`       | **Steps mode** — input is a list of JSON step objects (NDJSON). Each step is converted individually; results are printed as a JSON array to stdout. |
| `--text`        | Raw pipeline text inline (use `\n` for newlines). Output defaults to `out_<timestamp>.yml`. |

### Single-file mode

```bash
# Write to auto-named output file next to input
python main.py SamplePipeline.yml

# Write to a specific file
python main.py SamplePipeline.yml -o output/harness.yml

# Print to stdout (for piping or JS capture)
python main.py SamplePipeline.yml --stdout

# 5 retries, no masking
python main.py Jenkinsfile -o out.yml --retries 5 --no-encrypt
```

### Folder mode

```bash
# ./pipelines/**  →  ./harness-out/**  (mirrors structure)
python main.py ./pipelines -o ./harness-out

# Default output folder: <input>/output/
python main.py ./pipelines
```

In folder mode the tool will:

1. Walk the input folder recursively.
2. Open only files with extensions `.yml`, `.yaml`, `.groovy`, `.json` or named `Jenkinsfile`.
3. JSON files without pipeline-related keywords (e.g. plain config files) are skipped; all other files are sent to the LLM.
4. Output is written to `<output>/<relative/path>.yml`. If both `foo.yml` and `foo.json` exist in the same folder they are written as `foo.yml` and `foo_json.yml` respectively.
5. A summary is logged at the end: `processed N, skipped M, failed K`.

### Text mode (inline)

```bash
# Pass pipeline text directly — output defaults to out_<timestamp>.yml
python main.py --text "trigger:\n  branches:\n    include:\n      - main\njobs:\n..."

# Pipe from stdin
cat SamplePipeline.yml | python main.py -

# Pipe from stdin and print to stdout
cat SamplePipeline.yml | python main.py - --stdout
```

### Steps mode

Steps mode converts a list of individual pipeline step objects (one per conversion call) and prints a JSON array to stdout. This is designed for programmatic use from JavaScript or other callers.

**Input format** — newline-delimited JSON (NDJSON): multiple bare JSON objects written one after another, or a proper `[{...}, ...]` JSON array. Each object must have an `originalStep` field.

```bash
# From a file
python main.py steps.ndjson --steps

# From stdin
cat steps.ndjson | python main.py - --steps

# Inline text
python main.py --text "{...}{...}" --steps
```

**Output** (always to stdout):

```json
[
  {
    "index": 0,
    "displayName": "Activate-service-account",
    "taskName": "GcloudRunner@0",
    "yaml": "step:\n  type: Run\n  name: Activate_service_account\n  ..."
  },
  {
    "index": 1,
    "displayName": "build",
    "taskName": "Docker@0",
    "yaml": "step:\n  type: Run\n  name: build\n  ..."
  },
  {
    "index": 7,
    "displayName": "Delete Cloud Scheduler",
    "taskName": "GcloudRunner@0",
    "error": "LLM output never matched step schema after 3 attempts"
  }
]
```

Steps that fail validation return an `"error"` key instead of `"yaml"` — one bad step never aborts the whole batch.

**JavaScript usage:**

```javascript
const { spawn } = require('child_process');

function convertSteps(ndjsonText) {
  return new Promise((resolve, reject) => {
    const py = spawn('python', ['main.py', '--text', ndjsonText, '--steps']);
    let out = '';
    let err = '';
    py.stdout.on('data', c => out += c);
    py.stderr.on('data', c => err += c);
    py.on('close', code => {
      if (code === 0) resolve(JSON.parse(out));   // array of step results
      else reject(new Error(err));
    });
  });
}

// Or capture a full pipeline YAML string
function convertPipeline(yamlText) {
  return new Promise((resolve, reject) => {
    const py = spawn('python', ['main.py', '--text', yamlText, '--stdout']);
    let out = '';
    py.stdout.on('data', c => out += c);
    py.on('close', code => code === 0 ? resolve(out) : reject());
  });
}
```

---

## HTTP API (optional)

Same conversion logic as the CLI, exposed over REST.

```bash
python api_main.py
# or
uvicorn api.app:app --host 0.0.0.0 --port 8000
```

- Default bind: `http://0.0.0.0:8000` — override with `API_HOST`, `API_PORT`; set `API_RELOAD=true` for auto-reload.
- Open **`GET /docs`** for interactive Swagger UI.

| Method | Path | Description |
| ------ | ---- | ----------- |
| `GET`  | `/health` | Liveness check; reports whether required env vars are set. |
| `POST` | `/v1/convert` | JSON body: `pipeline` (string), optional `retries` (1–10), optional `encrypt` (default `true`). |
| `POST` | `/v1/convert/file` | Multipart form upload (`file` field); optional query params `retries`, `encrypt`. |

```bash
curl -s -X POST "http://127.0.0.1:8000/v1/convert" \
  -H "Content-Type: application/json" \
  -d "{\"pipeline\": \"$(cat SamplePipeline.yml)\", \"retries\": 3}"
```

---

## How it works

```
┌──────────────┐  is_pipeline_content?  ┌──────────────────────────────────┐
│  raw input   │───────────────────────▶│  PipelineEncryptor               │
│  (any format)│  skip if JSON with     │  masks secrets → [[SECURE_N]]    │
└──────────────┘  no pipeline keywords  └──────────────────┬───────────────┘
                                                            │
                                                            ▼ LLM (Gemini / Azure OpenAI)
                                                 "Identify format, then convert"
                                                            │
┌──────────────┐   decrypt tokens   ┌───────────────────────┴───────────────┐
│ Harness YAML │◀───────────────────│  validate pipeline.stages             │
│  (final)     │                    │  or step.spec shape  →  retry if fail │
└──────────────┘                    └───────────────────────────────────────┘
```

Key modules in `converter/`:

| File               | Responsibility |
| ------------------ | -------------- |
| `detector.py`      | Pipeline content gatekeeper — decides whether a file is worth sending to the LLM (not format detection; the LLM handles that). |
| `encryptor.py`     | `PipelineEncryptor` — masks sensitive values with Fernet-encrypted tokens before the LLM call and restores them afterwards. Covers YAML fields, JSON fields, URLs, image paths, and variable references. |
| `prompt.py`        | Assembles the full-pipeline system prompt (static rules + dynamically loaded few-shot examples) and the single-step system prompt + user templates. |
| `examples.py`      | `add_example()` utility + `build_examples_prompt()` — manages the examples library. |
| `controller.py`    | `convert()` — orchestrates mask → LLM → validate → retry → unmask for full pipelines. `convert_step()` — same flow using the step prompt and step validator. |
| `validator.py`     | Extracts YAML from fenced LLM responses; `validate_harness_yaml()` checks `pipeline.stages` shape; `validate_harness_step()` checks `step.spec` shape. |
| `azure_url.py`     | Builds the Azure OpenAI chat-completions URL (handy for proxy / firewall checks). |
| `logging_setup.py` | Loguru configuration. |

---

## Adding few-shot examples

The LLM's output quality improves with concrete before/after examples. Examples live in `converter/examples/` — one subfolder per example, loaded automatically at startup.

```python
from converter.examples import add_example

add_example(
    pipeline_type="CI",                   # "CI" or "CD"
    input_path="raw_pipeline.yml",        # source pipeline — sanitized automatically
    output_path="expected_harness.yml",   # correct Harness YAML output
    title="Jenkins Maven Build",          # label shown in the prompt (optional)
)
```

Restart the app and the new example is included in every subsequent LLM call.

### What `add_example` does automatically

| Pattern | Replaced with |
| ------- | ------------- |
| UUIDs | `<uuid-1>`, `<uuid-2>`, … |
| Full URLs | `<url-1>`, `<url-2>`, … |
| Values under sensitive YAML/JSON keys (`password`, `secret`, `token`, `apiKey`, `endpoint`, …) | `<masked>` |

### Folder structure

```
converter/examples/
├── 01_CI_azure_devops_gcp/       ← numbered prefix controls prompt order
│   ├── input.yml
│   ├── output.yml
│   └── meta.json                 ← {"type": "CI", "title": "..."}
├── 02_CI_mulesoft/
│   ├── input.yml
│   ├── output.yml
│   └── meta.json
└── 03_CD_mulesoft/
    ├── input.json                ← .json extension works too
    ├── output.yml
    └── meta.json
```

**Rules:** prefix with `01_`, `02_`, … to control order; input file must be `input.<ext>`; output file must be `output.yml`; `meta.json` is optional.

### Pre-loaded examples

| Folder | Type | Pattern |
| ------ | ---- | ------- |
| `01_CI_azure_devops_gcp` | CI | Azure DevOps YAML → GCP Cloud Run deploy |
| `02_CI_mulesoft` | CI | Azure DevOps YAML → MuleSoft Maven build |
| `03_CD_mulesoft` | CD | Azure DevOps Classic Release JSON → MuleSoft CloudHub deploy |

---

## Value masking (default ON)

Before the LLM sees your pipeline, `PipelineEncryptor` replaces sensitive values with placeholder tokens such as `[[SECURE_0]]`. Three passes are applied:

1. **Regex patterns** — GitHub Actions `${{ secrets.X }}`, Azure DevOps `$(varName)`, shell `${VAR}`, full URLs, container image paths.
2. **YAML field heuristics** — values under keys named `password`, `token`, `apiKey`, `secret`, `connectionString`, `*Endpoint`, etc.
3. **JSON field heuristics** — same sensitive key list, matching `"key": "value"` JSON syntax (for JSON pipeline definitions).

Each run uses a fresh in-memory Fernet key; tokens are only valid for the lifetime of that conversion. Disable with `--no-encrypt` to debug the raw prompt the LLM sees.

---

## Project layout

```
harness link/
├── main.py                  # CLI entry point
├── api_main.py              # HTTP API entry (uvicorn)
├── requirements.txt
├── .env.example
├── SamplePipeline.yml       # example pipeline for quick testing
├── api/
│   └── app.py               # FastAPI application
├── converter/
│   ├── __init__.py          # exports convert, convert_step, HarnessYamlValidationError
│   ├── constants.py
│   ├── azure_credentials.py
│   ├── controller.py        # convert() + convert_step()
│   ├── detector.py          # pipeline content gatekeeper
│   ├── encryptor.py         # PipelineEncryptor (YAML + JSON field masking)
│   ├── examples.py
│   ├── prompt.py            # SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, STEP_SYSTEM_PROMPT, STEP_USER_PROMPT_TEMPLATE
│   ├── validator.py         # validate_harness_yaml(), validate_harness_step()
│   ├── azure_url.py
│   ├── logging_setup.py
│   └── examples/
│       ├── 01_CI_azure_devops_gcp/
│       ├── 02_CI_mulesoft/
│       └── 03_CD_mulesoft/
└── tests/
    ├── test_api_health.py
    ├── test_detector_validator.py
    ├── test_encryption.py
    └── test_full_flow.py
```

---

## Testing

```bash
pip install pytest
pytest -v
```

`test_detector_validator.py` runs quick offline checks (gatekeeper + YAML helpers). `test_encryption.py` exercises the masking / unmasking round-trip. `test_full_flow.py` runs the full LLM flow when credentials are set (`python tests/test_full_flow.py`).

---

## Troubleshooting

- **`Input path does not exist`** — double-check the path; on Windows quote paths containing spaces.
- **`Could not detect a CI/CD pipeline`** — the JSON file contains no pipeline-related keywords. Ensure it has fields like `stages`, `steps`, `jobs`, `environments`, `deployPhases`, etc.
- **Validation keeps failing / retries exhausted** — increase `--retries`, or use `--no-encrypt` to inspect the raw content the LLM receives.
- **Gemini auth error** — ensure `GOOGLE_API_KEY` is set in `.env` or the environment.
- **Azure auth errors** — verify `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `OPENAI_API_VERSION`, and `AZURE_OPENAI_DEPLOYMENT` are set. The exact chat-completions URL is logged at startup.

---

## License

Internal POC — add a license here before distributing externally.
