"""Grok (X Premium / SuperGrok) quota fetcher — plugin standalone copy."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.request
import urllib.error
from typing import Optional

from .base import QuotaResult, QuotaWindow, build_unavailable, opt_in_flag, urlopen_no_redirect
from .browser_cookies import (
    ChromeCookieError,
    load_chrome_grok_cookies,
    load_firefox_grok_cookies,
)

_GROK_ENDPOINT = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
_REST_RATELIMITS_URL = "https://grok.com/rest/rate-limits"
_EMPTY_GRPCWEB_BODY = b"\x00\x00\x00\x00\x00"  # 0x00 frame + 4-byte len(0)
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

_SESSION_PATH = os.path.join(os.path.expanduser("~"), "grok_session.json")
_RAW_DEBUG_PATH = os.path.join(os.path.expanduser("~"), "grok_last_response.bin")


def _write_private_debug(path: str, raw: bytes) -> None:
    """Atomically replace a private debug file without following target symlinks."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    prefix = f".{os.path.basename(path)}."
    fd, temp_path = tempfile.mkstemp(prefix=prefix, dir=parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _load_cookies() -> Optional[str]:
    try:
        with open(_SESSION_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        cookies = data.get("cookies") or data
        if isinstance(cookies, dict):
            cookies = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if cookies and str(cookies).strip():
            return str(cookies)
    except Exception:
        pass

    # Automatic local fallback: Firefox first (plaintext cookies), then
    # Chrome (encrypted). Only grok.com cookies are selected.
    firefox = load_firefox_grok_cookies()
    if firefox:
        return firefox
    return load_chrome_grok_cookies()


def _token_from_auth_json(path: str) -> Optional[str]:
    """Read one Hermes ``xai-oauth`` access token without caching it."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    providers = data.get("providers")
    state = providers.get("xai-oauth") if isinstance(providers, dict) else None
    tokens = state.get("tokens") if isinstance(state, dict) else None
    if isinstance(tokens, dict):
        token = tokens.get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()

    pool = data.get("credential_pool")
    entries = pool.get("xai-oauth") if isinstance(pool, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict):
            continue
        token = entry.get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _hermes_xai_access_token(force_refresh: bool = False) -> Optional[str]:
    """Read Hermes' xAI OAuth token, refreshing it first when a caller asks."""
    try:
        from hermes_cli.auth_xai import resolve_xai_oauth_runtime_credentials

        runtime = resolve_xai_oauth_runtime_credentials(force_refresh=force_refresh) or {}
        token = str(runtime.get("api_key", "") or "").strip()
        if token:
            return token
    except Exception:
        pass

    for module_name in ("hermes_cli.auth_xai", "hermes_cli.auth"):
        try:
            module = __import__(module_name, fromlist=["_read_xai_oauth_tokens"])
            reader = getattr(module, "_read_xai_oauth_tokens")
            state = reader()
            tokens = state.get("tokens") if isinstance(state, dict) else None
            token = tokens.get("access_token") if isinstance(tokens, dict) else None
            if isinstance(token, str) and token.strip():
                return token.strip()
        except Exception:
            continue

    candidates: list[str] = []
    configured_home = os.environ.get("HERMES_HOME")
    if configured_home:
        candidates.append(os.path.join(configured_home, "auth.json"))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "hermes", "auth.json"))
    try:
        from hermes_constants import get_hermes_home

        home = os.path.abspath(str(get_hermes_home()))
        candidates.append(os.path.join(home, "auth.json"))
        parts = os.path.normpath(home).split(os.sep)
        if len(parts) >= 2 and parts[-2].lower() == "profiles":
            candidates.append(os.path.join(os.sep.join(parts[:-2]), "auth.json"))
    except Exception:
        pass

    seen: set[str] = set()
    for path in candidates:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        token = _token_from_auth_json(path)
        if token:
            return token
    return None


def _grok_grpc_headers(*, cookies: Optional[str] = None, bearer: Optional[str] = None) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "content-type": "application/grpc-web+proto",
        "origin": "https://grok.com",
        "referer": "https://grok.com/?_s=usage",
        "user-agent": _BROWSER_UA,
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "cache-control": "no-cache",
        "pragma": "no-cache",
    }
    if cookies:
        headers["cookie"] = cookies
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _fetch_grok_grpc(headers: dict[str, str]) -> Optional[QuotaResult]:
    """Fetch and parse the Grok billing gRPC response for one credential."""
    req = urllib.request.Request(
        _GROK_ENDPOINT,
        data=_EMPTY_GRPCWEB_BODY,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen_no_redirect(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return build_unavailable("grok", "cloudflare-blocked" if e.code == 403 else "auth-failed")
        return build_unavailable("grok", f"http-{e.code}")
    except Exception as e:
        return build_unavailable("grok", f"fetch-error:{type(e).__name__}")
    if os.environ.get("HERMES_QUOTA_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            _write_private_debug(_RAW_DEBUG_PATH, raw)
        except Exception:
            pass
    if not raw:
        # An invalid or absent credential gets HTTP 200 with an empty body, not 401.
        return build_unavailable("grok", "auth-failed")
    return _parse_grok_protobuf(raw)


def _is_grok_auth_failure(result: Optional[QuotaResult]) -> bool:
    return bool(
        result is not None
        and result.unavailable_reason in {"auth-failed", "cloudflare-blocked", "http-401", "http-403"}
    )


def _parse_grok_protobuf(raw: bytes) -> Optional[QuotaResult]:
    import struct

    msg = raw
    if msg[:1] == b"\x00" and len(msg) >= 5:
        length = int.from_bytes(msg[1:5], "big")
        if length and len(msg) >= 5 + length:
            msg = msg[5 : 5 + length]

    def parse(m):
        out = []
        i = 0
        while i < len(m):
            if i >= len(m):
                break
            key = m[i]
            i += 1
            fn = key >> 3
            wire = key & 0x07
            if wire == 0:
                v = 0
                s = 0
                while i < len(m):
                    b = m[i]
                    i += 1
                    v |= (b & 0x7F) << s
                    s += 7
                    if not (b & 0x80):
                        break
                out.append((fn, wire, v))
            elif wire == 2:
                ln = 0
                s = 0
                while i < len(m):
                    b = m[i]
                    i += 1
                    ln |= (b & 0x7F) << s
                    s += 7
                    if not (b & 0x80):
                        break
                d = m[i : i + ln]
                i += ln
                out.append((fn, wire, d))
            elif wire == 5:
                v = struct.unpack("<f", m[i : i + 4])[0]
                i += 4
                out.append((fn, wire, v))
            elif wire == 1:
                v = int.from_bytes(m[i : i + 8], "little")
                i += 8
                out.append((fn, wire, v))
            else:
                break
        return out

    top = parse(msg)
    if not top or top[0][0] != 1 or top[0][1] != 2:
        return None
    inner = parse(top[0][2])

    # Field map (decoded from a live capture, cross-checked against the
    # grok.com usage screen):
    #   fn1 float          — Weekly Limit % used (ABSENT = 0% used; the
    #                        grok.com panel renders "0% utilizado" when the
    #                        field is missing, e.g. free/unused accounts)
    #   fn4 / fn5 msg      — Weekly window start / reset (fn1 = epoch seconds)
    #   fn7 / fn8 msg      — typed breakdown of the SAME window: fn1 = kind,
    #                        fn2 float = % used. The panel draws these as the
    #                        legend under the single bar ("Grok Build 3%"), not
    #                        as extra meters — publishing them as windows showed
    #                        two quotas with the same % and reset date.
    #   fn11 / fn13 varint — flags with no counterpart in the grok.com UI; left
    #                        unreported on purpose. A fn11==1 → "Reset banked:
    #                        available" claim was shipped once and was wrong: the
    #                        Usage panel for the same payload showed only
    #                        "Weekly Limit 3% used / Resets …" and "Grok Build 3%".
    #                        Do not turn an unidentified field into a claim.
    used_percent: Optional[float] = None
    reset_epoch: Optional[int] = None

    def _sub_epoch(blob: bytes) -> Optional[int]:
        for sfn, sw, sv in parse(blob):
            if sfn == 1 and sw == 0 and isinstance(sv, int):
                return sv
        return None

    for fn, wire, v in inner:
        if fn == 1 and wire == 5:
            try:
                used_percent = float(v)
            except (TypeError, ValueError):
                pass
        elif fn == 5 and wire == 2:
            epoch = _sub_epoch(v)
            if epoch is not None and (reset_epoch is None or epoch > reset_epoch):
                reset_epoch = epoch

    if used_percent is None and reset_epoch is None:
        return None

    from datetime import datetime, timezone

    def _iso(epoch: Optional[int]) -> Optional[str]:
        if epoch is None:
            return None
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    windows: list[QuotaWindow] = []
    if used_percent is None and reset_epoch is not None:
        # Weekly window present but no usage field: the grok.com panel
        # renders this as "0% utilizado" (free/unused accounts).
        used_percent = 0.0
    if used_percent is not None or reset_epoch is not None:
        windows.append(
            QuotaWindow(
                label="Weekly Limit", used_percent=used_percent, reset_at=_iso(reset_epoch)
            )
        )

    if not windows:
        return None
    return QuotaResult(label="grok", windows=windows, details=[], unavailable_reason=None)


def _fetch_grok_rest(cookies: str) -> Optional[QuotaResult]:
    """Fallback: per-model chat quota JSON endpoint (rest/rate-limits).

    This is a DIFFERENT meter from the billing gRPC above: chat queries per
    model on a rolling 2h window (e.g. ``10/10``), not the Weekly/Grok Build
    usage shown on the grok.com panel. Only reached when the gRPC probe fails.
    Plan name is not exposed anywhere in the Grok API (verified against both
    surfaces), so we never invent one.
    """
    body = json.dumps({"requestKind": "DEFAULT", "modelName": "grok-4"}).encode("utf-8")
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/?_s=usage",
        "user-agent": _BROWSER_UA,
        "cookie": cookies,
    }
    req = urllib.request.Request(_REST_RATELIMITS_URL, data=body, headers=headers, method="POST")
    try:
        with urlopen_no_redirect(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return build_unavailable("grok", "cloudflare-blocked" if e.code == 403 else "auth-failed")
        return None  # fall through to the gRPC probe
    except Exception:
        return None  # fall through to the gRPC probe

    windows: list[QuotaWindow] = []
    remaining = data.get("remainingQueries")
    total = data.get("totalQueries")
    if isinstance(remaining, (int, float)) and isinstance(total, (int, float)) and total > 0:
        used_pct = round(100.0 * (1.0 - float(remaining) / float(total)), 2)
        # No reset instant is reported: windowSizeSeconds is the LENGTH of a
        # rolling window, not an anchor, so any timestamp derived from it would
        # be invented (the panel's real reset comes from the gRPC path).
        windows.append(QuotaWindow(label="2h", used_percent=used_pct, reset_at=None))

    def _eff(block, label):
        if not isinstance(block, dict):
            return
        rem, tot = block.get("remainingQueries"), block.get("totalQueries")
        if isinstance(rem, (int, float)) and isinstance(tot, (int, float)) and tot > 0:
            windows.append(
                QuotaWindow(
                    label=label,
                    used_percent=round(100.0 * (1.0 - float(rem) / float(tot)), 2),
                )
            )

    _eff(data.get("lowEffortRateLimits"), "low effort")
    _eff(data.get("highEffortRateLimits"), "high effort")

    if not windows:
        return None
    return QuotaResult(label="grok", windows=windows, plan=None, unavailable_reason=None)


def fetch_grok_quota() -> QuotaResult:
    # Hermes' xai-oauth bearer is the primary credential path. Only an
    # authentication failure falls through to the browser-cookie chain below;
    # transport and parse failures stay visible instead of masking the OAuth
    # result with a second request.
    token = _hermes_xai_access_token()
    if token:
        oauth_result = _fetch_grok_grpc(_grok_grpc_headers(bearer=token))
        if oauth_result is None or _is_grok_auth_failure(oauth_result):
            # Only the bearer can be stale here, so retry once with a forced
            # refresh before the cookie fallback.
            refreshed = _hermes_xai_access_token(force_refresh=True)
            if refreshed and refreshed != token:
                oauth_result = _fetch_grok_grpc(_grok_grpc_headers(bearer=refreshed))
        if oauth_result is None:
            return build_unavailable("grok", "parse-pending")
        if not _is_grok_auth_failure(oauth_result):
            return oauth_result

    try:
        cookies = _load_cookies()
    except ChromeCookieError as exc:
        return build_unavailable("grok", exc.reason)
    if not cookies:
        return build_unavailable("grok", "no-session-cookies")

    # Primary: the billing gRPC — this is the "Weekly Limit" + "Grok Build"
    # usage screen (percent used + reset). Verified 1:1 against the grok.com
    # usage panel: same percentages, same reset instant. Fields the panel does
    # not render (fn11/fn13 flags) are deliberately not reported.
    result = _fetch_grok_grpc(_grok_grpc_headers(cookies=cookies))
    if result is not None:
        return result

    # Fallback: the per-model chat quota (rest/rate-limits). NOTE: this is a
    # DIFFERENT meter — chat queries per model on a rolling 2h window, not the
    # Build/weekly usage the panel shows. Only used when the gRPC parse fails.
    rest = _fetch_grok_rest(cookies)
    if rest is not None:
        return rest
    return build_unavailable("grok", "parse-pending")


from .registry import register as _register  # noqa: E402

# Grok is opt-in: reuses the Hermes xai-oauth token first, then reads the
# grok.com cookie fallback chain. Disabled by default; enable with:
#   hermes config set plugins.entries.quota.settings.grokEnabled true
import os as _os

# Resolve opt-in from plugin config (set via `hermes config set ...`), falling
# back to the HERMES_QUOTA_GROK_ENABLED env var. Config wins so the UI toggle
# and CLI config are the single source of truth.
def _grok_enabled() -> bool:
    val = _os.environ.get("HERMES_QUOTA_GROK_ENABLED", "").strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get("quota") if isinstance(entries, dict) else None
        if isinstance(entry, dict):
            settings = entry.get("settings")
            if isinstance(settings, dict) and "grokEnabled" in settings:
                return opt_in_flag(settings.get("grokEnabled"))
    except Exception:
        pass
    return False


def _fetch_grok_optin() -> Optional["QuotaResult"]:
    if not _grok_enabled():
        return build_unavailable("grok", "opt-in-disabled")
    return fetch_grok_quota()


_register("grok")(_fetch_grok_optin)
