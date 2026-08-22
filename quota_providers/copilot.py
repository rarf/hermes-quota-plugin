"""GitHub Copilot quota fetcher — plugin standalone copy.

Reads the Copilot internal usage API, the same primary source CodexBar uses:

    GET https://api.github.com/copilot_internal/user

with a GitHub OAuth token (``Authorization: token <gh token>``) and the
editor headers Copilot's own tooling sends.  The response carries
``quota_snapshots`` (``chat``, ``completions``, ``premium_interactions``),
each with a server-reported ``percent_remaining``, plus ``copilot_plan``
and a monthly ``quota_reset_date``.

Token resolution order (no device flow here — login is delegated):

1. ``hermes_cli.copilot_auth.resolve_copilot_token()`` when Hermes core is
   importable (env vars ``COPILOT_TOKEN``/``GITHUB_TOKEN``/``GH_TOKEN`` in
   priority order, then ``gh auth token``);
2. direct ``gh auth token`` subprocess fallback for standalone installs.

Classic PATs (``ghp_*``) are rejected by the Copilot API, mirroring
``resolve_copilot_token``'s validation.  Budget extras via github.com
browser cookies (CodexBar's optional "Budget extras") are intentionally
NOT implemented: cookie readers must be opt-in and the internal API above
already covers the primary meters.

Snapshot honesty rules:

* ``percent_remaining`` is server-reported, so ``used = 100 - remaining``
  has a real denominator — safe per the plugin's percentage contract.
* A snapshot with ``has_quota: false`` and zero entitlement means the
  account simply has no such quota (e.g. premium interactions on a
  token-billed individual plan) — it is skipped instead of rendering a
  fake "100% used" bar.
* Reset dates: the API only exposes a monthly reset date, applied to every
  window; ``quota_reset_at: 0`` per-snapshot is treated as absent.

Parsing is tolerant: ``quota_snapshots`` may also appear as
``quotaSnapshots`` and percent fields accept 0-100 or 0-1 fractions.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .base import QuotaResult, QuotaWindow, build_unavailable

_PROVIDER_ID = "copilot"
_API_URL = "https://api.github.com/copilot_internal/user"

# (snapshot key, window label) in display order — premium first, like CodexBar.
_SNAPSHOT_SLOTS = (
    ("premium_interactions", "Premium requests"),
    ("chat", "Chat"),
    ("completions", "Completions"),
)

_HEADERS = {
    "Accept": "application/json",
    "Editor-Version": "vscode/1.96.2",
    "Editor-Plugin-Version": "copilot-chat/0.26.7",
    "User-Agent": "GitHubCopilotChat/0.26.7",
    "X-Github-Api-Version": "2025-04-01",
}


# -- credential resolution -----------------------------------------------------


def resolve_github_token() -> Optional[str]:
    """Resolve a GitHub OAuth token, or None when nothing is available.

    Prefers Hermes core's resolver (env vars then ``gh auth token``); falls
    back to invoking ``gh auth token`` directly so the fetcher also works in
    standalone installs without ``hermes_cli`` on the path.
    """
    try:
        from hermes_cli.copilot_auth import resolve_copilot_token

        token, _source = resolve_copilot_token()
        # Core's resolver already covers env vars + `gh auth token`; only a
        # missing core (ImportError) needs this module's own gh fallback.
        return token or None
    except ImportError:
        return _gh_cli_token()
    except Exception:  # noqa: BLE001 - any resolver failure = no token
        return None


def _gh_cli_token() -> Optional[str]:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001 - gh missing / timed out
        return None
    if result.returncode != 0:
        return None
    token = (result.stdout or "").strip()
    return token or None


# -- tolerant response parsing --------------------------------------------------


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _normalize_reset(value: Any) -> Optional[str]:
    """Normalize the API's reset fields to ISO-8601, or None.

    ``quota_reset_date`` is a bare date ("2026-09-01"); ``*_utc`` variants
    are ISO timestamps.  Zero/empty values mean "absent".
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text in ("0", "0001-01-01"):
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",  # tz-less ISO: assume UTC like the Z variants
    ):
        try:
            parsed = datetime.strptime(text.replace("Z", "+0000"), fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return None


def _snapshots(data: dict) -> dict:
    for key in ("quota_snapshots", "quotaSnapshots"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _parse_snapshot(snapshot: dict) -> Optional[float]:
    """Used percent for one snapshot, or None when there is nothing honest.

    Skips snapshots the account does not have (``has_quota`` false with no
    entitlement and nothing consumed) so a missing quota never renders as a
    full bar.  Accepts 0-100 and 0-1 fraction percents.
    """
    if not isinstance(snapshot, dict):
        return None
    remaining = _as_float(snapshot.get("percent_remaining"))
    if remaining is None:
        return None
    if 0.0 < remaining < 1.0:
        # A strict fraction is unambiguously 0-1 scale; bare 0.0 and 1.0
        # stay on the 0-100 scale (same convention as CodexBar).
        remaining *= 100.0
    has_quota = snapshot.get("has_quota")
    entitlement = _as_float(snapshot.get("entitlement"))
    quota_remaining = _as_float(snapshot.get("quota_remaining"))
    if has_quota is False and not entitlement and not quota_remaining:
        return None  # quota not attached to this account — not "all used"
    return max(0.0, min(100.0, 100.0 - remaining))


def parse_usage_payload(data: Any) -> tuple[list[QuotaWindow], Optional[str], list[str]]:
    """Extract windows, plan label, and detail lines from the user payload."""
    if not isinstance(data, dict):
        return [], None, []
    snapshots = _snapshots(data)
    reset_at = _normalize_reset(
        data.get("quota_reset_date_utc") or data.get("quota_reset_date")
    )
    windows: list[QuotaWindow] = []
    for key, label in _SNAPSHOT_SLOTS:
        snapshot = snapshots.get(key)
        if not isinstance(snapshot, dict):
            continue
        used = _parse_snapshot(snapshot)
        if used is None:
            continue
        if snapshot.get("unlimited") is True:
            continue  # unlimited meters have no honest percent bar
        windows.append(QuotaWindow(label=label, used_percent=round(used, 2), reset_at=reset_at))
    plan = None
    plan_value = data.get("copilot_plan") or data.get("copilotPlan")
    if isinstance(plan_value, str) and plan_value.strip():
        plan = plan_value.strip().title()
    details: list[str] = []
    if data.get("token_based_billing") is True or data.get("tokenBasedBilling") is True:
        details.append("Billing: token-based")
    return windows, plan, details


# -- network --------------------------------------------------------------------


def fetch_usage(github_token: str) -> QuotaResult:
    headers = dict(_HEADERS)
    headers["Authorization"] = f"token {github_token}"
    request = urllib.request.Request(_API_URL, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return build_unavailable(_PROVIDER_ID, "auth-failed")
        return build_unavailable(_PROVIDER_ID, f"http-{exc.code}")
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return build_unavailable(_PROVIDER_ID, f"fetch-error:{type(exc).__name__}")

    try:
        data = json.loads(raw)
    except Exception:
        return build_unavailable(_PROVIDER_ID, "bad-json")

    windows, plan, details = parse_usage_payload(data)
    if not windows and not details:
        return build_unavailable(_PROVIDER_ID, "no-data")
    return QuotaResult(
        label=_PROVIDER_ID,
        windows=windows,
        plan=plan,
        details=details,
        unavailable_reason=None,
    )


def fetch_copilot_quota() -> QuotaResult:
    token = resolve_github_token()
    if not token:
        return build_unavailable(_PROVIDER_ID, "no-credentials")
    return fetch_usage(token)


from .registry import register as _register  # noqa: E402

_register("copilot")(fetch_copilot_quota)
