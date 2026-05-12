"""Errors surfaced when conversion cannot produce acceptable Harness YAML."""


class HarnessYamlValidationError(RuntimeError):
    """Raised when the LLM response never passes ``pipeline.stages`` validation."""

    def __init__(self, max_retries: int, attempt_reasons: list[str] | None = None) -> None:
        self.max_retries = max_retries
        self.attempt_reasons = list(attempt_reasons or [])
        lines = [
            "The LLM did not produce valid Harness YAML "
            f"(expected pipeline.stages structure) after {max_retries} attempt(s).",
            "Per-attempt validation reasons:",
        ]
        if self.attempt_reasons:
            for i, reason in enumerate(self.attempt_reasons, start=1):
                lines.append(f"  {i}. {reason}")
        else:
            lines.append("  (no diagnostic captured)")
        super().__init__("\n".join(lines))
