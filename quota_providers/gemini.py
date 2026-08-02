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

from .base import QuotaResult, QuotaWindow, build_unavailable

_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_CREDS_PATH = os.path.join(os.path.expanduser("~"), ".gemini", "oauth_creds.json")


def _b64urldecode(s: str) -> dict:
    pad = "=" * (-len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(s + pad).decode("utf-8", "replace"))


def _extract_client_from_js() -> tuple[Optional[str], Optional[str]]:
    import subprocess

    try:
        out = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=10)
        npm_root = out.stdout.strip()
    except Exception:
        npm_root = ""
    if not npm_root:
        return None, None
    candidates = []
    for root, _dirs, files in os.walk(npm_root):
        if os.path.basename(root) == "code_assist":
            candidates.append(os.path.join(root, "oauth2.js"))
    for c in candidates:
        try:
            txt = open(c, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        import re

        m = re.findall(r'OAUTH_CLIENT_ID\s*=\s*["\']([^"\']+)["\']', txt)
        s = re.findall(r'OAUTH_CLIENT_SECRET\s*=\s*["\']([^"\']+)["\']', txt)
        if m and s:
            return m[0], s[0]
    return None, None


def _load_creds() -> Optional[dict]:
    try:
        with open(_CREDS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _refresh(creds: dict) -> Optional[str]:
    cid = os.environ.get("GEMINI_OAUTH_CLIENT_ID")
    csec = os.environ.get("GEMINI_OAUTH_CLIENT_SECRET")
    if not (cid and csec):
        cid, csec = _extract_client_from_js()
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read())
        return d.get("access_token")
    except Exception:
        return None


def _valid_token(creds: dict) -> Optional[str]:
    exp = creds.get("expiry_date")
    tok = creds.get("access_token")
    if not tok:
        return None
    if exp and time.time() * 1000 >= float(exp) - 30000:
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
        try:
            left = round(float(frac) * 100.0, 2)
        except (TypeError, ValueError):
            continue
        used = round(100.0 - left, 2)
        reset = b.get("resetTime")
        model = b.get("modelId") or "gemini"
        w = QuotaWindow(label=model, used_percent=used, reset_at=reset)
        if best is None or (left < (100 - (best.used_percent or 0))):
            best = w
    if best is None:
        return None
    return QuotaResult(label="gemini", windows=[best], plan=None, unavailable_reason=None)


def fetch_gemini_quota() -> QuotaResult:
    creds = _load_creds()
    if not creds:
        return build_unavailable("gemini", "no-credentials")
    tok = _valid_token(creds)
    if not tok:
        return build_unavailable("gemini", "token-refresh-failed")
    project = creds.get("quota_project") or ""
    body = json.dumps({"project": project}).encode("utf-8")
    req = urllib.request.Request(
        _QUOTA_URL,
        data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        if e.code in (401, 403) or "UNSUPPORTED_CLIENT" in txt or "IneligibleTier" in txt:
            return build_unavailable("gemini", "consumer-tier-deprecated")
        return build_unavailable("gemini", f"http-{e.code}")
    except Exception as e:
        return build_unavailable("gemini", f"fetch-error:{type(e).__name__}")
    res = _parse_quota(data)
    if res is None:
        return build_unavailable("gemini", "no-data")
    return res


from .registry import register as _register  # noqa: E402

_register("gemini")(fetch_gemini_quota)
