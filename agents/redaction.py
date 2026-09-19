"""Secret, header and URL redaction shared by the model transport and the Harness.

Evidence written to task events, checkpoints, the Repository, logs and test
output must never contain credentials. This module is deliberately
dependency-free so both `agents.llm` and `agents.harness.*` can import it
without creating a cycle.

Rules
-----
* Known secret values (the API key actually used for a request) are replaced
  verbatim, so a key that matches no pattern is still removed.
* ``Authorization``/``api_key``/``Bearer``/``token`` assignments are replaced.
* Opaque provider key shapes (``sk-...``) and JWT-shaped tokens are replaced.
* URLs keep only the host; path, query string and userinfo are removed, which
  matches the documented evidence rule ("record the base URL host, not the
  full sensitive URL").
"""
from __future__ import annotations

import re

REDACTED = "<redacted>"

# scheme://host/path?query  ->  scheme://host/<redacted>
_URL = re.compile(r"\bhttps?://([^\s/'\"<>?#]+)[^\s'\"<>]*")
# authorization=... / "api_key": "..." / x-api-key: ...
_AUTH = re.compile(
    r"(?i)(authorization|api[-_]?key|apikey|access[-_]?token|x-api-key|token)"
    r"['\"]?\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
)
# Authorization: Bearer <opaque>
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{4,}")
# OpenAI-style opaque keys
_OPAQUE_KEY = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{6,}\b")
# JWT-shaped tokens (MiniMax keys are commonly JWTs)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{2,}\b")

# A secret shorter than this is not treated as a replaceable literal: replacing
# very short strings would corrupt unrelated text (e.g. a 2-char key).
_MIN_SECRET_LENGTH = 8


def sanitize(value, secrets=()) -> str:
    """Return a redacted copy of ``value`` safe for events, logs and commits."""
    text = value if isinstance(value, str) else str(value)
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= _MIN_SECRET_LENGTH and secret in text:
            text = text.replace(secret, REDACTED)
    text = _AUTH.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    text = _OPAQUE_KEY.sub(REDACTED, text)
    text = _URL.sub(lambda match: f"https://{match.group(1)}/<redacted>", text)
    return text


def sanitize_exception(exc, secrets=()) -> str:
    """``TypeName: sanitized message`` — never the raw exception text."""
    return sanitize(f"{type(exc).__name__}: {exc}", secrets=secrets)
