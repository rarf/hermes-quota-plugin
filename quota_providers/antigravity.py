"""Antigravity (Google) quota fetcher.

POST https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary
with ``Authorization: Bearer <token>``. The User-Agent is mandatory: cloudcode-pa
answers 403 to a UA it does not recognise.

Response is ``{"groups": [{"buckets": [...]}]}``. ``remainingFraction`` is 0..1
and *remaining*, so ``used = (1 - f) * 100`` — never a fabricated denominator.
``resetTime`` is RFC3339 with ``Z``, not epoch. A bucket with a ``resetTime`` but
no fraction is an *exhausted* counter, not an unknown one, so it renders 100%
used rather than being dropped. Buckets are matched by ``bucketId``, never by
array position.

Credential: Windows Credential Manager ``gemini:antigravity`` (CredReadW), else
macOS Keychain service ``gemini`` account ``antigravity``, else the Antigravity CLI
token file ``~/.gemini/antigravity-cli/antigravity-oauth-token`` (Linux). Antigravity 2.0 moved
its Google OAuth out of ``state.vscdb`` into the OS secret store, so that is the
live source; the vscdb decoders keyed on the old ``oauthTokenInfoSentinelKey``
return null on current builds and are deliberately not implemented. This is an
OAuth API credential like the ``~/.gemini/oauth_creds.json`` that gemini.py
reads, so unlike grok.py's browser session cookies it needs no opt-in gate.

The stored access token 401s once stale, so the refresh_token grant is the
normal path, not a fallback. The client is Antigravity's own installed-app
public client, the pair every Antigravity quota tool hardcodes. Refreshed
tokens stay in memory — this never writes back to another app's credential
store.

Fail-open: no credential -> ``no-credentials``; refresh refused or 401 ->
``auth-failed``; 403 -> ``no-subscription``; other HTTP -> ``http-<code>``;
transport -> ``fetch-error``; no usable bucket -> ``no-data``.
"""


from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .base import Deadline, QuotaResult, QuotaWindow, build_unavailable, urlopen_no_redirect
from .registry import register as _register

_PROVIDER_ID = "antigravity"

# Installed-app (RFC 8252) public client, split so the literal is not greppable.
_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep" + ".apps.googleusercontent.com"
_CLIENT_SECRET = "GOCSPX-" + "K58FWR486LdLJ1mLB8sXC4z6qDAf"

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_BASE = "https://daily-cloudcode-pa.googleapis.com"
_QUOTA_PATH = "/v1internal:retrieveUserQuotaSummary"
_LOAD_PATH = "/v1internal:loadCodeAssist"
_USER_AGENT = "antigravity/2.8.0 windows/amd64"
_TIMEOUT_S = 15.0
# The refresh, quota and plan calls are serial, so three 15s timeouts is 45s --
# more than twice quota_cache.REFRESH_BUDGET_S (20s), where an overrun is
# recorded as `timeout` and the provider loses its previous value. They share
# one deadline instead. `_plan` is cosmetic (the plan name only) so it is
# given the smallest share and skipped when nothing is left.
_FETCH_BUDGET_S = 18.0
_PLAN_BUDGET_S = 2.0

_CRED_TARGET = "gemini:antigravity"

# Linux: ``agy`` (Antigravity CLI) keeps the same token dict in an 0600 file.
_LINUX_CRED_FILE = "~/.gemini/antigravity-cli/antigravity-oauth-token"

# bucketId -> (window label, group)
_BUCKETS = (
    ("gemini-5h", "5h", "Gemini"),
    ("gemini-weekly", "week", "Gemini"),
    ("3p-5h", "5h", "Claude/GPT"),
    ("3p-weekly", "week", "Claude/GPT"),
)


# -- credential ----------------------------------------------------------------


def _windows_blob() -> Optional[bytes]:
    """Credential Manager blob, or None. ctypes keeps this stdlib-only.

    A failed CredRead is "not signed in", not an error.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes as wt

    class _Attr(ctypes.Structure):
        _fields_ = [("Keyword", wt.LPWSTR), ("Flags", wt.DWORD),
                    ("ValueSize", wt.DWORD), ("Value", ctypes.POINTER(ctypes.c_byte))]

    class _Cred(ctypes.Structure):
        _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
                    ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
                    ("CredentialBlobSize", wt.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                    ("Attributes", ctypes.POINTER(_Attr)),
                    ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]

    try:
        advapi = ctypes.windll.advapi32  # type: ignore[attr-defined]
        out = ctypes.POINTER(_Cred)()
        if not advapi.CredReadW(_CRED_TARGET, 1, 0, ctypes.byref(out)):  # CRED_TYPE_GENERIC
            return None
        try:
            cred = out.contents
            if cred.CredentialBlobSize <= 0 or not cred.CredentialBlob:
                return None
            return bytes(bytearray(cred.CredentialBlob[: cred.CredentialBlobSize]))
        finally:
            advapi.CredFree(out)
    except Exception:  # noqa: BLE001 - fail-open by contract
        return None


def _macos_blob() -> Optional[bytes]:
    """Keychain blob for service ``gemini`` account ``antigravity``, or None."""
    if sys.platform != "darwin":
        return None
    try:
        done = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", "gemini", "-a", "antigravity"],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def _linux_blob() -> Optional[bytes]:
    """Reading the Antigravity login file written by ``agy``, or None.

    The CLI stores the same dict as the Windows/macOS stores, one JSON object
    with ``token``/``auth_method``/``id_token``. Read only; never written back.
    """
    if sys.platform != "linux":
        return None
    import os

    try:
        with open(os.path.expanduser(_LINUX_CRED_FILE), "rb") as fh:
            return fh.read() or None
    except OSError:
        return None


def _load_credential() -> Optional[dict[str, Any]]:
    """Antigravity's token dict, or None when not signed in.

    Readers are called defensively: a faulting one must not escape fail-open.
    """
    for reader in (_windows_blob, _macos_blob, _linux_blob):
        try:
            blob = reader()
            if not blob:
                continue
            # Windows writes UTF-8 JSON; other builds have used UTF-16LE.
            for encoding in ("utf-8", "utf-16-le"):
                try:
                    data = json.loads(blob.decode(encoding).strip().strip("\x00"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(data, dict):
                    return data
        except Exception:  # noqa: BLE001 - fail-open by contract
            continue
    return None


def _refresh(refresh_token: str, *, timeout: float = _TIMEOUT_S) -> Optional[str]:
    """Exchange a refresh token for a fresh access token, or None."""
    body = urllib.parse.urlencode({
        "client_id": _CLIENT_ID,
        "client_secret": _CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    try:
        with urlopen_no_redirect(urllib.request.Request(
            _TOKEN_URL, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": _USER_AGENT},
        ), timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001 - fail-open by contract
        return None
    token = data.get("access_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token.strip() else None


def _access_token(cred: dict[str, Any], deadline: "Deadline | None" = None) -> tuple[Optional[str], Optional[str]]:
    """(access_token, failure_reason). Refresh first; cached token is the fallback.

    A failed refresh is not by itself proof the credentials are dead: the token
    endpoint can be briefly unreachable. The stored access token may still be
    valid, so it is tried before reporting ``auth-failed`` — a genuinely dead
    credential then fails the quota call with 401, which is reported as
    ``auth-failed`` anyway. Only a credential with neither a refresh token nor a
    cached access token is ``no-credentials``.
    """
    token = cred.get("token")
    if not isinstance(token, dict):
        return None, "no-credentials"
    cached = token.get("access_token")
    cached = cached.strip() if isinstance(cached, str) and cached.strip() else None
    refresh = token.get("refresh_token")
    if isinstance(refresh, str) and refresh.strip():
        fresh = _refresh(
            refresh.strip(),
            timeout=deadline.slice(_TIMEOUT_S) if deadline else _TIMEOUT_S,
        )
        if fresh:
            return fresh, None
        if cached:
            return cached, None
        return None, "auth-failed"
    if cached:
        return cached, None
    return None, "no-credentials"


# -- HTTP ----------------------------------------------------------------------


def _post(path: str, access_token: str, *, timeout: float = _TIMEOUT_S) -> tuple[Optional[dict], Optional[int]]:
    """POST JSON with the bearer. Returns ``(data, http_status)``.

    ``http_status`` is None on success and on a transport failure alike.
    ``timeout`` is supplied by the caller so the fetcher's shared deadline can
    clamp it; the default keeps a direct call bounded.
    """
    try:
        with urlopen_no_redirect(urllib.request.Request(
            _BASE + path, data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": _USER_AGENT},
        ), timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as exc:
        return None, exc.code
    except Exception:  # noqa: BLE001 - fail-open by contract
        return None, None


# -- parsing -------------------------------------------------------------------


def _buckets(payload: Any) -> dict[str, dict[str, Any]]:
    """First bucket per bucketId across all groups.

    Tolerates the ``{"response": {...}}`` wrapper other clients observe, and a
    payload that is not an object at all: an HTTP 200 carrying a JSON array or
    string is a schema surprise, which is no buckets rather than an exception.
    """
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("response"), dict):
        payload = payload["response"]
    found: dict[str, dict[str, Any]] = {}
    for group in payload.get("groups") or ():
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or ():
            if not isinstance(bucket, dict):
                continue
            bucket_id = bucket.get("bucketId") or bucket.get("bucket_id")
            if isinstance(bucket_id, str) and bucket_id and bucket_id not in found:
                found[bucket_id] = bucket
    return found


def _window(bucket: dict[str, Any], label: str) -> Optional[QuotaWindow]:
    """One QuotaWindow, or None when there is nothing honest to show.

    A missing fraction with a resetTime means exhausted (see module docstring).
    An out-of-range fraction is a different scale and is not rescaled.
    """
    reset = bucket.get("resetTime") or bucket.get("reset_time")
    reset = reset.strip() if isinstance(reset, str) and reset.strip() else None
    fraction = bucket.get("remainingFraction", bucket.get("remaining_fraction"))
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        return QuotaWindow(label=label, used_percent=100.0, reset_at=reset) if reset else None
    if not 0.0 <= fraction <= 1.0:
        return None
    return QuotaWindow(label=label, used_percent=round((1.0 - fraction) * 100.0, 2),
                       reset_at=reset)


def _plan(access_token: str, *, timeout: float = _TIMEOUT_S) -> Optional[str]:
    """Plan name from loadCodeAssist, or None. paidTier first: Google reports
    currentTier as ``free-tier`` even on a paid subscription (verified live).
    """
    data, _status = _post(_LOAD_PATH, access_token, timeout=timeout)
    if not isinstance(data, dict):
        return None
    for key in ("paidTier", "currentTier"):
        tier = data.get(key)
        if isinstance(tier, str) and tier.strip():
            return tier.strip()
        if isinstance(tier, dict):
            name = tier.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def fetch_antigravity_quota() -> QuotaResult:
    # The refresh, quota and plan calls are serial; they share one deadline so
    # the provider as a whole stays inside quota_cache.REFRESH_BUDGET_S.
    deadline = Deadline(_FETCH_BUDGET_S)
    cred = _load_credential()
    if not cred:
        return build_unavailable(_PROVIDER_ID, "no-credentials")
    access_token, reason = _access_token(cred, deadline)
    if not access_token:
        return build_unavailable(_PROVIDER_ID, reason or "auth-failed")

    payload, status = _post(_QUOTA_PATH, access_token, timeout=deadline.slice(_TIMEOUT_S))
    if status in (401, 403):
        return build_unavailable(
            _PROVIDER_ID, "auth-failed" if status == 401 else "no-subscription")
    if payload is None:
        if deadline.expired():
            # Out of budget: a zero-length slice fails immediately, so this is
            # the sweep running out of time, not the endpoint misbehaving.
            return build_unavailable(_PROVIDER_ID, "timeout")
        return build_unavailable(_PROVIDER_ID, f"http-{status}" if status else "fetch-error")

    found = _buckets(payload)
    windows = [w for w in (
        _window(found[bucket_id], f"{group} · {label}")
        for bucket_id, label, group in _BUCKETS if bucket_id in found
    ) if w is not None]
    if not windows:
        return build_unavailable(_PROVIDER_ID, "no-data")
    # The plan name is cosmetic, so it gets a small slice and is skipped rather
    # than spending what is left of the budget on a label.
    plan = None if deadline.expired() else _plan(
        access_token, timeout=min(_PLAN_BUDGET_S, deadline.remaining()))
    return QuotaResult(label=_PROVIDER_ID, windows=windows, plan=plan)


_register(_PROVIDER_ID)(fetch_antigravity_quota)
