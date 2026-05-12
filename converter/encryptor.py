"""
Encrypt sensitive values in pipeline content before sending to the LLM,
then decrypt placeholder tokens in LLM output to restore real values.

This protects proprietary pipeline content — project names, URLs, image
paths, credentials, variable references — from being transmitted as plain
text to the LLM API.

Flow:
    original content
        → encrypt_content()  →  masked content (tokens replace values)
        → LLM call           →  Harness YAML with same tokens preserved
        → decrypt_content()  →  final Harness YAML with real values
"""

import re

from cryptography.fernet import Fernet
from loguru import logger


# ── Token format ──────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r'\[\[SECURE_(\d+)\]\]')
_TOKEN_FMT = "[[SECURE_{index}]]"


# ── Sensitive value detection patterns ────────────────────────────────────────

# Regex patterns applied to the full content (matched text gets encrypted).
# Order matters: most specific first.
_VALUE_PATTERNS: list[re.Pattern] = [
    # GitHub Actions  ${{ secrets.VAR }}  /  ${{ env.VAR }}
    re.compile(r'\$\{\{\s*(?:secrets|env|vars)\.[A-Za-z][A-Za-z0-9_]*\s*\}\}'),
    # Azure DevOps    $(variableName)   /   $(task.output)
    re.compile(r'\$\([A-Za-z_][A-Za-z0-9_.\-]*\)'),
    # Shell / Bash    ${VAR_NAME}
    re.compile(r'\$\{[A-Za-z_][A-Za-z0-9_]*\}'),
    # Full URLs
    re.compile(r'https?://[^\s\'">,\]]+'),
    # Docker / OCI registry image paths  (registry.domain/org/repo:tag)
    re.compile(r'[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9\-]+)+'
               r'/[a-z0-9][a-z0-9\-_./]*(?::[a-z0-9.\-_]+)?'),
]

# YAML field names whose VALUES are always encrypted regardless of content.
# Matched case-insensitively after stripping hyphens/underscores.
_SENSITIVE_FIELDS: frozenset[str] = frozenset({
    "password", "passwd", "secret", "token",
    "apikey", "apitoken", "authtoken",
    "credential", "credentials",
    "privatekey", "accesskey", "secretkey",
    "securefile", "securefilepath",
    "connectionstring",
    "dockerregistryendpoint", "registryendpoint", "containerregistryendpoint",
    "endpoint",                # covers many Azure DevOps connector fields
})

# Matches a YAML key: value line (single-line scalar only).
# Uses [ \t]* (not \s*) around the colon so the pattern never crosses newlines.
_YAML_FIELD_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?P<key>[A-Za-z][A-Za-z0-9_\-]*)(?P<sep>[ \t]*:[ \t]*)(?P<value>.+)$',
    re.MULTILINE,
)

# Matches a JSON string field: "key": "value"  (single-line, non-empty value).
_JSON_FIELD_RE = re.compile(
    r'"(?P<key>[A-Za-z][A-Za-z0-9_\-]*)"[ \t]*:[ \t]*"(?P<value>[^"\\]+)"',
)


def _normalise_key(key: str) -> str:
    """Lower-case, strip hyphens/underscores for field-name matching."""
    return key.lower().replace("-", "").replace("_", "")


# ── Encryptor class ───────────────────────────────────────────────────────────

class PipelineEncryptor:
    """
    Encrypts sensitive pipeline values before LLM processing and decrypts
    the tokens it places back in the LLM's output.

    Each instance generates a fresh Fernet key, so tokens are only valid
    for the lifetime of a single conversion run.
    """

    def __init__(self) -> None:
        self._key = Fernet.generate_key()
        self._fernet = Fernet(self._key)
        # index → Fernet-encrypted original bytes
        self._vault: dict[int, bytes] = {}
        self._counter = 0
        # reverse look-up: original value → existing token  (de-dup)
        self._seen: dict[str, str] = {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _store(self, value: str) -> str:
        """Encrypt *value*, store in vault, return its placeholder token."""
        if value in self._seen:
            return self._seen[value]
        encrypted = self._fernet.encrypt(value.encode("utf-8"))
        idx = self._counter
        self._vault[idx] = encrypted
        self._counter += 1
        token = _TOKEN_FMT.format(index=idx)
        self._seen[value] = token
        return token

    def _restore(self, idx: int) -> str:
        """Decrypt and return the original value for token index *idx*."""
        encrypted = self._vault.get(idx)
        if encrypted is None:
            # Unknown token — leave it untouched
            return _TOKEN_FMT.format(index=idx)
        return self._fernet.decrypt(encrypted).decode("utf-8")

    # ── Public API ────────────────────────────────────────────────────────────

    def encrypt_content(self, content: str) -> str:
        """
        Scan *content* for sensitive values, replace each with an encrypted
        placeholder token, and return the masked pipeline text.

        Three passes are performed:
        1. Regex patterns (variable refs, URLs, image paths).
        2. YAML field-name heuristics (password, secret, token, …).
        3. JSON field-name heuristics — same sensitive field list, quoted JSON syntax.
        """
        result = self._apply_value_patterns(content)
        result = self._apply_field_heuristics(result)
        result = self._apply_json_field_heuristics(result)
        logger.debug(
            "encrypt_content finished with {} unique masked value(s)",
            self.token_count,
        )
        return result

    def decrypt_content(self, content: str) -> str:
        """
        Replace every ``[[SECURE_N]]`` token in *content* with the original
        (decrypted) value and return the restored text.
        """
        out = _TOKEN_RE.sub(lambda m: self._restore(int(m.group(1))), content)
        logger.debug("decrypt_content applied token substitution")
        return out

    @property
    def token_count(self) -> int:
        """Number of unique values encrypted so far."""
        return self._counter

    # ── Private passes ────────────────────────────────────────────────────────

    def _apply_value_patterns(self, content: str) -> str:
        """Pass 1: replace values matching known sensitive regex patterns."""
        for pattern in _VALUE_PATTERNS:
            content = pattern.sub(lambda m: self._store(m.group(0)), content)
        return content

    def _apply_field_heuristics(self, content: str) -> str:
        """Pass 2: encrypt values under YAML keys with sensitive-sounding names."""

        def replace_field(m: re.Match) -> str:
            key_norm = _normalise_key(m.group("key"))
            value = m.group("value").strip()
            # Skip if already a token, a boolean/null literal, or a YAML anchor
            if (
                key_norm not in _SENSITIVE_FIELDS
                or not value
                or _TOKEN_RE.match(value)
                or value in ("true", "false", "null", "~")
                or value.startswith("*")   # YAML alias
                or value.startswith("&")   # YAML anchor
            ):
                return m.group(0)
            # Strip surrounding quotes for the stored value; re-add after
            inner, quote = _strip_quotes(value)
            token = self._store(inner)
            restored_token = f'{quote}{token}{quote}' if quote else token
            return (
                m.group("indent")
                + m.group("key")
                + m.group("sep")
                + restored_token
            )

        return _YAML_FIELD_RE.sub(replace_field, content)

    def _apply_json_field_heuristics(self, content: str) -> str:
        """Pass 3: encrypt values under JSON keys with sensitive-sounding names."""

        def replace_json_field(m: re.Match) -> str:
            key_norm = _normalise_key(m.group("key"))
            value = m.group("value")
            if (
                key_norm not in _SENSITIVE_FIELDS
                or not value
                or _TOKEN_RE.match(value)
                or value in ("true", "false", "null")
            ):
                return m.group(0)
            token = self._store(value)
            # Reconstruct: "key": "token"
            return m.group(0).replace(f'"{value}"', f'"{token}"', 1)

        return _JSON_FIELD_RE.sub(replace_json_field, content)


# ── Utility ───────────────────────────────────────────────────────────────────

def _strip_quotes(value: str) -> tuple[str, str]:
    """Return (inner_value, quote_char) for a quoted YAML scalar, or (value, '')."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1], value[0]
    return value, ""
