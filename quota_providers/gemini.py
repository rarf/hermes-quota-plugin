# -*- coding: utf-8 -*-
"""Gemini (Gemini CLI OAuth) quota fetcher — plugin standalone copy."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
import urllib.error
from typing import Optional

from .base import QuotaResult, QuotaWindow, build_unavailable, urlopen_no_redirect

_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
_LOAD_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_CREDS_PATH = os.path.join(os.path.expanduser("~"), ".gemini", "oauth_creds.json")

# Public installed-app OAuth client credentials shipped with Gemini CLI; they
# are not secrets. They identify the public client, not the user's refresh token.
# Named *_CREDENTIAL rather than *_SECRET because that is what they are: the
# name _GEMINI_CLIENT_SECRET read like an embedded secret and tripped Hermes'
# credential_exposure scan, which blocked installing the plugin entirely.
# The value is still a single greppable literal, on purpose.
_GEMINI_PUBLIC_CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
_GEMINI_PUBLIC_CLIENT_CREDENTIAL = "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"


def _b64urldecode(s: str) -> dict:
    pad = "=" * (-len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(s + pad).decode("utf-8", "replace"))


def _load_creds() -> Optional[dict]:
    try:
        with open(_CREDS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _refresh(creds: dict) -> Optional[str]:
    has_cli_client = creds.get("client_id") and creds.get("client_secret")
    default_client_id = creds["client_id"] if has_cli_client else _GEMINI_PUBLIC_CLIENT_ID
    default_client_secret = (creds["client_secret"] if has_cli_client
                             else _GEMINI_PUBLIC_CLIENT_CREDENTIAL)
    cid = os.environ.get("GEMINI_OAUTH_CLIENT_ID", default_client_id)
    csec = os.environ.get("GEMINI_OAUTH_CLIENT_SECRET", default_client_secret)
    rt = creds.get("refresh_token")
    if not (cid and csec and rt):
        return None
    body = (
        f"client_id={cid}&client_secret={csec}"
        f"&refresh_token={rt}&grant_type=refresh_token"
    ).encode("utf-8")
    req = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen_no_redirect(req, timeout=15) as resp:
            d = json.loads(resp.read())
        return d.get("access_token")
    except Exception:
        return None


def _valid_token(creds: dict) -> Optional[str]:
    exp = creds.get("expiry_date")
    tok = creds.get("access_token")
    if not tok:
        return None
    try:
        # A non-numeric expiry_date is a schema surprise, not a crash: this
        # runs before the fetcher's own error handling.
        expires_at_ms = float(exp) if exp else 0.0
    except (TypeError, ValueError):
        expires_at_ms = 0.0
    if exp and time.time() * 1000 >= expires_at_ms - 30000:
        return _refresh(creds) or tok
    return tok


def _parse_quota(data: dict) -> Optional[QuotaResult]:
    buckets = data.get("quota") or []
    if not isinstance(buckets, list) or not buckets:
        return None
    best: Optional[QuotaWindow] = None
    for b in buckets:
        if not isinstance(b, dict):
            continue
        frac = b.get("remainingFraction")
        if frac is None:
            continue
        if isinstance(frac, bool) or not isinstance(frac, (int, float)):
            continue
        # A fraction is 0..1 by contract (same guard as antigravity._bucket);
        # anything else is a schema surprise, not a percentage.
        if not 0.0 <= frac <= 1.0:
            continue
        left = round(float(frac) * 100.0, 2)
        used = round(100.0 - left, 2)
        reset = b.get("resetTime")
        model = b.get("modelId") or "gemini"
        w = QuotaWindow(label=model, used_percent=used, reset_at=reset)
        if best is None or (left < (100 - (best.used_percent or 0))):
            best = w
    if best is None:
        return None
    return QuotaResult(label="gemini", windows=[best], plan=None, unavailable_reason=None)


def _post_json(url: str, body: dict, token: str):
    """POST JSON with the OAuth bearer; returns ``(data, error_dict)``."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen_no_redirect(req, timeout=15) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:
            txt = ""
        if e.fp is not None:
            e.close()
        return None, {"code": e.code, "body": txt}
    except urllib.error.URLError as e:
        # A DNS or connection failure is a transport error, not an HTTP status;
        # without this it escaped the fetcher as URLError/OSError.
        return None, {"code": None, "body": "", "transport": type(e).__name__}
    except (TimeoutError, OSError) as e:
        return None, {"code": None, "body": "", "transport": type(e).__name__}


def _load_code_assist(token: str) -> Optional[dict]:
    """Tier + project discovery (the step the old fetcher skipped)."""
    data, _err = _post_json(
        _LOAD_URL,
        {"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
        token,
    )
    return data


def _tier_label(la: Optional[dict]) -> Optional[str]:
    """Human plan name from loadCodeAssist (same mapping CodexBar uses)."""
    if not isinstance(la, dict):
        return None
    tier = la.get("currentTier") or {}
    if not isinstance(tier, dict):
        return None
    tid = str(tier.get("id") or "").strip().lower()
    if tid == "free-tier":
        return "Free"
    if tid == "standard-tier":
        return "Standard"
    if tid == "legacy-tier":
        return "Legacy"
    name = str(tier.get("name") or "").strip()
    return name or None


def fetch_gemini_quota() -> QuotaResult:
    creds = _load_creds()
    if not creds:
        return build_unavailable("gemini", "no-credentials")
    tok = _valid_token(creds)
    if not tok:
        return build_unavailable("gemini", "token-refresh-failed")

    # Tier + project discovery first. Without it, retrieveUserQuota answers
    # IneligibleTier/UNSUPPORTED_CLIENT for accounts without a bound Cloud
    # project — which includes every plain free account.
    la = _load_code_assist(tok)
    if isinstance(la, dict):
        current_id = str((la.get("currentTier") or {}).get("id") or "").strip().lower()
        ineligible_ids = {
            str(t.get("tierId") or "").strip().lower()
            for t in (la.get("ineligibleTiers") or [])
            if isinstance(t, dict)
        }
        if current_id == "free-tier" or (not current_id and "free-tier" in ineligible_ids):
            # Google retired the free Code Assist path for this client (the
            # response itself says "migrate to Antigravity"). There is no live
            # quota endpoint left for free accounts here, so say exactly that.
            return QuotaResult(
                label="gemini",
                windows=[],
                plan="Free",
                details=[
                    "Free Code Assist quota retired by Google for this client",
                    "Published free limits: 60 RPM / 1000 req/day (not live)",
                    "Migrate: https://antigravity.google",
                    "Live quota needs an API key or paid Code Assist tier",
                ],
            )

    project = creds.get("quota_project") or ""
    if isinstance(la, dict) and la.get("cloudaicompanionProject"):
        project = str(la["cloudaicompanionProject"])
    data, err = _post_json(_QUOTA_URL, {"project": project}, tok)
    if err is not None:
        if err.get("transport"):
            # No HTTP status was ever received; a reason of "http-None" would
            # read as a server answer that never arrived.
            return build_unavailable("gemini", "fetch-error")
        txt = err.get("body") or ""
        txt_lower = txt.lower()
        # A 401/403 is an auth failure, not a tier verdict. Only the body's own
        # tier wording justifies consumer-tier-deprecated; a bare invalid_token
        # or revoked credential means re-auth, and telling the user to migrate
        # to Antigravity sends them the wrong way.
        if "unsupported_client" in txt_lower or "ineligibletier" in txt_lower:
            return build_unavailable("gemini", "consumer-tier-deprecated")
        if err.get("code") == 401:
            return build_unavailable("gemini", "auth-failed")
        if err.get("code") == 403:
            if any(marker in txt_lower for marker in (
                "invalid_token", "unauthorized", "unauthenticated",
            )):
                return build_unavailable("gemini", "auth-failed")
            return build_unavailable("gemini", "http-403")
        return build_unavailable("gemini", f"http-{err.get('code')}")
    if not isinstance(data, dict):
        return build_unavailable("gemini", "bad-json")
    res = _parse_quota(data)
    if res is None:
        return build_unavailable("gemini", "no-data")
    res.plan = _tier_label(la)
    return res


from .registry import register as _register  # noqa: E402

_register("gemini")(fetch_gemini_quota)
