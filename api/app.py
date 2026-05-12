"""FastAPI application — converts CI/CD pipeline text to Harness YAML via the same core as the CLI."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from loguru import logger
from pydantic import BaseModel, Field

from converter import HarnessYamlValidationError, convert
from converter.azure_credentials import (
    azure_credentials_help_text,
    missing_azure_openai_env_vars,
)
from converter.constants import DEFAULT_AZURE_OPENAI_DEPLOYMENT
from converter.detector import UNKNOWN_PIPELINE_FORMAT_LABEL, detect_format
from converter.validator import is_valid


def _deployment_name() -> str:
    return os.environ.get("AZURE_OPENAI_DEPLOYMENT", DEFAULT_AZURE_OPENAI_DEPLOYMENT)


def _require_llm_env() -> None:
    missing = missing_azure_openai_env_vars()
    if missing:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Azure OpenAI is not configured",
                "missing_environment_variables": missing,
                "hint": azure_credentials_help_text(),
            },
        )


class ConvertRequest(BaseModel):
    """JSON body for POST /v1/convert."""

    pipeline: str = Field(..., min_length=1, description="Raw pipeline file contents (YAML, Groovy, etc.)")
    retries: int = Field(default=3, ge=1, le=10, description="Max LLM validation retries")
    encrypt: bool = Field(default=True, description="Mask sensitive values before the LLM call")


class ConvertResponse(BaseModel):
    harness_yaml: str
    detected_format: str
    harness_yaml_valid: bool


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_dotenv()
    missing = missing_azure_openai_env_vars()
    if missing:
        logger.warning(
            "API started without full Azure OpenAI configuration; missing: {}",
            ", ".join(missing),
        )
    else:
        logger.info("Azure OpenAI environment variables present; POST /v1/convert is ready")
    yield


app = FastAPI(
    title="Harness Link API",
    description=(
        "Convert CI/CD pipelines (Azure DevOps, Jenkins, GitHub Actions, …) "
        "to Harness YAML using the same logic as ``python main.py``."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    """Liveness; does not call the LLM."""
    missing = missing_azure_openai_env_vars()
    return {
        "status": "ok",
        "llm_configured": len(missing) == 0,
        "missing_environment_variables": missing,
    }


@app.post("/v1/convert", response_model=ConvertResponse)
async def convert_pipeline_json(body: ConvertRequest) -> ConvertResponse:
    """Convert pipeline text from JSON body."""
    _require_llm_env()
    return await _convert_pipeline(
        body.pipeline.strip(),
        body.retries,
        body.encrypt,
    )


@app.post("/v1/convert/file", response_model=ConvertResponse)
async def convert_pipeline_upload(
    file: Annotated[UploadFile, File(description="Pipeline file (.yml, .yaml, Jenkinsfile, .groovy)")],
    retries: int = 3,
    encrypt: bool = True,
) -> ConvertResponse:
    """Convert an uploaded pipeline file (same behaviour as a single-file CLI run)."""
    _require_llm_env()
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail=f"File must be UTF-8 text: {e}") from e

    if retries < 1 or retries > 10:
        raise HTTPException(status_code=422, detail="retries must be between 1 and 10")

    return await _convert_pipeline(text.strip(), retries, encrypt)


async def _convert_pipeline(content: str, retries: int, encrypt: bool) -> ConvertResponse:
    if not content:
        raise HTTPException(status_code=422, detail="pipeline content is empty")

    detected = detect_format(content)
    if detected == UNKNOWN_PIPELINE_FORMAT_LABEL:
        raise HTTPException(
            status_code=422,
            detail=(
                "Pipeline format could not be detected as a known CI/CD type. "
                "Ensure the body looks like Azure DevOps, Jenkins, GitHub Actions, GitLab CI, etc."
            ),
        )

    deployment = _deployment_name()
    fn = partial(
        convert,
        content,
        deployment=deployment,
        max_retries=retries,
        encrypt=encrypt,
    )
    try:
        harness_yaml = await asyncio.to_thread(fn)
    except HarnessYamlValidationError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Harness YAML validation failed",
                "message": str(exc),
            },
        ) from exc

    return ConvertResponse(
        harness_yaml=harness_yaml,
        detected_format=detected,
        harness_yaml_valid=is_valid(harness_yaml),
    )
