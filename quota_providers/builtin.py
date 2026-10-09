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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .base import (
    GENERATED_ACCOUNT_ID_PREFIX,
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


def _backoff_state_path() -> str:
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home())
    except Exception:  # noqa: BLE001 - fail-open: fall back to the plugin dir
        home = str(Path(__file__).resolve().parent.parent)
    return os.path.join(home, _ANTHROPIC_BACKOFF_FILENAME)


def _backoff_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def _load_backoff() -> dict[str, float]:
    try:
        with open(_backoff_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 - missing/corrupt state means no cooldown
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
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if not math.isfinite(number) or number > ceiling:
            continue
        out[str(key)] = number
    return out


def _save_backoff(state: dict[str, float]) -> None:
    import tempfile

    tmp = None
    try:
        path = _backoff_state_path()
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


def _open_anthropic_cooldown(token: str) -> None:
    import time

    now = time.time()
    state = {k: v for k, v in _load_backoff().items() if v > now}
    state[_backoff_key(token)] = now + _ANTHROPIC_RATE_LIMIT_COOLDOWN_S
    _save_backoff(state)


def _request_anthropic_usage(
    token: str, *, timeout: float = _ANTHROPIC_TIMEOUT_S
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """One bounded, redirect-free usage read for a given bearer token.

    ``token`` is passed in, never resolved here, so the same request path
    serves the core-resolved account and every configured extra account.
    """
    if _anthropic_cooling_down(token):
        return None, "rate-limited"
    request = urllib.request.Request(
        _ANTHROPIC_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "hermes-quota-plugin/2.10.0",
        },
        method="GET",
    )
    try:
        with urlopen_no_redirect(request, timeout=timeout) as resp:
            body = resp.read(_MAX_HTTP_BYTES + 1)
        if len(body) > _MAX_HTTP_BYTES:
            return None, "bad-json"
        payload = json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "auth-failed"
        if exc.code == 429:
            _open_anthropic_cooldown(token)
        return None, f"http-{exc.code}"
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        return None, f"fetch-error:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "bad-json"
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


def _format_rate_limit(value) -> Optional[str]:
    number = _finite_usd(value)
    if number is None or number < 0:
        return None
    if number >= 1_000_000:
        return f"{number / 1_000_000:g}M"
    if number >= 1_000:
        return f"{number / 1_000:g}k"
    return f"{number:g}"


def _nous_tool_pool_labels(account_info) -> list[str]:
    """Names of the free Tool-Gateway categories this account may use."""
    try:
        import dataclasses

        coverage = getattr(getattr(account_info, "tool_access", None), "coverage", None)
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


def _credit_line(access, attr, label) -> Optional[str]:
	"""Return a '$X.XX' detail line when the attribute is a finite USD amount."""
	amount = _finite_usd(getattr(access, attr, None))
	if amount is None:
		return None
	return f"{label}: ${amount:.2f}"


def _fetch_nous_portal() -> QuotaResult:
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info

        account = get_nous_portal_account_info()
    except Exception:
        return build_unavailable("nous", "fetcher-unavailable")
    if account is None or not getattr(account, "logged_in", False):
        return build_unavailable("nous", "not-logged-in")

    access = getattr(account, "paid_service_access_info", None)
    sub = getattr(account, "subscription", None)
    paid = getattr(account, "paid_service_access", None)

    windows: list[QuotaWindow] = []
    details: list[str] = []

    # Subscription gauge — only with a positive monthly denominator and a sane
    # remaining value (remaining > cap means rollover across periods, where the
    # monthly number stops being a meaningful denominator).
    monthly = _finite_usd(getattr(sub, "monthly_credits", None)) if sub is not None else None
    remaining = _finite_usd(getattr(sub, "credits_remaining", None)) if sub is not None else None
    if monthly is not None and monthly > 0 and remaining is not None and remaining <= monthly:
        used_pct = max(0.0, min(100.0, (monthly - remaining) / monthly * 100.0))
        windows.append(QuotaWindow(label="Subscription", used_percent=round(used_pct, 2)))
        details.append(f"${remaining:.2f} of ${monthly:.2f} subscription credits left")

    if access is not None:
        for attr, label in (
            ("subscription_credits_remaining", "Subscription credits"),
            ("purchased_credits_remaining", "Top-up credits"),
            ("total_usable_credits", "Total usable"),
        ):
            line = _credit_line(access, attr, label)
            if line is not None:
                details.append(line)

    if sub is not None:
        rollover = _finite_usd(getattr(sub, "rollover_credits", None))
        if rollover is not None and rollover > 0:
            details.append(f"Rollover: ${rollover:.2f}")
        period_end = getattr(sub, "current_period_end", None)
        if period_end:
            details.append(f"Renews: {period_end}")

    plan = (getattr(sub, "plan", None) if sub is not None else None) or None

    # Some paid Portal accounts expose spend and subscription rate limits in
    # raw claims without a subscription object or credit cap. These are useful
    # details, but spend is not a quota denominator and must not become a
    # percentage window.
    member_spend = _finite_usd(_nous_claim(account, "member_spend_usd"))
    member_spend_cap = _finite_usd(_nous_claim(account, "member_spend_cap_usd"))
    if member_spend is not None:
        suffix = " (no cap reported)" if member_spend_cap is None else ""
        details.append(f"Spend this period: ${member_spend:.2f}{suffix}")

    tier = _finite_usd(_nous_claim(account, "subscription_tier"))
    if plan is None and tier is not None and tier >= 0 and tier.is_integer():
        plan = f"Tier {int(tier)}"

    rate_limits = []
    for claim, unit in (("rate_limit_rpm", "RPM"), ("rate_limit_tpm", "TPM"), ("rate_limit_rph", "RPH")):
        formatted = _format_rate_limit(_nous_claim(account, claim))
        if formatted is not None:
            rate_limits.append(f"{formatted} {unit}")
    if rate_limits:
        details.append("Rate limits: " + " · ".join(rate_limits))

    if not windows and not details:
        if paid is False:
            # Free tier: the portal exposes no credit/usage numbers at all
            # (verified against a live free account). Show what IS true.
            details.append("Free tier - free models only")
            details.append("Rate ceiling: 50 RPM / 500k TPM (published)")
            pool = _nous_tool_pool_labels(account)
            if pool:
                details.append("Tool pool: " + ", ".join(pool))
            plan = plan or "Free"
        else:
            return build_unavailable("nous", "no-data")
    elif paid is False:
        details.append("Status: access depleted - top up to restore")

    return QuotaResult(label="nous", windows=windows, plan=plan, details=details)


_register("nous")(_fetch_nous_portal)


# -- OpenAI Codex (with per-model Spark limits) ------------------------------
# The core fetcher covers plan-level Session/Weekly windows, but drops
# ``additional_rate_limits`` — the per-model quotas (e.g. GPT-5.3-Codex-Spark)
# the Codex backend reports alongside them. The read-only adapter enumerates
# saved credentials; this parser normalizes each account independently.

def _fetch_codex_with_models() -> QuotaResult:
    from .codex import fetch_codex_quota

    return fetch_codex_quota(_parse_codex_payload)


# OpenAI's public plan names: Pro is the $100 tier ("5x" usage), Pro 20x the
# $200 tier. The API's "prolite" is the $100 tier. Unknown values keep their
# upstream spelling rather than being guessed.
_CODEX_PLAN_NAMES = {
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "prolite": "Pro 5x",
    "pro": "Pro 20x",
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
