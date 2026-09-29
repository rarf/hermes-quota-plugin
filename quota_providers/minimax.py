"""MiniMax (Token Plan) quota fetcher.

Reads the same ``/v1/token_plan/remains`` endpoint MiniMax's own subscription
UI uses (documented in MiniMax's official Token Plan FAQ:

    curl --location 'https://www.minimax.io/v1/token_plan/remains' \\
         --header 'Authorization: Bearer <API Key>' \\
         --header 'Content-Type: application/json'

Auth: ``Authorization: Bearer *** key or OAuth access token``. MiniMax ships two
ways to obtain a Bearer for the same backend:

* **Subscription Key / API key** — stored by Hermes core for the ``minimax``
  / ``minimax-cn`` providers in ``~/.hermes/.env`` (``MINIMAX_API_KEY`` /
  ``MINIMAX_CN_API_KEY``) or the credential pool.
* **OAuth access token** — the ``minimax-oauth`` provider persists the access
  + refresh tokens in ``~/.hermes/auth.json`` after the user logs in via the
  browser. Hermes core's auth resolver surfaces either transparently through
  ``hermes_cli.auth._resolve_api_key_provider_secret``. No separate OAuth flow
  is implemented here — the key path covers both, since the endpoint accepts
  any valid Bearer.

Pay-as-you-go API keys are **not** supported: they target MiniMax's standard
Open Platform balance product, which has no documented quota-window endpoint.
If a pay-as-you-go key is used the endpoint returns
``{"base_resp":{"status_code":..., "status_msg":"plan not found"}}`` and this
fetcher reports ``no-subscription``.

Response shape (envelope around ``model_remains[]``)::

    {"model_remains": [
        {"model_name": "general",
         "current_interval_total_count": 10000,
         "current_interval_usage_count": 412,
         "current_interval_remaining_percent": 96,
         "current_weekly_total_count": 100000,
         "current_weekly_usage_count": 12345,
         "current_weekly_remaining_percent": 88,
         "end_time": 1773000000,
         "remains_time": 43200,
         "weekly_end_time": 1773600000},
        ...more model_name entries...
     ],
     "base_resp": {"status_code": 0, "status_msg": "success"}}

Mapping rules (window identity comes from the field prefix, never array
position — MiniMax is free to add/remove model entries):

* ``current_interval_*`` → ``Session`` 5-hour rolling window (per model);
* ``current_weekly_*``   → ``Weekly`` window (per model);
* remaining percent is the server-reported ``*_remaining_percent``; when
  absent it is computed from ``*_usage_count`` / ``*_total_count``;
* ``end_time`` / ``weekly_end_time`` are epoch seconds (defensively accepted
  in milliseconds too) → ISO-8601 UTC;
* the ``general`` model family aggregates every model under it; per-model
  cards render only when the entry carries both 5-hour and weekly counts.
* plan label is never invented — ``None`` unless the envelope carries one.

Fail-open contract: HTTP 401/403 → ``auth-failed``; non-zero
``base_resp.status_code`` → ``no-subscription`` when it indicates the key has
no Token Plan; schema surprise / no usable windows → ``no-data``; never a
fabricated zero and never an exception.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .base import QuotaResult, QuotaWindow, build_unavailable
from .registry import register

PROVIDER_ID = "minimax"
_QUOTA_PATH = "/v1/token_plan/remains"
# Official FAQ host. The third-party ``api.minimax.io`` mirror is also known to
# work — the plugin falls back to it when the canonical host 404s.
_HOSTS = ("https://www.minimax.io", "https://api.minimax.io")
# The whole provider must stay inside the cache sweep's budget (20 s), so the
# two hosts share one deadline instead of timing out twice in sequence.
_DEADLINE_S = 10.0
_REQUEST_TIMEOUT_S = 8.0
_MAX_BYTES = 1024 * 1024
# Env vars Hermes core's ``minimax`` / ``minimax-cn`` providers check.
_ENV_KEYS = ("MINIMAX_API_KEY", "MINIMAX_CN_API_KEY", "MINIMAX_OAUTH_TOKEN")

# Windows the response names use; the fetcher stays field-prefix based so a
# rename of the model bucket cannot hide a window.
_FIELD_SESSION_PREFIX = "current_interval_"
_FIELD_WEEKLY_PREFIX = "current_weekly_"

# Video is opt-in. Lowest Token Plan tiers don't include video generation at
# all, so the ``model_remains`` entry reports a meaningless 100% for them —
# showing it as a real bar would be misleading. Default off; users on higher
# tiers enable it via ``HERMES_QUOTA_MINIMAX_VIDEO_ENABLED`` / the plugin
# settings toggle.
_OPT_IN_VIDEO_ENV = "HERMES_QUOTA_MINIMAX_VIDEO_ENABLED"
_OPT_IN_VIDEO_CONFIG = "minimaxVideoEnabled"


# -- credential resolution ----------------------------------------------------


def _env_bearer() -> Optional[str]:
    for name in _ENV_KEYS:
        value = os.environ.get(name)
        if not value:
            continue
        trimmed = value.strip().strip("\"'")
        if trimmed:
            return trimmed
    return None


def _video_enabled() -> bool:
    """Opt-in for the ``video`` model bucket.

    Resolved from ``HERMES_QUOTA_MINIMAX_VIDEO_ENABLED`` first, then the
    plugin's own settings dict. Default off — lowest Token Plan tiers don't
    include video generation, so the bucket reports a meaningless 100%.
    """
    env = os.environ.get(_OPT_IN_VIDEO_ENV, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get("quota") if isinstance(entries, dict) else None
        if isinstance(entry, dict):
            settings = entry.get("settings")
            if isinstance(settings, dict) and _OPT_IN_VIDEO_CONFIG in settings:
                return bool(settings.get(_OPT_IN_VIDEO_CONFIG))
    except Exception:  # noqa: BLE001 - standalone install / locked store
        pass
    return False


def resolve_bearer() -> Optional[str]:
    """Resolve the MiniMax Bearer (Subscription Key OR OAuth access token).

    Core's resolver (``_resolve_api_key_provider_secret``) covers
    ``~/.hermes/.env`` env vars plus the credential pool in ``auth.json`` —
    that pool holds OAuth refresh/access tokens for ``minimax-oauth`` and the
    raw API key for ``minimax`` / ``minimax-cn``. A missing core (standalone
    install) falls through to this module's env-var check.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, _resolve_api_key_provider_secret

        for provider_id in ("minimax", "minimax-cn", "minimax-oauth"):
            pconfig = PROVIDER_REGISTRY.get(provider_id)
            if pconfig is None:
                continue
            auth_type = getattr(pconfig, "auth_type", "") or ""
            if auth_type not in ("api_key", "oauth"):
                continue
            try:
                key, _source = _resolve_api_key_provider_secret(provider_id, pconfig)
            except Exception:  # noqa: BLE001 - resolver may raise on locked store
                continue
            if key:
                return key
    except Exception:  # noqa: BLE001 - standalone install / locked store
        pass
    return _env_bearer()


# -- tolerant parsing --------------------------------------------------------


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


def _parse_reset(value: Any) -> Optional[str]:
    """Normalize end_time / weekly_end_time (epoch s or ms) to ISO-8601 UTC."""
    number = _as_float(value)
    if number is None or number <= 0:
        return None
    # Defensive: accept ms even though docs say seconds.
    if number > 100_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _remaining_percent(prefix: str, entry: dict) -> Optional[float]:
    """Server-reported remaining percent, else computed from usage/total.

    MiniMax reports *remaining* (0..100). The plugin's contract is *used*
    percent (0..100), so we invert. When the remaining field is absent we
    compute used from usage_count / total_count — a real denominator, never
    a fabricated ratio.
    """
    field = f"{prefix}remaining_percent"
    remaining = _as_float(entry.get(field))
    if remaining is None:
        used = _as_float(entry.get(f"{prefix}usage_count"))
        limit = _as_float(entry.get(f"{prefix}total_count"))
        if used is None or limit is None or limit <= 0:
            return None
        used_percent = used / limit * 100.0
    else:
        used_percent = 100.0 - remaining
    if used_percent < 0:
        return None
    return max(0.0, min(100.0, used_percent))


def _window_for(prefix: str, entry: dict, label: str) -> Optional[QuotaWindow]:
    used_percent = _remaining_percent(prefix, entry)
    if used_percent is None:
        return None
    reset_field = "end_time" if prefix == _FIELD_SESSION_PREFIX else "weekly_end_time"
    reset = _parse_reset(entry.get(reset_field))
    return QuotaWindow(label=label, used_percent=round(used_percent, 2), reset_at=reset)


def parse_quota_payload(payload: Any) -> QuotaResult:
    """Map the ``/v1/token_plan/remains`` envelope onto per-model Session/Weekly windows.

    Returns a QuotaResult even for unusable payloads (``unavailable_reason``
    set); never raises.
    """
    if not isinstance(payload, dict):
        return build_unavailable(PROVIDER_ID, "no-data")

    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict):
        status_code = base_resp.get("status_code")
        if isinstance(status_code, int) and status_code != 0:
            status_msg = str(base_resp.get("status_msg") or "").lower()
            if any(token in status_msg for token in ("plan", "subscript", "cookie", "auth")):
                return build_unavailable(PROVIDER_ID, "no-subscription")
            return build_unavailable(PROVIDER_ID, "no-data")

    models = payload.get("model_remains")
    if not isinstance(models, list) or not models:
        return build_unavailable(PROVIDER_ID, "no-data")

    windows: list[QuotaWindow] = []
    seen_labels: set[str] = set()

    for entry in models:
        if not isinstance(entry, dict):
            continue
        model_name = str(entry.get("model_name") or "").strip()
        if not model_name:
            continue

        # Video bucket is opt-in. On lower tiers it's a meaningless 100% —
        # drop it silently rather than rendering a fake bar.
        if model_name.lower() == "video" and not _video_enabled():
            continue

        # Display labels match the other providers' plain "Session" /
        # "Weekly" / "Monthly" naming instead of echoing the server's raw
        # ``model_name`` ("general" for text/image, "video" for video
        # generation). When MiniMax adds another model family, the prefix
        # returns so users can tell windows apart.
        lower_name = model_name.lower()
        if lower_name == "general":
            session_label, weekly_label = "Session", "Weekly"
        elif lower_name == "video":
            session_label, weekly_label = "Video Session", "Video Weekly"
        else:
            session_label, weekly_label = f"{model_name} · 5h", f"{model_name} · Weekly"

        # Only render one card per label even if the server returns multiple
        # entries sharing a name.
        session = _window_for(_FIELD_SESSION_PREFIX, entry, session_label)
        weekly = _window_for(_FIELD_WEEKLY_PREFIX, entry, weekly_label)

        for window in (session, weekly):
            if window is None or window.label in seen_labels:
                continue
            windows.append(window)
            seen_labels.add(window.label)

    if not windows:
        return build_unavailable(PROVIDER_ID, "no-data")

    return QuotaResult(
        label=PROVIDER_ID,
        windows=windows,
        plan=None,  # MiniMax's envelope carries no plan tier today; never invent.
        unavailable_reason=None,
        details=[],
    )


# -- network ------------------------------------------------------------------


class _ResponseTooLarge(Exception):
    """The endpoint answered with more than the plugin is willing to read."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a bearer credential to a redirected host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


_urlopen = urllib.request.build_opener(_NoRedirect()).open


def _get_json(url: str, bearer: str, timeout: float) -> Any:
    """GET ``url`` with the Bearer; raises on HTTP/parse failure."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
            "User-Agent": "hermes-quota-plugin",
        },
        method="GET",
    )
    with _urlopen(request, timeout=timeout) as resp:
        raw = resp.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise _ResponseTooLarge()
    return json.loads(raw)


def _http_reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "auth-failed"
        if exc.code == 404:
            return "host-not-found"
        return f"http-{exc.code}"
    if isinstance(exc, _ResponseTooLarge):
        return "response-too-large"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "bad-json"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    return "fetch-error"


@register(PROVIDER_ID)
def fetch_minimax_quota() -> QuotaResult:
    bearer = resolve_bearer()
    if not bearer:
        return build_unavailable(PROVIDER_ID, "no-credentials")

    deadline = time.monotonic() + _DEADLINE_S
    last_reason = "host-not-found"
    for index, host in enumerate(_HOSTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return build_unavailable(PROVIDER_ID, "timeout")
        try:
            payload = _get_json(f"{host}{_QUOTA_PATH}", bearer, min(_REQUEST_TIMEOUT_S, remaining))
            return parse_quota_payload(payload)
        except urllib.error.HTTPError as exc:
            # A 404 on the canonical host → try the mirror before giving up.
            if exc.code == 404 and index < len(_HOSTS) - 1:
                last_reason = "host-not-found"
                continue
            return build_unavailable(PROVIDER_ID, _http_reason(exc))
        except Exception as exc:  # noqa: BLE001 - fail-open by contract
            return build_unavailable(PROVIDER_ID, _http_reason(exc))

    return build_unavailable(PROVIDER_ID, last_reason)