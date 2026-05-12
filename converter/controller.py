"""Orchestrates format detection → LLM call → validation with retries."""

import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import AzureChatOpenAI
from loguru import logger

from .azure_url import azure_chat_completions_url
from .constants import DEFAULT_AZURE_OPENAI_DEPLOYMENT
from .encryptor import PipelineEncryptor
from .exceptions import HarnessYamlValidationError
from .prompt import STEP_SYSTEM_PROMPT, STEP_USER_PROMPT_TEMPLATE, SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from .validator import extract_yaml, validate_harness_step, validate_harness_yaml


def _invoke_llm_with_validation(
    llm: BaseChatModel,
    llm_label: str,
    user_message: str,
    fallback_yaml: str,
    max_retries: int,
) -> str:
    """Call the LLM up to *max_retries* times until output parses as valid Harness YAML."""
    result = fallback_yaml
    failure_reasons: list[str] = []

    for attempt in range(1, max_retries + 1):
        logger.info(
            "LLM attempt {}/{} — {}",
            attempt,
            max_retries,
            llm_label,
        )

        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ])

        result = extract_yaml(response.content)
        ok, reason = validate_harness_yaml(result)

        if ok:
            logger.info("LLM output validated as Harness YAML")
            return result

        failure_reasons.append(reason)
        preview = result.strip().replace("\n", "\\n")
        if len(preview) > 320:
            preview = preview[:320] + "…"
        logger.warning(
            "Attempt {}/{} rejected — {} — output preview: {!r}",
            attempt,
            max_retries,
            reason,
            preview or "(empty)",
        )

    logger.error(
        "Giving up after {} attempt(s): output never matched Harness YAML schema. Reasons logged above.",
        max_retries,
    )
    raise HarnessYamlValidationError(max_retries, failure_reasons)


def _invoke_step_llm_with_validation(
    llm: BaseChatModel,
    llm_label: str,
    user_message: str,
    fallback_yaml: str,
    max_retries: int,
) -> str:
    """Call the LLM up to *max_retries* times until output parses as valid Harness step YAML."""
    result = fallback_yaml
    failure_reasons: list[str] = []

    for attempt in range(1, max_retries + 1):
        logger.info(
            "Step LLM attempt {}/{} — {}",
            attempt,
            max_retries,
            llm_label,
        )

        response = llm.invoke([
            SystemMessage(content=STEP_SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ])

        result = extract_yaml(response.content)
        ok, reason = validate_harness_step(result)

        if ok:
            logger.info("Step LLM output validated as Harness step YAML")
            return result

        failure_reasons.append(reason)
        preview = result.strip().replace("\n", "\\n")
        if len(preview) > 320:
            preview = preview[:320] + "…"
        logger.warning(
            "Step attempt {}/{} rejected — {} — output preview: {!r}",
            attempt,
            max_retries,
            reason,
            preview or "(empty)",
        )

    logger.error(
        "Giving up after {} attempt(s): step output never matched Harness step schema.",
        max_retries,
    )
    raise HarnessYamlValidationError(max_retries, failure_reasons)


def convert(
    content: str,
    deployment: str = DEFAULT_AZURE_OPENAI_DEPLOYMENT,
    max_retries: int = 3,
    encrypt: bool = True,
) -> str:
    """Convert *content* to Harness YAML via Azure OpenAI."""
    encryptor = PipelineEncryptor() if encrypt else None
    if encryptor:
        masked_content = encryptor.encrypt_content(content)
        logger.info(
            "Masked {} sensitive value(s) with placeholder tokens before LLM call",
            encryptor.token_count,
        )
    else:
        masked_content = content
        logger.info("Value masking disabled; sending pipeline content as-is to the LLM")

    logger.info("Pipeline format will be auto-detected by the LLM")
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_version = os.environ.get("OPENAI_API_VERSION", "")
    chat_url = azure_chat_completions_url(endpoint, deployment, api_version)
    logger.info("Azure OpenAI chat completions URL (for firewall / proxy checks): {}", chat_url)

    llm = AzureChatOpenAI(azure_deployment=deployment, temperature=0)
    user_message = USER_PROMPT_TEMPLATE.format(pipeline=masked_content)

    result = _invoke_llm_with_validation(
        llm, deployment, user_message, masked_content, max_retries,
    )

    if encryptor:
        result = encryptor.decrypt_content(result)
        logger.info("Restored sensitive values from placeholder tokens in LLM output")

    return result


def convert_step(
    step_json: str,
    max_retries: int = 3,
    encrypt: bool = True,
    deployment: str = DEFAULT_AZURE_OPENAI_DEPLOYMENT,
) -> str:
    """Convert a single step JSON object to a Harness step YAML snippet.

    *step_json* is the raw JSON string of one step object from the steps-mode
    input (fields: index, displayName, taskName, condition, enabled, originalStep).
    Returns a YAML string with a top-level ``step:`` key.
    """
    encryptor = PipelineEncryptor() if encrypt else None
    if encryptor:
        masked = encryptor.encrypt_content(step_json)
    else:
        masked = step_json

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    api_version = os.environ.get("OPENAI_API_VERSION", "")
    chat_url = azure_chat_completions_url(endpoint, deployment, api_version)
    logger.info("Azure OpenAI chat completions URL (step mode, firewall / proxy): {}", chat_url)

    llm = AzureChatOpenAI(azure_deployment=deployment, temperature=0)
    user_message = STEP_USER_PROMPT_TEMPLATE.format(step=masked)

    result = _invoke_step_llm_with_validation(
        llm, deployment, user_message, masked, max_retries,
    )

    if encryptor:
        result = encryptor.decrypt_content(result)

    return result
