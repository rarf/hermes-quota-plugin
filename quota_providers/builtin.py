"""Built-in provider quota fetchers and their normalized adapters.

OpenAI Codex and Nous adapt core account data. Anthropic
is fetched directly because its single OAuth payload contains both the legacy
windows and newer model-scoped limits. We adapt those snapshots into the
plugin's QuotaResult shape and register them so the cache builder treats them
uniformly with the other fetchers.

Anthropic also supports additional Claude subscription logins: an explicit,
opt-in ``claudeAccounts`` list in the plugin settings points at other Claude
config directories, each read read-only for its access token. Those accounts
are attached as ``QuotaAccount`` rows and flattened by the cache into sibling
provider rows, so the footer, /quota and the widget render them with no
account-specific code. No account is read unless it was listed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import contextvars
import inspect
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from .base import (
    GENERATED_ACCOUNT_ID_PREFIX,
    AccountBalance,
    Deadline,
    QuotaAccount,
    QuotaResult,
    QuotaWindow,
    build_unavailable,
    urlopen_no_redirect,
)
from .registry import register as _register


def _snapshot_to_result(snapshot) -> QuotaResult:
    provider = getattr(snapshot, "provider", "unknown")
    windows = []
    for w in getattr(snapshot, "windows", ()) or ():
        used = getattr(w, "used_percent", None)
        reset = getattr(w, "reset_at", None)
        reset_iso = None
        if reset is not None:
            from datetime import datetime, timezone

            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=timezone.utc)
            reset_iso = reset.isoformat()
        windows.append(
            QuotaWindow(
                label=str(getattr(w, "label", "") or "window"),
                used_percent=float(used) if used is not None else None,
                reset_at=reset_iso,
            )
        )
    return QuotaResult(
        label=str(provider),
        windows=windows,
        plan=getattr(snapshot, "plan", None),
        unavailable_reason=getattr(snapshot, "unavailable_reason", None),
        details=[str(d) for d in (getattr(snapshot, "details", ()) or ())],
    )


def _make_fetcher(provider_id: str):
    def _fetch() -> QuotaResult:
        try:
            from agent.account_usage import fetch_account_usage
        except Exception:
            return build_unavailable(provider_id, "fetcher-unavailable")
        try:
            snap = fetch_account_usage(provider_id)
        except Exception:
            return build_unavailable(provider_id, "fetch-error")
        if snap is None:
            return build_unavailable(provider_id, "no-data")
        return _snapshot_to_result(snap)

    return _fetch


def _core_anthropic_token() -> Optional[str]:
    """Resolvable Anthropic token per core auth, or None. Never raises."""
    try:
        from agent.anthropic_credentials import resolve_anthropic_token

        token = (resolve_anthropic_token() or "").strip()
        return token or None
    except Exception:  # noqa: BLE001 - standalone install / locked store
        return None


def _core_anthropic_is_oauth(token: str) -> Optional[bool]:
    """Use the core token classifier when it is available."""
    try:
        from agent.anthropic_adapter import _is_oauth_token

        return bool(_is_oauth_token(token))
    except Exception:
        # Standalone plugin installs may not ship the core classifier; the
        # vendor response remains the source of truth in that case.
        return None


_ANTHROPIC_OAUTH_REQUIRED_REASON = (
    "Anthropic account limits are only available for OAuth-backed Claude accounts."
)


_ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Keep one direct usage read below the cache refresh budget. The response is
# parsed for both the legacy top-level windows and the newer ``limits`` list.
_ANTHROPIC_TIMEOUT_S = 15.0
# The whole multi-account fetch (primary plus every extra account, run
# serially) shares this budget, so N accounts cannot each spend the full
# timeout and blow through ``quota_cache.REFRESH_BUDGET_S`` (20s). Serially is
# deliberate: it bounds concurrent work to one request at a time and lets a
# spent budget mark later accounts ``timeout`` truthfully instead of opening N
# threads.
_ANTHROPIC_MULTI_BUDGET_S = 18.0
# Extra Claude subscription accounts live in the quota plugin settings as
# ``claudeAccounts``: an explicit, opt-in list of {id, label, configDir}. No
# account is read unless the user added it here; an absent setting keeps the
# single-account path byte-identical.
_CLAUDE_ACCOUNTS_SETTING = "claudeAccounts"
_CLAUDE_ACCOUNT_ID_MAX = 64
_MAX_CLAUDE_ACCOUNTS = 8
_CLAUDE_CREDENTIALS_FILENAME = ".credentials.json"
# Upper bound on a single ``.credentials.json`` read. The real file is a few
# hundred bytes of JSON; anything larger is refused rather than slurped, and the
# file is only ever read through a stat-verified regular-file descriptor.
_MAX_CREDENTIAL_BYTES = 65536
# Upper bound on one usage response body, read with a ``+1`` probe so an
# oversized body is detected rather than silently truncated.
_MAX_HTTP_BYTES = 65536
_CLAUDE_LIMIT_REASON = "config-limit"
_MACOS_UNSUPPORTED_DETAIL = (
    "macOS Claude Code keeps its login in the Keychain (service "
    "\"Claude Code-credentials\"); per-directory Keychain items are not "
    "documented or read here. Point configDir at a directory that contains "
    ".credentials.json, or use the default Hermes login."
)
_ACCOUNT_TIMEOUT_DETAIL = "Shared refresh budget was spent before this account was read."
_ACCOUNT_LIMIT_DETAIL = (
    f"Only the first {_MAX_CLAUDE_ACCOUNTS} claudeAccounts entries are read."
)


def parse_anthropic_scoped_limits(payload: Any) -> list[QuotaWindow]:
    """Model-scoped and all-model weekly limits from ``/api/oauth/usage``.

    Core maps only the fixed ``five_hour``/``seven_day*`` keys. Newer models
    get opaque codenamed keys instead; the display name lives in the
    ``limits`` list::

        {"kind": "weekly_scoped", "group": "weekly", "percent": 0,
         "resets_at": "2026-09-30T08:00:00+00:00",
         "scope": {"model": {"id": null, "display_name": "Fable"}, "surface": null}}
    """
    limits = payload.get("limits") if isinstance(payload, dict) else None
    windows: list[QuotaWindow] = []
    seen: set[str] = set()
    for entry in limits if isinstance(limits, list) else ():
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind not in {"session", "weekly_all", "weekly_scoped"}:
            continue
        percent = entry.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        percent = float(percent)
        if not math.isfinite(percent):
            continue

        if kind == "session":
            label = "Current session"
        elif kind == "weekly_all":
            label = "Current week"
        else:
            scope = entry.get("scope") or {}
            model = scope.get("model") if isinstance(scope, dict) else None
            if not isinstance(model, dict):
                model = {}
            name = model.get("display_name") or model.get("id")
            if not isinstance(name, str) or not name.strip():
                surface = scope.get("surface") if isinstance(scope, dict) else None
                name = surface
            if not isinstance(name, str) or not name.strip():
                continue
            label = f"{name.strip()} week"

        if label in seen:
            continue
        seen.add(label)
        reset = entry.get("resets_at")
        if isinstance(reset, str) and reset.endswith("Z"):
            reset = reset[:-1] + "+00:00"
        windows.append(QuotaWindow(
            label=label,
            used_percent=max(0.0, min(100.0, percent)),
            reset_at=reset if isinstance(reset, str) and reset else None,
        ))
    return windows


# After an HTTP 429 from the usage endpoint, stop reading that account for this
# long. The widget refreshes on a timer, so without a cooldown every cycle
# re-hits a rate-limited endpoint. Keyed by a token digest, never the token.
_ANTHROPIC_RATE_LIMIT_COOLDOWN_S = 30 * 60
_ANTHROPIC_BACKOFF_FILENAME = "quota_anthropic_backoff.json"


def _shared_state_home() -> str:
    # Profiles normally share one Claude login, and the usage endpoint rate-limits
    # per token, not per profile. Keep this state in the Hermes ROOT rather than
    # the active profile home so every profile sees the same cooldown and the same
    # recent payload instead of each one polling the endpoint on its own.
    try:
        from hermes_constants import get_default_hermes_root

        return str(get_default_hermes_root())
    except Exception:  # noqa: BLE001 - older core: fall back to the profile home
        pass
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:  # noqa: BLE001 - fail-open: fall back to the plugin dir
        return str(Path(__file__).resolve().parent.parent)


def _backoff_state_path() -> str:
    return os.path.join(_shared_state_home(), _ANTHROPIC_BACKOFF_FILENAME)


# A successful usage payload is reused for the same token (keyed by digest, never
# the token itself) across profiles: fresh for _ANTHROPIC_SHARED_TTL_S, and as a
# last-known value for up to _ANTHROPIC_SHARED_STALE_MAX_S while a 429 cooldown
# is active, so the card keeps its numbers instead of going blank.
_ANTHROPIC_SHARED_TTL_S = 300
_ANTHROPIC_SHARED_STALE_MAX_S = 60 * 60
_ANTHROPIC_SHARED_FILENAME = "quota_anthropic_shared.json"


@contextmanager
def _anthropic_state_lock(path: str, deadline: float):
    """Persistent advisory lock: never unlink a lock another process may hold.

    Unsupported/unwritable storage preserves the historical fail-open read.
    Contention times out without issuing a duplicate HTTP request.
    """
    import time

    fd = None
    acquired = None
    try:
        try:
            import fcntl
            def try_lock():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt
            def try_lock():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

        fd = _open_anthropic_state(path, os.O_CREAT | os.O_RDWR)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('nonregular lock')
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'\0')
        while True:
            try:
                try_lock()
                acquired = True
                break
            except OSError as exc:
                import errno
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    acquired = False
                    break
                time.sleep(min(.02, remaining))
    except (ImportError, OSError, AttributeError):
        pass
    try:
        yield acquired
    finally:
        if fd is not None:
            os.close(fd)


def _shared_payload_path() -> str:
    # Lives beside the backoff state, so whatever relocates that file relocates
    # this one too.
    return os.path.join(os.path.dirname(_backoff_state_path()) or ".", _ANTHROPIC_SHARED_FILENAME)


_ANTHROPIC_STATE_MAX_BYTES = 1024 * 1024


def _open_anthropic_state(path: str, flags: int):
    """Validate both the path and the opened file, including on Windows."""
    import stat

    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise OSError('nonregular shared state')
    except FileNotFoundError:
        before = None
    fd = os.open(path, flags | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        opened = os.fstat(fd)
        after = os.lstat(path)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(after.st_mode)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
                or (before is not None and (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino))):
            raise OSError('shared state changed during open')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_anthropic_state(path: str):
    """Read only a bounded regular file without following a final symlink."""
    import stat

    fd = _open_anthropic_state(path, os.O_RDONLY)
    with os.fdopen(fd, 'rb') as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _ANTHROPIC_STATE_MAX_BYTES:
            raise ValueError('invalid shared state file')
        body = fh.read(_ANTHROPIC_STATE_MAX_BYTES + 1)
    if len(body) > _ANTHROPIC_STATE_MAX_BYTES:
        raise ValueError('oversize shared state')
    return json.loads(body)


def _anthropic_state_writable(path: str) -> bool:
    import stat

    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return True


def _state_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (OverflowError, ValueError):
        return None


class _ObservedAnthropicPayload(dict):
    """Keep local cache provenance out of the vendor's payload keys."""

    def __init__(self, payload, observed_at, stale):
        super().__init__(payload)
        self.observed_at = observed_at
        self.stale = stale


def _load_shared_payload(token: str, max_age: float = _ANTHROPIC_SHARED_TTL_S) -> Optional[dict[str, Any]]:
    import time

    try:
        data = _read_anthropic_state(_shared_payload_path())
        entry = data.get(_backoff_key(token)) if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            return None
        at = _state_number(entry.get("at"))
        payload = entry.get("payload")
        if at is None or not isinstance(payload, dict):
            return None
        age = time.time() - float(at)
        if math.isfinite(age) and 0 <= age <= max_age:
            return _ObservedAnthropicPayload(payload, float(at), age > _ANTHROPIC_SHARED_TTL_S)
    except Exception:  # noqa: BLE001 - missing/corrupt state means no shared payload
        pass
    return None


def _store_shared_payload(token: str, payload: dict[str, Any], *, deadline=None) -> None:
    import time

    deadline = time.monotonic() + .25 if deadline is None else deadline
    with _anthropic_state_lock(_shared_payload_path() + '.lock', deadline) as acquired:
        if acquired is True:
            _store_shared_payload_locked(token, payload)


def _store_shared_payload_locked(token: str, payload: dict[str, Any]) -> None:
    import tempfile
    import time

    tmp = None
    try:
        path = _shared_payload_path()
        if not _anthropic_state_writable(path):
            return
        try:
            data = _read_anthropic_state(path)
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            # Missing or demonstrably corrupt JSON may be initialized. An
            # uncertain read must abort, not erase other accounts' payloads.
            data = {}
        if not isinstance(data, dict):
            data = {}
        now = time.time()
        kept: dict[str, Any] = {}
        for key, entry in data.items():
            at = _state_number(entry.get("at")) if isinstance(entry, dict) else None
            if at is not None and 0 <= now - at <= _ANTHROPIC_SHARED_STALE_MAX_S:
                kept[str(key)] = entry
        kept[_backoff_key(token)] = {"at": now, "payload": payload}
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(path) or ".", prefix=".anthropic_shared.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(kept, fh)
        os.replace(tmp, path)
        tmp = None
    except Exception:  # noqa: BLE001 - failing to persist must not break a read
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _backoff_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def _load_backoff(*, for_update: bool = False) -> dict[str, float]:
    """Fail open for requests, but require a certain read before merging writes."""
    try:
        data = _read_anthropic_state(_backoff_state_path())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    except Exception:  # noqa: BLE001 - missing/corrupt state means no cooldown
        if for_update:
            raise
        return {}
    if not isinstance(data, dict):
        return {}
    import time

    # Reject non-finite values (Infinity/1e999 parse as inf and would lock the
    # account forever) and anything later than one cooldown from now (clock
    # jumps). Bool is an int subclass, so it is excluded explicitly.
    ceiling = time.time() + _ANTHROPIC_RATE_LIMIT_COOLDOWN_S
    out: dict[str, float] = {}
    for key, value in data.items():
        number = _state_number(value)
        if number is None or number > ceiling:
            continue
        out[str(key)] = number
    return out


def _save_backoff(state: dict[str, float]) -> None:
    import tempfile

    tmp = None
    try:
        path = _backoff_state_path()
        if not _anthropic_state_writable(path):
            return
        # mkstemp gives a unique 0600 file per writer, so concurrent writers
        # cannot interleave in one shared temp file.
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(path) or ".", prefix=".anthropic_backoff.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - failing to persist must not break a read
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _anthropic_cooling_down(token: str) -> bool:
    import time

    now = time.time()
    state = _load_backoff()
    return state.get(_backoff_key(token), 0.0) > now


def _open_anthropic_cooldown(token: str, *, deadline=None) -> None:
    import time

    deadline = time.monotonic() + .25 if deadline is None else deadline
    with _anthropic_state_lock(_backoff_state_path() + '.lock', deadline) as acquired:
        if acquired is True:
            _open_anthropic_cooldown_locked(token)


def _open_anthropic_cooldown_locked(token: str) -> None:
    import time

    now = time.time()
    try:
        state = {k: v for k, v in _load_backoff(for_update=True).items() if v > now}
    except Exception:  # noqa: BLE001 - uncertain reads must not replace state
        return
    state[_backoff_key(token)] = now + _ANTHROPIC_RATE_LIMIT_COOLDOWN_S
    _save_backoff(state)


def _request_anthropic_usage(
    token: str, *, timeout: float = _ANTHROPIC_TIMEOUT_S
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    import time

    deadline = time.monotonic() + max(0.0, timeout)
    path = _shared_payload_path() + '.' + _backoff_key(token) + '.lock'
    with _anthropic_state_lock(path, deadline) as acquired:
        if acquired is False:
            return None, 'timeout'
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, 'timeout'
        return _request_anthropic_usage_locked(token, timeout=remaining)


def _request_anthropic_usage_locked(
    token: str, *, timeout: float
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """One bounded, redirect-free usage read for a given bearer token.

    ``token`` is passed in, never resolved here, so the same request path
    serves the core-resolved account and every configured extra account.
    """
    import time

    deadline = time.monotonic() + timeout
    shared = _load_shared_payload(token)
    if shared is not None:
        return shared, None
    if _anthropic_cooling_down(token):
        stale = _load_shared_payload(token, _ANTHROPIC_SHARED_STALE_MAX_S)
        if stale is not None:
            return stale, None
        return None, "rate-limited"
    request = urllib.request.Request(
        _ANTHROPIC_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "hermes-quota-plugin/2.11.0",
        },
        method="GET",
    )
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, 'timeout'
        with urlopen_no_redirect(request, timeout=remaining) as resp:
            body = resp.read(_MAX_HTTP_BYTES + 1)
        if len(body) > _MAX_HTTP_BYTES:
            return None, "bad-json"
        payload = json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "auth-failed"
        if exc.code == 429:
            _open_anthropic_cooldown(token, deadline=deadline)
            stale = _load_shared_payload(token, _ANTHROPIC_SHARED_STALE_MAX_S)
            if stale is not None:
                return stale, None
        return None, f"http-{exc.code}"
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return None, f"fetch-error:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "bad-json"
    _store_shared_payload(token, payload, deadline=deadline)
    return payload, None


def _anthropic_usage_payload() -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Fetch the OAuth usage payload once for the core-resolved account."""
    token = _core_anthropic_token()
    if not token:
        return None, "no-credentials"
    if _core_anthropic_is_oauth(token) is False:
        return None, _ANTHROPIC_OAUTH_REQUIRED_REASON
    return _request_anthropic_usage(token)


def _parse_anthropic_usage(payload: dict[str, Any]) -> tuple[list[QuotaWindow], list[str]]:
    windows: list[QuotaWindow] = []
    seen: set[str] = set()
    for key, label in (
        ("five_hour", "Current session"),
        ("seven_day", "Current week"),
        ("seven_day_opus", "Opus week"),
        ("seven_day_sonnet", "Sonnet week"),
    ):
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        utilization = window.get("utilization")
        if isinstance(utilization, bool):
            continue
        try:
            used = float(utilization)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(used):
            continue
        # A 0-1 value is a fraction, except a bare 1.0 which is a whole percent:
        # the same rule issue #8 settled for opencode_go (`f9e6255`, "preserve
        # integer percent values"). `used <= 1` rescaled a genuine 1% to 100%.
        if 0.0 < used < 1.0:
            used *= 100
        if label in seen:
            continue
        seen.add(label)
        reset = window.get("resets_at")
        if isinstance(reset, str) and reset.endswith("Z"):
            reset = reset[:-1] + "+00:00"
        windows.append(QuotaWindow(
            label=label,
            used_percent=max(0.0, min(100.0, used)),
            reset_at=reset if isinstance(reset, str) and reset else None,
        ))

    for window in parse_anthropic_scoped_limits(payload):
        if window.label not in seen:
            seen.add(window.label)
            windows.append(window)

    details: list[str] = []
    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = extra.get("used_credits")
        monthly = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if (
            isinstance(used, (int, float))
            and not isinstance(used, bool)
            and isinstance(monthly, (int, float))
            and not isinstance(monthly, bool)
        ):
            details.append(f"Extra usage: {used:.2f} / {monthly:.2f} {currency}")
    if (windows or details) and isinstance(payload, _ObservedAnthropicPayload):
        import time
        from datetime import datetime, timezone

        observed = datetime.fromtimestamp(payload.observed_at, timezone.utc).isoformat()
        age = max(0, int(time.time() - payload.observed_at))
        status = 'stale; 429 cooldown' if payload.stale else 'shared cache'
        details.insert(0, f'Observed {observed} · {age}s old · {status}')
    return windows, details


# -- Anthropic extra accounts (opt-in, config-driven) -------------------------
# Claude Code relocates its whole config directory with ``CLAUDE_CONFIG_DIR``
# and keeps the OAuth login in ``<dir>/.credentials.json`` as
# ``{"claudeAiOauth": {"accessToken": ...}}``. That file is the one Claude
# Code's own CLI writes; it is read here, never written. Additional accounts
# are declared explicitly under ``claudeAccounts``; nothing is discovered by
# scanning the home directory or by reading any account that was not listed.


def _effective_claude_config_dir() -> Path:
    """The Claude config dir the current process already resolves to.

    Delegates to the installed Claude reader so ``CLAUDE_CONFIG_DIR`` and the
    platform default are honoured exactly as the rest of Hermes sees them.
    """
    try:
        from agent.anthropic_credentials import claude_code_credentials_path

        return claude_code_credentials_path().parent
    except Exception:  # noqa: BLE001 - standalone install / core not importable
        override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        return Path(override).expanduser() if override else Path.home() / ".claude"


def _canonical_dir(path: Path) -> str:
    """Stable key for "the same directory" — literal path, never printed."""
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        resolved = path.expanduser()
    return os.path.normcase(str(resolved))


def _token_fingerprint(token: Optional[str]) -> Optional[str]:
    """One-way fingerprint used only to dedupe identical credentials in memory."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _claude_accounts_setting() -> tuple[list[dict[str, Any]], bool]:
    """Validated ``claudeAccounts`` entries and whether the setting is present.

    ``configured`` is False when the setting is absent *or* an empty list — both
    keep the byte-identical single-account path. Present-but-malformed values
    become explicit ``config-invalid`` entries so the user sees an actionable
    card instead of a silent no-op. Parsing is bounded to
    ``_MAX_CLAUDE_ACCOUNTS`` entries plus one ``config-limit`` summary row, so a
    config with hundreds of items cannot flood the cache or the UI.
    """
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:  # noqa: BLE001 - standalone install / locked store
        return [], False
    plugins = config.get("plugins") if isinstance(config, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get("quota") if isinstance(entries, dict) else None
    settings = entry.get("settings") if isinstance(entry, dict) else None
    if not isinstance(settings, dict) or _CLAUDE_ACCOUNTS_SETTING not in settings:
        return [], False
    raw = settings.get(_CLAUDE_ACCOUNTS_SETTING)
    if raw is None:
        return [], True
    # ``hermes config set`` parses a JSON/YAML list literal into a real list
    # before writing it (verified against the installed CLI), but a value stored
    # by another writer may still be a JSON string: accept both, and reject a
    # string that is not valid JSON with an actionable message.
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return [], False
        try:
            raw = json.loads(text)
        except ValueError:
            return [{
                "error": "config-invalid",
                "detail": f"settings.{_CLAUDE_ACCOUNTS_SETTING} is not valid JSON.",
            }], True
    if not isinstance(raw, list):
        return [{
            "error": "config-invalid",
            "detail": f"settings.{_CLAUDE_ACCOUNTS_SETTING} must be a list of "
                      "{id, label, configDir} objects.",
        }], True
    if not raw:
        return [], False  # an empty list behaves exactly like an absent one

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    truncated = len(raw) > _MAX_CLAUDE_ACCOUNTS
    for index, item in enumerate(raw[:_MAX_CLAUDE_ACCOUNTS]):
        position = index + 1
        if not isinstance(item, dict):
            out.append({"index": position, "error": "config-invalid",
                        "detail": f"account #{position} is not an object; expected "
                                  "{id, label, configDir}."})
            continue
        label = item.get("label")
        if label is not None and (
            not isinstance(label, str) or len(label) > 80
            or any(ord(char) < 32 or ord(char) == 127 for char in label)
        ):
            out.append({"index": position, "error": "config-invalid",
                        "detail": f"account #{position} label must be at most 80 characters with no control characters."})
            continue
        account_id = item.get("id")
        config_dir = item.get("configDir", item.get("config_dir"))
        if not isinstance(account_id, str) or not account_id.strip():
            out.append({"index": position, "label": label if isinstance(label, str) else None,
                        "error": "config-invalid",
                        "detail": f"account #{position} needs a non-empty string 'id'."})
            continue
        account_id = account_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", account_id):
            out.append({"index": position, "label": label if isinstance(label, str) else None,
                        "error": "config-invalid",
                        "detail": f"account id for #{position} must start with an ASCII letter or digit and contain only ASCII letters, digits, '.', '_' or '-' (maximum {_CLAUDE_ACCOUNT_ID_MAX} characters)."})
            continue
        if account_id in seen_ids:
            out.append({"index": position, "label": label if isinstance(label, str) else None,
                        "error": "config-invalid",
                        "detail": f"duplicate claudeAccounts id '{account_id}'."})
            continue
        if not isinstance(config_dir, str) or not config_dir.strip():
            out.append({"index": position, "id": account_id, "label": label if isinstance(label, str) else None,
                        "error": "config-invalid",
                        "detail": f"account '{account_id}' needs a non-empty string 'configDir'."})
            continue
        seen_ids.add(account_id)
        out.append({
            "id": account_id,
            "label": label.strip() if isinstance(label, str) and label.strip() else account_id,
            "config_dir": config_dir.strip(),
        })
    if truncated:
        out.append({
            "index": _MAX_CLAUDE_ACCOUNTS + 1,
            "error": _CLAUDE_LIMIT_REASON,
            "detail": _ACCOUNT_LIMIT_DETAIL,
        })
    return out, True


def _read_claude_account_token(config_dir: str) -> tuple[Optional[str], Optional[str]]:
    """Read-only access token from a Claude config dir, or a failure reason.

    The path mirrors the installed Claude reader: ``<configDir>/.credentials.json``
    holding ``{"claudeAiOauth": {"accessToken": "..."}}``. That layout is
    implementation-derived from Claude Code's own reader, not a published
    credential spec, and only the Linux form is verified here.

    The file is opened once with ``O_NONBLOCK`` and the *descriptor* is fstat'd,
    so a symlink to a FIFO/device (or a file swapped in between a path check and
    the open) can neither block the worker thread nor be slurped without bound:
    only a regular file of at most ``_MAX_CREDENTIAL_BYTES`` is read. The token
    and the resolved path are never logged, returned in a reason, or cached. A
    missing file on macOS is ``unsupported-platform`` (the login there lives in
    a Keychain item this plugin cannot address per directory); elsewhere it is
    ``no-credentials``.
    """
    path = Path(config_dir).expanduser() / _CLAUDE_CREDENTIALS_FILENAME
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        if sys.platform == "darwin":
            return None, "unsupported-platform"
        return None, "no-credentials"
    except OSError:
        return None, "no-credentials"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CREDENTIAL_BYTES:
            return None, "no-credentials"
        chunks: list[bytes] = []
        remaining = _MAX_CREDENTIAL_BYTES
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks).decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None, "no-credentials"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "no-credentials"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if isinstance(token, str) and token.strip():
        return token.strip(), None
    return None, "no-credentials"


def _invalid_account(entry: dict[str, Any]) -> QuotaAccount:
    """An actionable ``config-invalid``/``config-limit`` row for a bad entry."""
    account_id = entry.get("id")
    if not isinstance(account_id, str) or not account_id.strip():
        account_id = f"{GENERATED_ACCOUNT_ID_PREFIX}{entry.get('index') or 'x'}"
    label = entry.get("label")
    if not isinstance(label, str) or not label.strip():
        position = entry.get("index")
        label = f"Account {position}" if position else account_id
    detail = entry.get("detail") if isinstance(entry.get("detail"), str) else "Invalid claudeAccounts entry."
    error = entry.get("error")
    reason = error if isinstance(error, str) and error else "config-invalid"
    return QuotaAccount(id=account_id.strip(), label=label.strip(),
                        unavailable_reason=reason, details=[detail])


def _credential_details(reason: Optional[str]) -> list[str]:
    if reason == "unsupported-platform":
        return [_MACOS_UNSUPPORTED_DETAIL]
    return []


def _claude_subscription_type(config_dir: str) -> Optional[str]:
    """Plan name from ``claudeAiOauth.subscriptionType``, read locally.

    Only this one string is taken from the credentials file; the token is never
    read here. Missing, non-string or oversized values give no plan.
    """
    try:
        path = Path(config_dir).expanduser() / _CLAUDE_CREDENTIALS_FILENAME
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > _MAX_CREDENTIAL_BYTES:
            return None
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - a missing plan is not an error
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    value = oauth.get("subscriptionType") if isinstance(oauth, dict) else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 32:
        return None
    return text.title()


def _claude_account_results(
    entries: list[dict[str, Any]],
    *,
    primary_token: Optional[str],
    deadline: Deadline,
) -> list[QuotaAccount]:
    """Fetch every configured extra account under one shared deadline.

    Identity is never inferred from a directory. A listed account is read even
    when it points at the same directory the core resolver uses, because the
    core token may come from ``ANTHROPIC_API_KEY``, the environment, or a
    different OAuth grant rather than that file. Dedup is exact: a listed
    directory seen twice, or an access token byte-identical to the primary's or
    to a previously listed account's, becomes one card. Accounts are never
    merged by organization or by an equal quota.

    The result is bounded: at most ``_MAX_CLAUDE_ACCOUNTS`` rows are produced,
    and when entries remain beyond the bound exactly one ``config-limit`` row is
    appended, so a huge or all-invalid config cannot flood the cache and UI.
    """
    accounts: list[QuotaAccount] = []
    # Only directories the user actually listed are collapsed together; the
    # primary's directory is deliberately absent so a listed account pointing at
    # it is still read (and deduped by token, not by path).
    seen_dirs: set[str] = set()
    seen_tokens: set[str] = set()
    primary_fp = _token_fingerprint(primary_token)
    if primary_fp:
        seen_tokens.add(primary_fp)
    fetched = 0
    overflow = False
    for entry in entries:
        if len(accounts) >= _MAX_CLAUDE_ACCOUNTS:
            overflow = True
            break
        if "error" in entry:
            accounts.append(_invalid_account(entry))
            continue
        if fetched >= _MAX_CLAUDE_ACCOUNTS:
            overflow = True
            break
        account_dir = Path(entry["config_dir"]).expanduser()
        canonical = _canonical_dir(account_dir)
        if canonical in seen_dirs:
            continue
        seen_dirs.add(canonical)
        fetched += 1
        if deadline.expired():
            accounts.append(QuotaAccount(id=entry["id"], label=entry["label"],
                                         unavailable_reason="timeout",
                                         details=[_ACCOUNT_TIMEOUT_DETAIL]))
            continue
        token, reason = _read_claude_account_token(str(account_dir))
        if not token:
            accounts.append(QuotaAccount(id=entry["id"], label=entry["label"],
                                         unavailable_reason=reason or "no-credentials",
                                         details=_credential_details(reason)))
            continue
        fingerprint = _token_fingerprint(token)
        if fingerprint in seen_tokens:
            continue
        seen_tokens.add(fingerprint)
        payload, reason = _request_anthropic_usage(
            token, timeout=deadline.slice(_ANTHROPIC_TIMEOUT_S))
        if payload is None:
            accounts.append(QuotaAccount(id=entry["id"], label=entry["label"],
                                         unavailable_reason=reason or "no-data"))
            continue
        windows, details = _parse_anthropic_usage(payload)
        if not windows and not details:
            accounts.append(QuotaAccount(id=entry["id"], label=entry["label"],
                                         unavailable_reason="no-data"))
            continue
        accounts.append(QuotaAccount(id=entry["id"], label=entry["label"],
                                     plan=_claude_subscription_type(str(account_dir)),
                                     windows=windows, details=details))
    if overflow:
        accounts.append(QuotaAccount(
            id=f"{GENERATED_ACCOUNT_ID_PREFIX}limit",
            label="Additional accounts",
            unavailable_reason=_CLAUDE_LIMIT_REASON,
            details=[_ACCOUNT_LIMIT_DETAIL]))
    return accounts


def _fetch_anthropic_single() -> QuotaResult:
    """Fetch and parse Anthropic's usage payload in one network request."""
    try:
        payload, reason = _anthropic_usage_payload()
    except ImportError:
        return build_unavailable("anthropic", "fetcher-unavailable")
    except Exception:
        return build_unavailable("anthropic", "fetch-error")
    if payload is None:
        return build_unavailable("anthropic", reason or "no-data")

    windows, details = _parse_anthropic_usage(payload)
    if not windows and not details:
        return build_unavailable("anthropic", "no-data")
    return QuotaResult(
        label="anthropic",
        windows=windows,
        plan=None,
        unavailable_reason=None,
        details=details,
    )


def _fetch_anthropic_multi(entries: list[dict[str, Any]]) -> QuotaResult:
    """Fetch the core account plus every configured extra account."""
    deadline = Deadline(_ANTHROPIC_MULTI_BUDGET_S)
    token = _core_anthropic_token()
    primary: Optional[QuotaResult] = None
    primary_reason: Optional[str] = None
    if not token:
        primary_reason = "no-credentials"
    elif _core_anthropic_is_oauth(token) is False:
        primary_reason = _ANTHROPIC_OAUTH_REQUIRED_REASON
    else:
        payload, reason = _request_anthropic_usage(
            token, timeout=deadline.slice(_ANTHROPIC_TIMEOUT_S))
        if payload is None:
            primary_reason = reason or "no-data"
        else:
            windows, details = _parse_anthropic_usage(payload)
            if windows or details:
                primary = QuotaResult(label="anthropic", windows=windows, details=details)
            else:
                primary_reason = "no-data"

    result = primary or build_unavailable("anthropic", primary_reason or "no-data")
    result.accounts = _claude_account_results(
        entries, primary_token=token, deadline=deadline)
    return result


def _fetch_anthropic() -> QuotaResult:
    """Anthropic usage: the core account, plus any configured extra accounts.

    With no ``claudeAccounts`` — or an empty list — this is exactly the
    historical single-account fetch: same request, same result, no accounts.
    """
    entries, configured = _claude_accounts_setting()
    if not configured or not entries:
        return _fetch_anthropic_single()
    return _fetch_anthropic_multi(entries)


_register("anthropic")(_fetch_anthropic)


# -- Nous Portal (direct account-info adapter) --------------------------------
# The core ``fetch_account_usage`` dispatcher does not route "nous" (only
# openai-codex / anthropic / openrouter), so the generic adapter above always
# produced ``no-data`` for it — for every account, paid or not. This fetcher
# reads the Portal account model directly (the same Hermes-managed OAuth state
# ``hermes portal status`` shows) and mirrors the semantics of the proposed
# ``portal usage --json`` contract (upstream hermes-agent PR #77791):
#   * a usage percentage only ever appears with a real positive denominator;
#   * free accounts render an honest status card (plan + free tool pool +
#     published rate ceiling) instead of fabricated zeros.


def _finite_usd(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nous_claim(account_info, name):
    claims = getattr(account_info, "raw_claims", None)
    return claims.get(name) if isinstance(claims, dict) else None


#: Display-safe claims from the local OAuth JWT snapshot. The old master card
#: read these directly; the fresh account-API snapshot does not carry them.
#: Entitlement claims are deliberately excluded: the Billing/account snapshot
#: owns plan and access state, and a stale paid claim must never override it.
_NOUS_MASTER_CLAIM_NAMES = (
    "rate_limit_rpm",
    "rate_limit_tpm",
    "rate_limit_rph",
    "member_spend_usd",
    "member_spend_cap_usd",
)


def _nous_master_claims(account_info) -> dict:
    """Only the old card's display-safe rate/spend claims, or an empty map."""
    claims = getattr(account_info, "raw_claims", None)
    if not isinstance(claims, dict):
        return {}
    return {
        name: claims[name]
        for name in _NOUS_MASTER_CLAIM_NAMES
        if name in claims
    }


def _nous_identity_text(value) -> Optional[str]:
    """A non-empty identity string, or ``None`` when the value proves nothing."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _nous_user_identities(account_info) -> dict[str, set[str]]:
    """Every display-safe user identifier this snapshot carries.

    The fresh account-API snapshot does not populate ``user_id`` on older
    cores, but its raw payload may still name the user (``user.id``), and both
    sides can carry a Privy DID or email. Bucketing by kind means an email is
    only compared with an email, a DID with a DID, and a subject with a
    subject; a kind that exists on both sides must intersect.
    """
    buckets: dict[str, set[str]] = {"user": set(), "privy": set(), "email": set()}

    def add(kind: str, value) -> None:
        text = _nous_identity_text(value)
        if text:
            buckets[kind].add(text)

    add("user", getattr(account_info, "user_id", None))
    add("privy", getattr(account_info, "privy_did", None))
    add("email", getattr(account_info, "email", None))

    raw_account = getattr(account_info, "raw_account", None)
    if isinstance(raw_account, dict):
        user = raw_account.get("user")
        if isinstance(user, dict):
            for name in ("id", "user_id", "sub"):
                add("user", user.get(name))
            add("privy", user.get("privy_did"))
            add("email", user.get("email"))
        for name in ("user_id", "sub"):
            add("user", raw_account.get(name))

    claims = getattr(account_info, "raw_claims", None)
    if isinstance(claims, dict):
        for name in ("sub", "user_id"):
            add("user", claims.get(name))
        add("privy", claims.get("privy_did"))
        add("email", claims.get("email"))
    return buckets


def _nous_org_identity(account_info) -> Optional[str]:
    """The portal organisation id, or ``None`` when this snapshot has none."""
    org = _nous_identity_text(getattr(account_info, "org_id", None))
    if org:
        return org
    raw_account = getattr(account_info, "raw_account", None)
    if isinstance(raw_account, dict):
        organisation = raw_account.get("organisation")
        if isinstance(organisation, dict):
            org = _nous_identity_text(organisation.get("id"))
        if not org:
            org = _nous_identity_text(raw_account.get("organisation_id"))
        if org:
            return org
    return _nous_identity_text(_nous_claim(account_info, "org_id"))


def _nous_portal_identity(account_info) -> Optional[str]:
    """The portal base URL, normalized only by its trailing slash."""
    portal = _nous_identity_text(getattr(account_info, "portal_base_url", None))
    if not portal:
        return None
    return portal.rstrip("/")


def _nous_org_match_key(value) -> Optional[str]:
    """A pseudonymous, local organisation digest for the Desktop cache merge.

    `billing.state` exposes `org_slug` but no signed-in user id. Hashing the
    slug to an unkeyed FNV-1a 64-bit hex digest avoids persisting the raw slug
    in the quota cache. It is not a secret, not authentication, and not a
    cryptographic identifier: an organisation slug is low-entropy, so anyone
    holding it can recompute this digest. It is only a pseudonymous matching
    aid, and it cannot distinguish two users who share the same organisation.
    """
    text = _nous_identity_text(value)
    if not text:
        return None
    key = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        key ^= byte
        key = (key * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{key:016x}"


def _nous_plan_identity(account_info) -> Optional[tuple[Optional[str], Optional[int]]]:
    """A provable plan identity, or ``None`` when this snapshot cannot name one.

    The name and the numeric tier are compared independently: two snapshots
    that both carry names must agree on the name, and two that both carry tiers
    must agree on the tier. A name on one side and only a tier on the other is
    not proof. ``account_tier`` is used for the free tier (where it is the plan
    identity) but never as a generic paid identity, because ``"paid"`` does not
    say which paid plan.
    """
    sub = getattr(account_info, "subscription", None)
    plan = _nous_subscription_plan(sub)
    name = " ".join(plan.split()).casefold() if plan else None
    tier_tag = _nous_account_tier(account_info)
    if name is None and tier_tag in _NOUS_FREE_TIER_NAMES:
        name = "free"
    access = getattr(account_info, "paid_service_access_info", None)
    tier = _nous_tier_claim(account_info, access)
    if tier is None:
        tier = _nous_int(getattr(sub, "tier", None))
    if tier is not None and tier <= 0:
        tier = None
    if name is None and tier is None:
        return None
    return name, tier


def _nous_same_plan(cached, fresh) -> bool:
    """Whether two snapshots prove the same plan for their JWT claims.

    Claim-level facts such as rate limits and period spend are plan-specific:
    a cached JWT from an older paid tier must not decorate a newer plan just
    because both snapshots are paid. Missing or only partially comparable
    identities fail closed.
    """
    left = _nous_plan_identity(cached)
    right = _nous_plan_identity(fresh)
    if left is None or right is None:
        return False
    left_name, left_tier = left
    right_name, right_tier = right
    if left_name is not None and right_name is not None and left_name != right_name:
        return False
    if left_tier is not None and right_tier is not None and left_tier != right_tier:
        return False
    return (
        (left_name is not None and right_name is not None)
        or (left_tier is not None and right_tier is not None)
    )


def _nous_same_account(cached, fresh) -> bool:
    """Whether two snapshots prove they describe the same Portal account.

    Fail-closed: portal and organisation ids must both be present and equal,
    and at least one user identifier kind (subject, Privy DID, email) must be
    present on both sides and intersect. A missing identifier is not a match,
    and one disagreeing shared kind vetoes an otherwise matching one.
    """
    try:
        cached_portal = _nous_portal_identity(cached)
        fresh_portal = _nous_portal_identity(fresh)
        if not cached_portal or not fresh_portal or cached_portal != fresh_portal:
            return False
        cached_org = _nous_org_identity(cached)
        fresh_org = _nous_org_identity(fresh)
        if not cached_org or not fresh_org or cached_org != fresh_org:
            return False
        cached_users = _nous_user_identities(cached)
        fresh_users = _nous_user_identities(fresh)
        matched = False
        for kind, cached_values in cached_users.items():
            fresh_values = fresh_users.get(kind) or set()
            if not cached_values or not fresh_values:
                continue
            if not (cached_values & fresh_values):
                return False
            matched = True
        return matched
    except Exception:
        return False


def _nous_snapshot_is_free(account_info) -> bool:
    """Whether a snapshot's own facts prove a free-tier account."""
    sub = getattr(account_info, "subscription", None)
    if _nous_subscription_is_empty(sub):
        sub = None
    access = getattr(account_info, "paid_service_access_info", None)
    paid = getattr(account_info, "paid_service_access", None)
    evidence = _nous_has_paid_plan_evidence(access, sub, paid)
    return _nous_free_tier_evidence(account_info, access, sub, paid, evidence)


def _nous_snapshot_is_free_plan(account_info) -> bool:
    """Whether the snapshot's own plan identity explicitly names Free."""
    try:
        identity = _nous_plan_identity(account_info)
    except Exception:
        return False
    return identity is not None and identity[0] == "free"


def _nous_snapshot_may_read_free(account_info) -> bool:
    """Whether a cached snapshot is consistent with a fresh Free reading.

    A JWT minted for a Free plan that holds purchased credits carries
    ``paid_access: true``, but core's JWT path has no plan name and cannot
    populate the purchased-credit fields, so ``_nous_snapshot_reads_free``
    cannot prove that shape. Requiring proof there dropped the live JWT's
    rate-limit claims and an enabled tool pool.

    Only the unprovable shape is accepted: a JWT with ``paid_access: true``
    and no paid plan name, paid account tier, subscription, or paid
    subscription evidence. Anything that actually names or corroborates a paid
    plan still fails closed.
    """
    if _nous_snapshot_reads_free(account_info):
        return True
    if getattr(account_info, "source", None) != "jwt":
        return False
    if getattr(account_info, "paid_service_access", None) is not True:
        return False
    identity = _nous_plan_identity(account_info)
    if identity is not None and identity[0] not in (None, "free"):
        return False
    if _nous_account_tier(account_info) in _NOUS_PAID_TIER_NAMES:
        return False
    sub = getattr(account_info, "subscription", None)
    if _nous_subscription_is_empty(sub):
        sub = None
    access = getattr(account_info, "paid_service_access_info", None)
    return not _nous_has_paid_subscription_evidence(access, sub)


def _nous_snapshot_reads_free(account_info) -> bool:
    """The free/paid reading used to match cached JWT facts.

    A JWT minted for a Free plan holding purchased credits can carry
    ``paid_access: true``; core's JWT path does not populate the purchased
    credit fields, so ``_nous_snapshot_is_free`` cannot corroborate that shape
    and reads it as non-free. An explicit Free plan identity is the remaining
    proof, and a genuine paid plan fails it. Everything else keeps the existing
    free-tier evidence rule.
    """
    if _nous_snapshot_is_free(account_info):
        return True
    if (
        getattr(account_info, "source", None) != "jwt"
        or getattr(account_info, "paid_service_access", None) is not True
    ):
        return False
    access = getattr(account_info, "paid_service_access_info", None)
    sub = getattr(account_info, "subscription", None)
    if _nous_has_paid_subscription_evidence(access, sub):
        return False
    return _nous_snapshot_is_free_plan(account_info)


def _nous_with_master_claims(account_info, claims: dict):
    """Return the account snapshot with independent JWT facts attached.

    The fresh account API omits ``raw_claims`` entirely, so its card lost the
    RPM/TPM/RPH and spend details the old JWT-backed method showed. The claims
    are attached to a copy (never the caller's object) and only the whitelisted
    display facts above cross over; no token, identity or entitlement field is
    copied into the cache.
    """
    if not claims:
        return account_info
    existing = getattr(account_info, "raw_claims", None)
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(claims)
    try:
        import dataclasses

        return dataclasses.replace(account_info, raw_claims=merged)
    except Exception:
        # Test doubles and older core snapshots may be simple objects rather
        # than dataclasses; copy before mutating so the source stays pristine.
        try:
            import copy

            clone = copy.copy(account_info)
            setattr(clone, "raw_claims", merged)
            return clone
        except Exception:
            return account_info


def _nous_with_tool_access(account_info, tool_access):
    """Return the account snapshot with a proven cached tool pool attached.

    ``NousPortalAccountInfo`` is frozen, so the same copy-on-write approach as
    ``_nous_with_master_claims`` applies; test doubles and older snapshots fall
    back to a shallow copy.
    """
    try:
        import dataclasses

        return dataclasses.replace(account_info, tool_access=tool_access)
    except Exception:
        try:
            import copy

            clone = copy.copy(account_info)
            setattr(clone, "tool_access", tool_access)
            return clone
        except Exception:
            return account_info


class _NousToolAccess:
    """Minimal ``tool_access`` shape for a raw-claim-only snapshot."""

    __slots__ = ("enabled", "coverage")

    def __init__(self, *, enabled: bool, coverage: dict[str, bool]) -> None:
        self.enabled = enabled
        self.coverage = coverage


def _nous_tool_access_claim(account_info):
    """The snapshot's parsed ``tool_access`` entitlement, or ``None``.

    Core normally supplies a normalized ``tool_access`` object from either the
    JWT claim or the account payload. An older snapshot may carry only the raw
    claim dict, so parse that with the same fail-closed rule as core: only a
    literal ``true`` enables the pool or a coverage category.
    """
    value = getattr(account_info, "tool_access", None)
    if value is not None:
        return value
    raw = _nous_claim(account_info, "tool_access")
    if not isinstance(raw, dict):
        return None
    coverage = raw.get("coverage")
    return _NousToolAccess(
        enabled=raw.get("enabled") is True,
        coverage={
            key: flag is True
            for key, flag in coverage.items()
            if isinstance(key, str)
        } if isinstance(coverage, dict) else {},
    )


def _format_rate_limit(value) -> Optional[str]:
    number = _finite_usd(value)
    if number is None or number < 0:
        return None
    if number >= 1_000_000:
        return f"{number / 1_000_000:g}M"
    if number >= 1_000:
        return f"{number / 1_000:g}k"
    return f"{number:g}"


#: Published API-key rate limits from the official Nous Portal API docs
#: (https://portal.nousresearch.com/api-docs, retrieved 2026-11-06). Only
#: RPM and TPM are published; there is no published requests-per-hour ceiling,
#: so RPH is never invented for a fallback. Live account claims always take
#: precedence and include RPH when the account supplies it.
_NOUS_PUBLISHED_RATE_LIMITS: dict[str, tuple[str, int, int]] = {
    "free": ("Free", 50, 500_000),
    "basic": ("Basic", 400, 2_000_000),
    "plus": ("Plus", 400, 4_000_000),
    "super": ("Super", 800, 8_000_000),
    "scale": ("Scale", 600, 6_000_000),
    "ultra": ("Ultra", 1_600, 16_000_000),
    "default paid users": ("Default paid users", 180, 720_000),
}


def _nous_has_rate_claim(account_info) -> bool:
    """Whether the account snapshot carries any usable live rate-limit claim.

    A key with a null/blank value is absence, not a claim; a key with any
    other value is an actual account fact and must win over a published
    fallback even if that value later fails formatting. This prevents a
    malformed claim from being silently replaced by a different published
    ceiling.
    """
    claims = getattr(account_info, "raw_claims", None)
    if not isinstance(claims, dict):
        return False
    for name in ("rate_limit_rpm", "rate_limit_tpm", "rate_limit_rph"):
        if name not in claims:
            continue
        value = claims[name]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def _nous_published_plan_key(plan: Optional[str]) -> Optional[str]:
    """Match a live plan label to one documented published tier.

    The account API names paid plans with a product prefix ("Hermes Plus")
    while the docs table uses the bare tier ("Plus"). Stripping that prefix
    is an exact tier mapping, not a generic paid-tier guess; an unknown
    suffix (or a numeric tier) still has no entry.
    """
    if not plan:
        return None
    key = " ".join(plan.split()).casefold()
    if key in _NOUS_PUBLISHED_RATE_LIMITS:
        return key
    prefix = "hermes "
    if key.startswith(prefix):
        bare = key[len(prefix):]
        if bare in _NOUS_PUBLISHED_RATE_LIMITS:
            return bare
    return None


def _nous_published_rate_limits(account_info, state, sub) -> Optional[tuple[str, int, int]]:
    """The documented ceiling for an unambiguous tier, or ``None``.

    This is a fallback only: any live rate-limit claim wins. The tier identity
    must be exact — the subscription's own plan label (or a proven Free state
    when that label is absent). Numeric tiers and generic ``paid``/``paid_access``
    flags are deliberately not normalized into a plan, because they cannot
    distinguish Plus, Super, Scale, Ultra, Basic or the default paid ceiling.
    A product-prefixed account label ("Hermes Plus") maps to the same bare
    published tier; an unknown label still yields no fallback.

    ``unknown`` is accepted only because a low balance maps to it on Desktop
    while the plan label itself is still exact. The published ceiling is a
    property of that documented plan, not a claim that the account is currently
    usable; a missing or non-exact label still yields no fallback.
    """
    if state not in ("free", "active", "depleted", "paused", "unknown"):
        return None
    if _nous_has_rate_claim(account_info):
        return None
    plan = _nous_subscription_plan(sub)
    key = _nous_published_plan_key(plan)
    if key is None and state == "free":
        key = "free"
    if key is None:
        return None
    entry = _NOUS_PUBLISHED_RATE_LIMITS.get(key)
    if entry is None:
        return None
    # Fail closed across the free/paid boundary: a Free account must not wear a
    # paid tier's published ceiling, and a paid plan must not inherit Free's.
    if state == "free" and key != "free":
        return None
    if state != "free" and key == "free":
        return None
    return entry


def _nous_tool_pool_labels(account_info) -> list[str]:
    """Names of the free Tool-Gateway categories this account may use.

    ``tool_access.enabled`` is the Portal's live entitlement switch: a positive
    pool balance that is not gated off. Coverage entries alone are not proof the
    pool is usable, so a missing/False switch keeps the whole line off.
    """
    try:
        import dataclasses

        tool_access = _nous_tool_access_claim(account_info)
        if getattr(tool_access, "enabled", None) is not True:
            return []
        coverage = getattr(tool_access, "coverage", None)
        if coverage is None:
            return []
        if isinstance(coverage, dict):
            items = list(coverage.items())
        elif dataclasses.is_dataclass(coverage):
            items = [(f.name, getattr(coverage, f.name)) for f in dataclasses.fields(coverage)]
        else:
            return []
        pretty = {"browser_use": "browser-use", "fal_video": "fal-video", "openai_audio": "openai-audio"}
        return sorted(pretty.get(str(k), str(k)) for k, v in items if v is True)
    except Exception:
        return []


#: Display names for the usage-window gauges the Portal reports. The server
#: sends stable ids plus its own title case ("Today", "This week", "This
#: month", "Large models (monthly)"); the quota card uses the compact, exact
#: names below for the ids it knows. An unknown id keeps the server's own
#: label.
_NOUS_USAGE_WINDOW_LABELS = {
    "day": "Daily",
    "week": "Weekly",
    "month": "Monthly",
    "top_tier": "Large models (Monthly)",
}


def _nous_window_label(gauge) -> Optional[str]:
    """One display label for a live usage-window gauge, or None when unusable."""
    gauge_id = getattr(gauge, "id", None)
    if not isinstance(gauge_id, str) or not gauge_id.strip():
        return None
    gauge_id = gauge_id.strip()
    known = _NOUS_USAGE_WINDOW_LABELS.get(gauge_id)
    if known is not None:
        return known
    raw = getattr(gauge, "label", None)
    text = raw.strip() if isinstance(raw, str) and raw.strip() else gauge_id
    # Provider-authored fallback text: never let control or bidi characters
    # reach a terminal/widget row, and keep a malformed label bounded.
    text = re.sub(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", text)
    text = text.strip()
    return text[:80] or None


def _nous_paused_reason(account_info) -> Optional[str]:
    """The usage-window plan's own pause fact, when core supplied one.

    Core's ``NousUsageWindowInfo.paused_reason`` is set while a usage-window
    plan's renewal is unpaid. It is the authoritative pause signal; the older
    ``member_spend_cap_exceeded`` flag on the access block is a different
    pause reason and neither field can stand in for the other.
    """
    window = getattr(account_info, "usage_window", None) if account_info is not None else None
    reason = getattr(window, "paused_reason", None) if window is not None else None
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return None


def _nous_usage_window_exhausted(window) -> Optional[bool]:
    """Whether a live usage-window plan's included allowance is fully spent.

    Only ``scope == "all"`` gauges gate every paid model; a full ``top_tier``
    gauge blocks that model class, not paid calls generally. ``True`` means at
    least one general gauge is full, ``False`` means usable gauges exist and
    none is full, and ``None`` means the snapshot proves neither (no usable
    general gauge).
    """
    gauges = getattr(window, "gauges", None) if window is not None else None
    if not isinstance(gauges, (list, tuple)) or not gauges:
        return None
    seen = False
    exhausted = False
    for gauge in gauges:
        if getattr(gauge, "scope", None) != "all":
            continue
        used_bp = getattr(gauge, "used_bp", None)
        if isinstance(used_bp, bool) or not isinstance(used_bp, (int, float)):
            continue
        if not math.isfinite(float(used_bp)) or not 0 <= used_bp <= 10_000:
            continue
        seen = True
        if used_bp >= 10_000:
            exhausted = True
    return exhausted if seen else None


def _nous_credit_continuation(access) -> Optional[bool]:
    """Whether purchased credits can carry calls past the included allowance.

    Only the purchased wallet is spendable after a pause or an exhausted
    included window; included subscription credits are exactly what stopped.
    ``total_usable_credits`` sums those withheld credits in, so it can never
    prove continuation. An explicit zero wallet proves a stop; a missing
    wallet stays unknown rather than assumed.
    """
    purchased = _nous_credit_value(access, "purchased_credits_remaining")
    if purchased is None:
        return None
    return purchased > 0


def _nous_usage_window_windows(account_info) -> list[QuotaWindow]:
    """Live usage-window allowances as quota windows, in the server's order."""
    from datetime import datetime, timezone

    if _nous_paused_reason(account_info) is not None:
        # A paused plan withholds its included allowance; stale gauges are not
        # remaining quota and must never be ranked.
        return []
    window = getattr(account_info, "usage_window", None)
    gauges = getattr(window, "gauges", None) if window is not None else None
    if not gauges:
        return []
    windows: list[QuotaWindow] = []
    for gauge in gauges:
        label = _nous_window_label(gauge)
        if label is None:
            continue
        used_bp = getattr(gauge, "used_bp", None)
        if isinstance(used_bp, bool) or not isinstance(used_bp, (int, float)):
            continue
        used = max(0.0, min(100.0, float(used_bp) / 100.0))
        reset_at = None
        stamp = getattr(gauge, "resets_at_ms", None)
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            try:
                reset_at = datetime.fromtimestamp(float(stamp) / 1000.0, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                reset_at = None
        windows.append(QuotaWindow(label=label, used_percent=used, reset_at=reset_at))
    return windows


#: ``NousPortalAccountInfo.account_tier`` values the core uses for the free
#: tier. ``anonymous`` is the guest identity; ``free`` is a named free account.
_NOUS_FREE_TIER_NAMES = frozenset({"free", "anonymous"})
#: Account-tier claims that contradict a free-tier reading. They are evidence
#: that the account is (or was) paid, not evidence that it is currently usable.
_NOUS_PAID_TIER_NAMES = frozenset({"paid", "pro"})
#: Denial reasons that mean the entitlement could not be resolved or has
#: lapsed. They must not be rendered as credit exhaustion even when a stale
#: subscription object happens to show a paid plan.
_NOUS_UNAVAILABLE_REASONS = frozenset({"account_missing", "subscription_expired"})


def _nous_account_tier(account_info) -> Optional[str]:
    """The portal's account-tier claim, normalized, or ``None`` when absent.

    ``NousPortalAccountInfo.account_tier`` is normalized by core from the JWT
    or the account payload. Older snapshots may only carry the raw claim, so
    fall back to it without treating an absent claim as "free".
    """
    value = getattr(account_info, "account_tier", None)
    if not isinstance(value, str) or not value.strip():
        value = _nous_claim(account_info, "account_tier")
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _nous_int(value) -> Optional[int]:
    number = _finite_usd(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _nous_subscription_plan(sub) -> Optional[str]:
    """The subscription's own plan name, never a synthesized tier label."""
    plan = getattr(sub, "plan", None) if sub is not None else None
    if isinstance(plan, str) and plan.strip():
        return plan.strip()
    return None


def _nous_subscription_is_empty(sub) -> bool:
    """Whether a subscription object carries no fact at all.

    The portal serializes an absent subscription as ``{}``, but padding those
    fields with zeroes is just as empty: a zero tier/charge/grant/remaining
    balance is not a plan, and treating it as one used to suppress the free-tier
    classification (the snapshot looked like it had a subscription only because
    every number was 0). A positive grant, charge, rollover or balance remains a
    fact, as does a real plan name, a positive tier, or a period end.
    """
    if sub is None:
        return True
    if _nous_subscription_plan(sub) is not None:
        return False
    tier = _nous_int(getattr(sub, "tier", None))
    if tier is not None and tier > 0:
        return False
    if getattr(sub, "current_period_end", None):
        return False
    for attr in (
        "monthly_charge",
        "monthly_credits",
        "credits_remaining",
        "rollover_credits",
    ):
        value = _finite_usd(getattr(sub, attr, None))
        if value is not None and value > 0:
            return False
    return True


def _nous_tier_claim(account_info, access) -> Optional[int]:
    """The subscription-tier number from the normalized access record or JWT.

    A tier number is a plan label, not proof of paid entitlement: a live free
    JWT carries ``subscription_tier`` 5. Callers must gate it on paid evidence.
    """
    tier = _nous_int(getattr(access, "subscription_tier", None)) if access is not None else None
    if tier is None:
        tier = _nous_int(_nous_claim(account_info, "subscription_tier"))
    return tier


def _nous_credit_value(access, name) -> Optional[float]:
    return _finite_usd(getattr(access, name, None)) if access is not None else None


def _nous_spend_value(account_info, access, name) -> Optional[float]:
    """A period-spend/cap fact, preferring the fresh account snapshot.

    The account-API access block carries ``member_spend_usd`` and
    ``member_spend_cap_usd`` directly; a cached JWT carries them as raw claims.
    Reading only the claims dropped the fresh snapshot's own values whenever
    no JWT was available, so the access record wins and the claims are fallback.
    """
    value = _nous_credit_value(access, name)
    if value is None:
        value = _finite_usd(_nous_claim(account_info, name))
    return value


def _nous_has_positive_credit(access) -> bool:
    for name in (
        "subscription_credits_remaining",
        "purchased_credits_remaining",
        "total_usable_credits",
    ):
        value = _nous_credit_value(access, name)
        if value is not None and value > 0:
            return True
    return False


def _nous_has_purchased_credit(access) -> bool:
    value = _nous_credit_value(access, "purchased_credits_remaining")
    return value is not None and value > 0


def _nous_has_paid_plan_evidence(access, sub, paid) -> bool:
    """Whether the snapshot carries actual evidence of a paid plan or credits.

    ``paid_service_access is False`` alone is deliberately not evidence: core
    uses that flag for both "not a paid plan" and "paid plan with no usable
    credits" (see ``format_nous_portal_entitlement_message``). Subscription
    facts must corroborate it.
    """
    if paid is True:
        return True
    if access is not None:
        paid_sub = getattr(access, "active_subscription_is_paid", None)
        charge = _nous_credit_value(access, "subscription_monthly_charge")
        if paid_sub is True:
            return True
        if paid_sub is False:
            # The portal explicitly says this subscription is not paid. Only
            # purchased credits can still constitute paid-service evidence.
            return _nous_has_purchased_credit(access)
        if getattr(access, "has_active_subscription", None) is True and _nous_has_positive_credit(access):
            return True
        if charge is not None and charge > 0:
            return True
    if _nous_has_positive_credit(access):
        return True
    if sub is not None:
        monthly_charge = _finite_usd(getattr(sub, "monthly_charge", None))
        monthly_credits = _finite_usd(getattr(sub, "monthly_credits", None))
        plan = _nous_subscription_plan(sub)
        if (monthly_charge is not None and monthly_charge > 0) or (
            monthly_credits is not None and monthly_credits > 0
        ):
            return True
        if plan is not None and plan.lower() not in _NOUS_FREE_TIER_NAMES:
            return True
    return False


def _nous_has_active_paid_subscription(access, sub) -> bool:
    """Whether an active paid subscription is corroborated by the snapshot.

    This mirrors the core's ``has_active_subscription`` +
    ``active_subscription_is_paid`` pair (and a positive subscription charge),
    not the ambiguous ``paid_service_access`` denial. An explicit
    ``active_subscription_is_paid is False`` is authoritative: it is not paid.
    """
    if access is not None:
        active = getattr(access, "has_active_subscription", None) is True
        paid_sub = getattr(access, "active_subscription_is_paid", None)
        charge = _nous_credit_value(access, "subscription_monthly_charge")
        if active and paid_sub is True:
            return True
        if active and paid_sub is None and charge is not None and charge > 0:
            return True
    if sub is not None:
        charge = _finite_usd(getattr(sub, "monthly_charge", None))
        if charge is not None and charge > 0:
            return True
    return False


def _nous_has_positive_subscription_credit(access) -> bool:
    value = _nous_credit_value(access, "subscription_credits_remaining")
    return value is not None and value > 0


#: The portal's credit totals are USD. A corrupt or absurd value must not
#: allocate an enormous formatted string or claim an impossible balance.
_NOUS_BALANCE_MAX_USD = 1e15


def _nous_total_usable(access, state: str) -> Optional[float]:
    """The account's total usable credit figure, only when the source proves it.

    ``total_usable_credits`` already includes both the included subscription
    credits and the purchased wallet, so it is rendered as-is and never summed
    with either component. A missing total may fall back to the purchased
    wallet only when the snapshot explicitly reports zero subscription credits
    (the wallet is then the whole usable balance); an unknown subscription
    component is not a supported total. Zero is a known amount and must remain
    visible. States whose snapshot cannot prove a current amount
    (unknown/unavailable) contribute nothing.

    A paused plan withholds its included allowance, yet the portal's total
    still counts it. Only the purchased wallet is spendable there, and only
    when the snapshot reports it: the paused total is not a balance.
    """
    if access is None or state in ("unknown", "unavailable"):
        return None

    def _valid(amount: Optional[float]) -> Optional[float]:
        if amount is None or amount < 0 or amount > _NOUS_BALANCE_MAX_USD:
            return None
        return amount

    if state == "paused":
        return _valid(_finite_usd(getattr(access, "purchased_credits_remaining", None)))
    amount = _valid(_finite_usd(getattr(access, "total_usable_credits", None)))
    if amount is not None:
        return amount
    subscription = _finite_usd(getattr(access, "subscription_credits_remaining", None))
    purchased = _valid(_finite_usd(getattr(access, "purchased_credits_remaining", None)))
    if subscription == 0 and purchased is not None:
        return purchased
    return None


def _nous_has_paid_subscription_evidence(access, sub) -> bool:
    """Whether the snapshot proves a paid *subscription*, not just paid calls.

    ``paid_service_access`` can be true because an otherwise free account holds
    purchased credits. Subscription facts have to corroborate a paid plan: an
    active paid subscription, a positive monthly charge, positive subscription
    credits, positive monthly credits, or a non-Free plan name.
    """
    if _nous_has_active_paid_subscription(access, sub):
        return True
    if access is not None:
        if getattr(access, "active_subscription_is_paid", None) is True:
            return True
        charge = _nous_credit_value(access, "subscription_monthly_charge")
        if charge is not None and charge > 0:
            return True
        if _nous_has_positive_subscription_credit(access):
            return True
    if sub is not None:
        charge = _finite_usd(getattr(sub, "monthly_charge", None))
        monthly = _finite_usd(getattr(sub, "monthly_credits", None))
        plan = _nous_subscription_plan(sub)
        if (charge is not None and charge > 0) or (monthly is not None and monthly > 0):
            return True
        if plan is not None and plan.lower() not in _NOUS_FREE_TIER_NAMES:
            return True
    return False


def _nous_credits_exhausted(access, sub) -> Optional[bool]:
    """True only when the snapshot explicitly reports zero usable credits.

    Missing fields stay unknown: an absent balance is not proof of depletion.
    """
    values: list[float] = []
    if access is not None:
        for name in (
            "subscription_credits_remaining",
            "purchased_credits_remaining",
            "total_usable_credits",
        ):
            value = _nous_credit_value(access, name)
            if value is not None:
                values.append(value)
    if sub is not None:
        value = _finite_usd(getattr(sub, "credits_remaining", None))
        if value is not None:
            values.append(value)
    if not values:
        return None
    return all(value <= 0 for value in values)


def _nous_free_tier_evidence(account_info, access, sub, paid, paid_plan_evidence) -> bool:
    """Whether this snapshot says free tier, rather than inferring it from a denial.

    An explicit free plan or free ``account_tier`` is evidence, but a paid
    *subscription* fact vetoes it: a paid account can carry a stale free tier
    claim. Conversely, ``paid_service_access: true`` alone does not make a
    free-plan account paid — purchased credits on a Free plan also produce it.
    With ``paid_service_access: true`` the free tier is only accepted when the
    account names itself free and the credits are purchased (not included-plan
    subscription credits). A JWT snapshot has only claims, so ``account_tier``
    is the reliable free signal there. A richer account snapshot also carries
    explicit subscription facts; only an explicit ``has_active_subscription is
    False`` (never a missing field) counts as "no paid plan".
    """
    plan = _nous_subscription_plan(sub)
    if _nous_has_paid_subscription_evidence(access, sub):
        return False
    if plan is not None and plan.lower() == "free":
        if paid is True:
            return (
                _nous_has_purchased_credit(access)
                and not _nous_has_positive_subscription_credit(access)
            )
        return True
    tier = _nous_account_tier(account_info)
    if tier in _NOUS_FREE_TIER_NAMES:
        if paid is True:
            # ``paid_service_access`` is true for a Free plan holding purchased
            # credits. Only that shape is accepted; included-plan subscription
            # credits would contradict a free plan.
            return (
                _nous_has_purchased_credit(access)
                and not _nous_has_positive_subscription_credit(access)
            )
        return True
    if getattr(account_info, "is_anonymous_tier", False) is True:
        return True
    # An explicit "no active subscription" is the account API's own answer and
    # outranks the aggregate ``paid_service_access`` flag and a bare paid tier
    # claim. That flag is also true for a Free plan whose purchased credits
    # fund calls; treating it as paid here classified a Free+top-up account as
    # an active paid plan and rendered its padded renewal date. Only the
    # explicit False counts, and only when the subscription is not paid; all
    # paid-subscription evidence (plan name, charge, included credits) was
    # already checked above.
    if access is not None:
        if (
            getattr(access, "has_active_subscription", None) is False
            and getattr(access, "active_subscription_is_paid", None) is not True
        ):
            return True
    if tier in _NOUS_PAID_TIER_NAMES:
        return False
    if paid is True:
        return False
    if paid_plan_evidence:
        return False
    return False


def _nous_access_state(account_info, access, sub, paid, paid_plan_evidence) -> str:
    """Classify the snapshot as free, active, depleted, paused, unavailable or unknown."""
    # The usage-window plan's own pause reason is authoritative. Core withholds
    # the included allowance while it is set, so neither `paid_service_access`,
    # the access reason, nor the older member-spend-cap flag may turn this into
    # active, free or unavailable. A failed or stale lookup is converted to
    # `unknown` by the caller after this classification, so only a fresh
    # snapshot reaches this branch.
    if _nous_paused_reason(account_info) is not None:
        return "paused"
    reason = getattr(access, "reason", None) if access is not None else None
    if reason in _NOUS_UNAVAILABLE_REASONS:
        # The lookup could not resolve an account or the subscription lapsed.
        # That authoritative denial outranks any stale paid flag still carried
        # by the snapshot.
        return "unavailable"
    if _nous_free_tier_evidence(account_info, access, sub, paid, paid_plan_evidence):
        return "free"
    if access is not None and getattr(access, "member_spend_cap_exceeded", None) is True:
        return "paused"
    if paid is True:
        return "active"
    if paid_plan_evidence:
        exhausted = _nous_credits_exhausted(access, sub)
        explicitly_unpaid = (
            access is not None
            and getattr(access, "active_subscription_is_paid", None) is False
        )
        if exhausted is True and not explicitly_unpaid and (paid is False or reason == "no_usable_credits"):
            return "depleted"
        if (
            paid is False
            and not explicitly_unpaid
            and _nous_has_active_paid_subscription(access, sub)
            and exhausted is not False
        ):
            # Core's entitlement copy treats an active paid subscription whose
            # access was denied as credit exhaustion. Explicit positive credits
            # contradict that, so only then fall through to "unavailable".
            return "depleted"
        if paid is False:
            # A paid plan exists but the portal did not grant access and the
            # snapshot does not prove the credits are gone.
            return "unavailable"
        if exhausted is False:
            return "active"
        return "unknown"
    return "unknown"


def _nous_api_calls_available(
    state,
    access,
    sub,
    paid,
    total_usable=None,
    usage_window=None,
) -> Optional[bool]:
    """Structured availability, only where the snapshot proves it.

    A free snapshot with a positive total-usable balance is available through
    purchased credits: the balance funds calls even though the plan stays Free
    and no subscription exists. A plain free account with no credits stays
    ``None`` (muted): free-model access is not evidence that paid API calls are
    available.

    A usage-window plan carries the authoritative included allowance in its
    gauges. Once an all-scope gauge is full, calls continue only when
    ``keep_going_on_credits`` allows the purchased wallet to pay, and only if
    that purchased balance is explicitly positive. A paused plan is likewise
    governed by that switch plus the purchased balance; an absent wallet answer
    is unknown, not a promise or a denial. The portal's total-usable figure is
    never evidence here: it still counts the withheld included credits.
    """
    keep_going = getattr(usage_window, "keep_going_on_credits", None) if usage_window is not None else None
    if not isinstance(keep_going, bool):
        keep_going = None

    if state == "depleted":
        return False
    if state == "paused":
        if keep_going is False:
            return False
        if keep_going is True:
            return _nous_credit_continuation(access)
        return None
    if state == "free":
        # Positive purchased credits fund calls even if the portal omitted the
        # total field. When the total is present it is authoritative: a zero
        # total cannot coexist with a "purchased credits are available" claim.
        if total_usable is not None:
            return True if total_usable > 0 else None
        if _nous_has_purchased_credit(access):
            return True
        return None
    if state != "active":
        return None
    # The live allowance outranks a bare paid flag. `paid_service_access` can
    # stay true while every included gauge is full; claiming calls available
    # there was the false "API calls available: yes".
    exhausted = _nous_usage_window_exhausted(usage_window)
    if exhausted is True:
        if keep_going is False:
            # The included allowance is spent and the account is configured not
            # to spend purchased credits past it: calls stop.
            return False
        if keep_going is True:
            return _nous_credit_continuation(access)
        # Included allowance spent, continuation unproven.
        return None
    if exhausted is False:
        # A live all-scope gauge with headroom proves the included allowance is
        # still usable, even though `usage.available` is false for these plans.
        return True
    if usage_window is not None and getattr(usage_window, "gauges", None):
        # Gauges exist but none is a usable all-scope gauge (for example only a
        # top-tier meter): this snapshot cannot prove general call availability.
        return None
    if paid is True:
        return True
    if _nous_has_positive_credit(access):
        return True
    if sub is not None:
        remaining = _finite_usd(getattr(sub, "credits_remaining", None))
        monthly = _finite_usd(getattr(sub, "monthly_credits", None))
        if (remaining is not None and remaining > 0) or (monthly is not None and monthly > 0):
            return True
    return None


#: The whole Nous fetch must finish inside ``quota_cache.REFRESH_BUDGET_S``
#: (20s). Core's account lookup has an 8s socket timeout, which bounds socket
#: inactivity rather than wall time, so each lookup runs in a daemon thread
#: under this hard wall-clock budget; an abandoned worker cannot delay the
#: sweep or process exit.
_NOUS_FETCH_BUDGET_S = 19.0
_NOUS_ACCOUNT_TIMEOUT_S = 8.0

#: Returned by :func:`_bounded_account_lookup` when the wall clock expired.
#: Distinct from ``None`` (a lookup that completed without a value) so the
#: caller can refuse to start a second, overlapping lookup: the abandoned
#: worker may still be in flight, and a duplicate could repeat a token refresh
#: or portal request. Never passed to consumers.
_NOUS_LOOKUP_TIMEOUT = object()


def _bounded_account_lookup(get_info, *, force_fresh: Optional[bool], timeout: float):
    """Call the account lookup under a hard wall-clock timeout.

    ``contextvars.copy_context()`` preserves the active profile/auth context in
    the worker thread. ``force_fresh`` is passed explicitly as a keyword
    whenever it is a bool; ``None`` is the compatibility path for an older core
    whose lookup accepts no keyword and is the only case that calls it with no
    arguments. Returns ``None`` on an empty result, raises a failed lookup's
    exception, and returns :data:`_NOUS_LOOKUP_TIMEOUT` when the wall clock
    expired.
    The worker is a daemon: a timed-out portal request keeps running to its own
    socket timeout but never blocks the quota sweep or process exit.
    """
    if timeout <= 0:
        return _NOUS_LOOKUP_TIMEOUT
    result: dict[str, Any] = {}
    done = threading.Event()
    context = contextvars.copy_context()
    kwargs = {} if force_fresh is None else {"force_fresh": bool(force_fresh)}

    def _worker() -> None:
        try:
            result["value"] = context.run(get_info, **kwargs)
        except Exception as exc:
            result["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_worker, name="quota-nous-account", daemon=True)
    thread.start()
    if not done.wait(timeout):
        return _NOUS_LOOKUP_TIMEOUT
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _nous_lookup_accepts_force_fresh(get_info) -> bool:
    """Whether the lookup advertises the ``force_fresh`` keyword.

    Used only to avoid a speculative keyword call (and its retry) against an
    older core. A wrapper with ``**kwargs`` is treated as supported; if it then
    rejects the keyword, the TypeError fallback still performs one no-argument
    call.
    """
    try:
        parameters = inspect.signature(get_info).parameters
    except (TypeError, ValueError):
        return True
    if "force_fresh" in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _nous_portal_account(get_info, *, deadline: float) -> tuple[Any, bool]:
    """The freshest usable Nous snapshot plus independent master-method claims.

    Two reads, in this order:

    * ``force_fresh=False`` is the old master method. When a valid saved JWT
      exists it is decoded locally; when it does not, core may fall through to
      a network/pool lookup, so this read is not assumed local. If it returns
      an account-API snapshot already, that snapshot is reused as the fresh one
      instead of issuing a duplicate forced read.
    * ``force_fresh=True`` is the authoritative account snapshot (subscription,
      credits, entitlement) and may omit ``raw_claims`` entirely.

    The account-API snapshot wins for service facts. Cached JWT claims are
    attached only when all of these hold: the cached snapshot really came from
    the local JWT (``source == "jwt"``), both snapshots prove the same portal,
    organisation and user identity, their free/paid readings do not contradict
    each other, and their plans are provably identical (matching name and/or
    numeric tier). Missing identity or an unprovable plan change is not a
    match; it contributes no claims. A transient fresh-lookup failure falls
    back to the cached snapshot, but the caller marks it stale and never turns
    its fields into a current Free/paid verdict.

    A first lookup that exceeds its wall-clock slice ends the whole read: its
    abandoned worker may still be running, so starting the forced lookup would
    duplicate the same core work. Both lookups remain inside the sweep's
    deadline.

    NOTE: ``hermes_cli.nous_account`` is a core module, not a documented plugin
    API; ``hermes plugins validate`` warns about it under catalog rule 9. This
    plugin has no public account-data seam (``agent.account_usage`` documents
    ``fetch_account_usage`` for usage panels but registers no ``nous`` fetcher),
    so this stays a fail-open best effort rather than a compliance claim.
    """
    supports_force_fresh = _nous_lookup_accepts_force_fresh(get_info)

    def _lookup(*, force_fresh: bool):
        nonlocal supports_force_fresh
        timeout = min(_NOUS_ACCOUNT_TIMEOUT_S, deadline - time.monotonic())
        if not supports_force_fresh:
            # Known-old core: call it exactly once, with no keyword.
            try:
                return _bounded_account_lookup(get_info, force_fresh=None, timeout=timeout)
            except Exception:
                return None
        try:
            return _bounded_account_lookup(get_info, force_fresh=force_fresh, timeout=timeout)
        except TypeError:
            # A wrapper advertised **kwargs but rejected the keyword. Do not
            # retry the forced read: one no-argument call is enough, and
            # retrying both reads would repeat the same core work.
            supports_force_fresh = False
            if force_fresh:
                return None
            timeout = min(_NOUS_ACCOUNT_TIMEOUT_S, deadline - time.monotonic())
            try:
                return _bounded_account_lookup(get_info, force_fresh=None, timeout=timeout)
            except Exception:
                return None
        except Exception:
            return None

    def _usable(info) -> bool:
        return (
            info is not None
            and getattr(info, "logged_in", False)
            and getattr(info, "source", None) != "error"
        )

    # Read the master snapshot first. If the fresh account lookup ran first,
    # core's process cache could satisfy this call with the claim-less account
    # API object instead.
    cached = _lookup(force_fresh=False)
    if cached is _NOUS_LOOKUP_TIMEOUT:
        # The abandoned first worker may still be refreshing/reading; a second
        # lookup now would duplicate it. Fail closed for this sweep.
        return None, False

    if _usable(cached) and getattr(cached, "source", None) == "account_api":
        # The supposedly local read already went to the account API. Reuse it:
        # a forced read would be the same request a second time.
        fresh = cached
    elif supports_force_fresh:
        fresh = _lookup(force_fresh=True)
        if fresh is _NOUS_LOOKUP_TIMEOUT:
            fresh = None
    else:
        fresh = None

    if _usable(fresh):
        account = fresh
    elif _usable(cached):
        account = cached
    else:
        account = fresh if fresh is not None else cached

    # A forced account-API read that failed leaves only the cached JWT/local
    # snapshot. That snapshot is usable history, not a current entitlement
    # verdict; the caller must mark it stale instead of presenting its paid
    # plan, renewal or credit lines as a fresh read.
    stale_fallback = supports_force_fresh and not _usable(fresh) and _usable(cached)

    # Prove identity once. A cached JWT may contribute display facts only when
    # it is the same Portal account and organisation; rate/spend claims are
    # plan-specific too, while the free tool-pool switch is not (core documents
    # it as decoupled from paid/billing access).
    same_account = (
        _usable(cached)
        and getattr(cached, "source", None) == "jwt"
        and _usable(fresh)
        and _nous_same_account(cached, fresh)
    )
    same_plan = same_account and _nous_same_plan(cached, fresh)
    same_free = False
    if same_account:
        # A stale JWT can still carry the previous plan's rate/spend claims.
        # This plain parity answer rejects a proven paid snapshot against a
        # free one. It also accepts a JWT whose paid_access: true cannot be
        # disproved as Free-with-purchased-credits (core's JWT path has no
        # plan name or purchased-credit fields), because that live shape
        # otherwise lost its rate-limit claims and enabled tool pool. A paid
        # plan name, paid account tier, or paid-subscription evidence still
        # vetoes the match. The unproven shape also needs the same plan tier;
        # otherwise an older paid tier could look free merely because core's
        # JWT path cannot name it.
        cached_free = _nous_snapshot_reads_free(cached)
        fresh_free = _nous_snapshot_reads_free(fresh)
        same_free = cached_free == fresh_free or (
            same_plan and fresh_free and _nous_snapshot_may_read_free(cached)
        )

    claims: dict[str, Any] = {}
    fresh_plan_identity = _nous_plan_identity(fresh) if same_account else None
    fresh_plan_is_free_or_absent = (
        fresh_plan_identity is None or fresh_plan_identity[0] in (None, "free")
    )
    free_plan_match = (
        same_account
        and _nous_snapshot_is_free_plan(cached)
        and _nous_snapshot_reads_free(fresh)
        and fresh_plan_is_free_or_absent
    )
    if free_plan_match:
        # A cached JWT whose own plan identity says Free, against a fresh
        # snapshot that reads Free, is the same plan even when the fresh label
        # is missing (the account API can prove Free through its subscription
        # block alone). Rate limits are then the Free plan's, not a stale paid
        # tier's.
        claims = _nous_master_claims(cached)
    elif same_plan and same_free:
        claims = _nous_master_claims(cached)
    if not claims and _usable(account) and getattr(account, "source", None) == "jwt":
        claims = _nous_master_claims(account)
    if _usable(account) and claims:
        account = _nous_with_master_claims(account, claims)

    # The fresh account API may omit ``tool_access`` entirely on a Free plan
    # whose JWT minted an enabled pool. Inherit it only from a cached JWT that
    # proves the same account and the same free/paid reading. An explicit
    # fresh value (including ``enabled: false``) always wins: absence is the
    # only shape that may be backfilled, so a disabled pool is never revived.
    inherited_tool_access = None
    if (
        same_account
        and same_free
        and _usable(fresh)
        and _nous_snapshot_reads_free(fresh)
    ):
        fresh_tool_access = _nous_tool_access_claim(fresh)
        if fresh_tool_access is None:
            cached_tool_access = _nous_tool_access_claim(cached)
            if (
                cached_tool_access is not None
                and getattr(cached_tool_access, "enabled", None) is True
                and _nous_snapshot_may_read_free(cached)
            ):
                inherited_tool_access = cached_tool_access
    if inherited_tool_access is not None and _usable(account):
        account = _nous_with_tool_access(account, inherited_tool_access)
    return account, stale_fallback


def _fetch_nous_portal() -> QuotaResult:
    deadline = time.monotonic() + _NOUS_FETCH_BUDGET_S
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info
    except Exception:
        return build_unavailable("nous", "fetcher-unavailable")
    account, stale_fallback = _nous_portal_account(get_nous_portal_account_info, deadline=deadline)
    if account is None:
        return build_unavailable("nous", "fetcher-unavailable")
    if not getattr(account, "logged_in", False):
        return build_unavailable("nous", "not-logged-in")

    access = getattr(account, "paid_service_access_info", None)
    sub = getattr(account, "subscription", None)
    if _nous_subscription_is_empty(sub):
        sub = None
    paid = getattr(account, "paid_service_access", None)

    # Classify before rendering any money line. A free account can carry the
    # portal's padded zero balances (and a free-plan subscription object), and
    # those are not facts worth three "$0.00" rows or a "Cycle ends:" date.
    paid_plan_evidence = _nous_has_paid_plan_evidence(access, sub, paid)
    state = _nous_access_state(account, access, sub, paid, paid_plan_evidence)
    lookup_failed = getattr(account, "source", None) == "error"
    stale_snapshot = bool(stale_fallback) and not lookup_failed
    if lookup_failed or stale_snapshot:
        # A failed lookup can still carry a stored tier claim; a stale cached
        # JWT can still carry a paid plan, renewal and rate/spend facts. None
        # of those is evidence of the account's current entitlement. Stay
        # unknown/muted rather than presenting the fallback as a fresh read.
        state = "unknown"

    windows: list[QuotaWindow] = []
    details: list[str] = []
    detail_facts: dict[str, str] = {}
    account_balances: list[AccountBalance] = []

    def _add_detail(key: str, line: Optional[str]) -> None:
        """Append one display line and preserve its identity for merging."""
        if line:
            details.append(line)
            detail_facts[key] = line

    # One prominent, explicitly labeled amount. The portal computes it from
    # every usable credit (included plan allowance + purchased wallet), so it is
    # rendered as-is and never re-added from the component lines below.
    total_usable = _nous_total_usable(access, state)
    if total_usable is not None:
        account_balances.append(
            AccountBalance(
                currency="USD",
                total_balance=f"{total_usable:.2f}",
                label="Account balance",
            )
        )

    # Paid facts (subscription gauge, credit lines, rollover, renewal, spend,
    # rate limits) describe the *current* plan. An unavailable snapshot could
    # not resolve the account or its subscription lapsed, so its stale paid
    # values must not be rendered as current facts. Free and unknown snapshots
    # have no paid plan to describe either.
    paid_facts = state in ("active", "depleted", "paused")
    # A paused plan withholds its included allowance. Its gauges are stale
    # quota, not remaining quota: neither the subscription gauge nor live
    # usage-window windows may be shown or ranked. Renewal/spend facts about
    # the plan may still describe it.
    window_facts = paid_facts and state != "paused"

    # Subscription gauge — only with a positive monthly denominator and a sane
    # remaining value (remaining > cap means rollover across periods, where the
    # monthly number stops being a meaningful denominator). A free plan has no
    # paid subscription window to gauge.
    monthly = _finite_usd(getattr(sub, "monthly_credits", None)) if sub is not None else None
    remaining = _finite_usd(getattr(sub, "credits_remaining", None)) if sub is not None else None
    if window_facts and monthly is not None and monthly > 0 and remaining is not None and remaining <= monthly:
        used_pct = max(0.0, min(100.0, (monthly - remaining) / monthly * 100.0))
        windows.append(QuotaWindow(label="Subscription", used_percent=round(used_pct, 2)))
        _add_detail(
            "subscription_window",
            f"${remaining:.2f} of ${monthly:.2f} subscription credits left",
        )

    # Usage-window plans (Plus / Super / Ultra) carry their allowances in the
    # account snapshot's ``usage_window`` block rather than a monthly credit
    # grant. Render the live gauges in the server's order with the card's
    # exact window names. A stale or unavailable snapshot never reaches here
    # (``paid_facts`` is false), so an old allowance cannot masquerade as
    # current.
    usage_windows = _nous_usage_window_windows(account) if window_facts else []
    if usage_windows:
        windows.extend(usage_windows)

    # The card shows one prominent Account balance and no separate
    # subscription/top-up component lines: `total_usable` above is the
    # authoritative spendable amount, and a zero component beside it is noise,
    # not a second balance.

    if sub is not None and paid_facts:
        rollover = _finite_usd(getattr(sub, "rollover_credits", None))
        if rollover is not None and rollover > 0:
            _add_detail("rollover", f"Rollover: ${rollover:.2f}")
        period_end = getattr(sub, "current_period_end", None)
        if period_end:
            # The account snapshot carries no cancellation state, so it cannot
            # prove the subscription will renew. "Cycle ends" is true for both
            # a renewal and a cancellation; "Renews" was false for the latter.
            _add_detail("cycle_ends", f"Cycle ends: {period_end}")

    # Only the subscription's own plan name is a label here. A bare numeric
    # tier is merely a claim; it becomes "Tier N" below only when a paid
    # subscription/access fact corroborates it (a live free JWT also carries
    # subscription_tier 5).
    plan = _nous_subscription_plan(sub)

    # Some paid Portal accounts expose spend and subscription rate limits in
    # raw claims without a subscription object or credit cap. The fresh account
    # API has its own member_spend_usd/member_spend_cap_usd fields, so prefer
    # those over a cached JWT when both exist; an older JWT-only snapshot still
    # supplies them. These are useful details, but spend is not a quota
    # denominator and must not become a percentage window.
    member_spend = _nous_spend_value(account, access, "member_spend_usd")
    member_spend_cap = _nous_spend_value(account, access, "member_spend_cap_usd")
    if member_spend is not None and paid_facts:
        suffix = " (no cap reported)" if member_spend_cap is None else ""
        _add_detail("spend", f"Spend this period: ${member_spend:.2f}{suffix}")

    rate_limits = []
    for claim, unit in (("rate_limit_rpm", "RPM"), ("rate_limit_tpm", "TPM"), ("rate_limit_rph", "RPH")):
        formatted = _format_rate_limit(_nous_claim(account, claim))
        if formatted is not None:
            rate_limits.append(f"{formatted} {unit}")
    if rate_limits and state != "unavailable" and not (lookup_failed or stale_snapshot):
        _add_detail("rate_limits", "Rate limits: " + " · ".join(rate_limits))
    elif not (lookup_failed or stale_snapshot):
        published = _nous_published_rate_limits(account, state, sub)
        if published is not None:
            _tier, published_rpm, published_tpm = published
            rpm_text = _format_rate_limit(published_rpm)
            tpm_text = _format_rate_limit(published_tpm)
            if rpm_text and tpm_text:
                _add_detail(
                    "rate_ceiling",
                    f"Rate ceiling: {rpm_text} RPM / {tpm_text} TPM (published)",
                )

    if lookup_failed or stale_snapshot:
        plan = None
    tier = _nous_tier_claim(account, access)

    # A subscription-tier claim is a plan label only when a paid subscription
    # actually corroborates it. A live free JWT carries ``subscription_tier`` 5;
    # turning that claim into "Tier 5" was the original misclassification. An
    # unavailable snapshot's paid labels are stale, so they are suppressed.
    tier_is_paid_label = (
        paid is True
        or sub is not None
        or (
            access is not None
            and (
                getattr(access, "has_active_subscription", None) is True
                or getattr(access, "active_subscription_is_paid", None) is True
            )
        )
    )
    if plan is None and tier is not None and tier_is_paid_label and state in (
        "active", "depleted", "paused",
    ):
        plan = f"Tier {tier}"
    if state == "unavailable":
        # The plan name/subscription object may be a stale copy from before the
        # lookup failed or the subscription lapsed; the status line is the only
        # current fact this snapshot proves.
        plan = None

    if state == "free":
        # A Free plan can hold a positive purchased wallet. Those dollars fund
        # paid models, so the old "free models only" line would deny a balance
        # the same snapshot reports. Show the positive top-up component and say
        # what it pays for; zero subscription lines and the free plan's padded
        # renewal date stay suppressed. A zero total contradicts a usable
        # wallet, so in that inconsistent shape the copy stays conservative.
        purchased = _nous_credit_value(access, "purchased_credits_remaining")
        purchased_positive = purchased is not None and purchased > 0
        purchased_funds_calls = purchased_positive and (
            total_usable is None or total_usable > 0
        )
        if purchased_funds_calls:
            _add_detail(
                "free_tier",
                "Free tier - paid models via purchased credits",
            )
        elif total_usable is not None and total_usable > 0:
            # A funded total without a positive purchased component is a
            # credit source this snapshot does not name. Do not deny it with
            # "free models only" and do not invent "purchased" either.
            _add_detail("free_tier", "Free tier - credits available")
        else:
            _add_detail("free_tier", "Free tier - free models only")
        pool = _nous_tool_pool_labels(account)
        if pool:
            _add_detail("tool_pool", "Tool pool: " + ", ".join(pool))
        plan = "Free"
    elif state == "depleted":
        _add_detail("status", "Status: access depleted - top up to restore")
    elif state == "paused":
        paused_reason = _nous_paused_reason(account)
        if paused_reason == "payment_past_due":
            _add_detail("status", "Status: included usage paused - subscription payment failed")
        elif access is not None and getattr(access, "member_spend_cap_exceeded", None) is True:
            _add_detail("status", "Status: access paused - member spend cap exceeded")
        else:
            _add_detail("status", "Status: included usage paused - check billing")
    elif state == "unavailable":
        _add_detail("status", "Paid access is unavailable - check billing or refresh the Portal session")
    elif state == "unknown":
        if stale_snapshot:
            _add_detail(
                "status",
                "Paid access: unknown - cached account snapshot is stale; the fresh lookup failed",
            )
        elif lookup_failed:
            _add_detail("status", "Account lookup failed - access status unknown")
        else:
            _add_detail("status", "Paid access: unknown - this account snapshot has no subscription details")
    elif state == "active" and not windows and not details:
        _add_detail("status", "Paid access: active")

    return QuotaResult(
        label="nous",
        windows=windows,
        plan=plan,
        details=details,
        detail_facts=detail_facts,
        account_balances=account_balances,
        api_calls_available=_nous_api_calls_available(
            state, access, sub, paid, total_usable,
            usage_window=getattr(account, "usage_window", None),
        ),
        access_state=state,
        account_org_key=_nous_org_match_key(getattr(account, "org_slug", None)),
    )


_register("nous")(_fetch_nous_portal)

# -- OpenAI Codex (with per-model Spark limits) ------------------------------
# The core fetcher covers plan-level Session/Weekly windows, but drops
# ``additional_rate_limits`` — the per-model quotas (e.g. GPT-5.3-Codex-Spark)
# the Codex backend reports alongside them. The read-only adapter enumerates
# saved credentials; this parser normalizes each account independently.

def _fetch_codex_with_models() -> QuotaResult:
    from .codex import fetch_codex_quota

    return fetch_codex_quota(_parse_codex_payload)


# Codex plan names as shown to the user: the API's "prolite" is the $100 Pro
# tier and "pro" the $200 tier. Unknown values (including a Pro 500 tier, whose
# raw API value is not yet confirmed) keep a title-cased upstream spelling.
_CODEX_PLAN_NAMES = {
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "prolite": "Pro 100",
    "pro": "Pro 200",
    "business": "Business",
    "enterprise": "Enterprise",
}


def _codex_plan_label(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    return _CODEX_PLAN_NAMES.get(text.lower(), text.title())


def _parse_codex_payload(payload: dict) -> QuotaResult:
    from datetime import datetime, timezone

    def _iso(ts):
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return None
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            # A timestamp outside the representable range is a schema surprise,
            # not a crash: this runs after the request try/except has closed.
            return None

    def _window(raw: dict, label: str) -> Optional[QuotaWindow]:
        if not isinstance(raw, dict):
            return None
        used = raw.get("used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            return None
        try:
            used_percent = float(used)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(used_percent) or not 0.0 <= used_percent <= 100.0:
            return None
        return QuotaWindow(
            label=label,
            used_percent=used_percent,
            reset_at=_iso(raw.get("reset_at")),
        )

    windows: list[QuotaWindow] = []
    # A malformed optional field must not erase healthy quota windows.
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    duration_labels = {18_000: "Session", 604_800: "Weekly"}
    for key, fallback in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        raw_window = rate_limit.get(key)
        raw_window = raw_window if isinstance(raw_window, dict) else {}
        seconds = raw_window.get("limit_window_seconds")
        label = fallback
        if isinstance(seconds, int) and not isinstance(seconds, bool):
            label = duration_labels.get(seconds, fallback)
        elif isinstance(seconds, float) and math.isfinite(seconds):
            label = duration_labels.get(int(seconds), fallback)
        w = _window(raw_window, label)
        if w is not None:
            windows.append(w)

    # Per-model limits (research-preview models like Codex Spark).
    extras = payload.get("additional_rate_limits")
    for extra in extras if isinstance(extras, list) else ():
        if not isinstance(extra, dict):
            continue
        model_name = str(extra.get("limit_name") or "").strip()
        if not model_name:
            continue
        short = model_name.replace("GPT-", "").replace("-Codex-", " Codex ")
        inner = extra.get("rate_limit")
        if not isinstance(inner, dict):
            continue
        for key, label in (("primary_window", "5h"), ("secondary_window", "Weekly")):
            w = _window(inner.get(key) or {}, f"{short} · {label}")
            if w is not None:
                windows.append(w)

    details: list[str] = []
    reset_credits = payload.get("rate_limit_reset_credits")
    if not isinstance(reset_credits, dict):
        reset_credits = {}
    def _finite_number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            value = float(value)
            return value if math.isfinite(value) else None
        except (OverflowError, ValueError):
            return None

    banked = _finite_number(reset_credits.get("available_count"))
    if banked is not None and banked >= 1:
        count = int(banked)
        plural = "s" if count != 1 else ""
        details.append(f"You have {count} reset{plural} banked - use /usage reset to activate")
    credits = payload.get("credits")
    if not isinstance(credits, dict):
        credits = {}
    if credits.get("has_credits"):
        balance = _finite_number(credits.get("balance"))
        if balance is not None:
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")

    plan = _codex_plan_label(payload.get("plan_type"))
    if not windows and not details:
        return build_unavailable("openai-codex", "no-data")
    return QuotaResult(
        label="openai-codex",
        windows=windows,
        plan=plan,
        unavailable_reason=None,
        details=details,
    )


_register("openai-codex")(_fetch_codex_with_models)
