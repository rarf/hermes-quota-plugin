"""Private native credential resolution and safe read-only API primitives.

Secrets stay inside this module/fetchers, never in labels or exception text.
Resolve the native API key without selecting or modifying a credential pool.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
import urllib.error
import urllib.request

from .base import urlopen_no_redirect


def resolve_api_key(provider: str):
    """Use Hermes' provider-specific dotenv/key_env precedence, not model routing."""
    from hermes_cli.auth import resolve_api_key_provider_credentials

    resolved = resolve_api_key_provider_credentials(provider)
    secret = resolved.get("api_key")
    return secret.strip() if isinstance(secret, str) and secret.strip() else None


# Never forward a bearer credential to another endpoint (see base.NoRedirectHandler).
urlopen = urlopen_no_redirect


def get_json(url: str, secret: str, timeout: float | None = None):
    """Return (payload, safe reason); never expose response/error text.

    *timeout* defaults to 7 s when omitted, which preserves backward
    compatibility for all existing callers.  Pass ``deadline.slice(cap)``
    when budget enforcement is needed.
    """
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"})
        with urlopen(req, timeout=timeout if timeout is not None else 7) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return None, "response-too-large"
        try:
            return json.loads(body), None
        except (ValueError, UnicodeError):
            return None, "bad-json"
    except urllib.error.HTTPError as exc:
        code = exc.code
        if exc.fp is not None:
            exc.close()
        return None, "auth-failed" if code == 401 else f"http-{code}"
    except (TimeoutError,):
        return None, "timeout"
    except Exception:
        return None, "fetch-error"


def amount(value):
    """Finite bounded decimal, preserving balance precision without coercing bool."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value)
    if len(text) > 64:
        return None
    try:
        number = Decimal(text)
        if (
            not number.is_finite()
            or abs(int(number.as_tuple().exponent)) > 64
            or number.copy_abs() > Decimal('1e15')
            or (number and number.adjusted() < -15)
        ):
            return None
        return number
    except (InvalidOperation, ValueError):
        return None
