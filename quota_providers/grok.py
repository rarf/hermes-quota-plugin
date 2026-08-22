"""Grok (X Premium / SuperGrok) quota fetcher — plugin standalone copy."""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
import urllib.error
from typing import Optional

from .base import QuotaResult, QuotaWindow, build_unavailable
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
    #   fn1 float          — Weekly Limit % used
    #   fn4 / fn5 msg      — Weekly window start / reset (fn1 = epoch seconds)
    #   fn7 msg            — typed sub-quota: fn1 = kind, fn2 float = % used
    #                        (kind 2 = "Grok Build" on the usage screen)
    #   fn8 msg            — same kind, with fn2/fn3 = start/reset sub-messages
    #   fn11 varint        — 1 = "Reset Available" (banked usage-limit reset;
    #                        its expiry date is NOT part of this payload)
    used_percent: Optional[float] = None
    reset_epoch: Optional[int] = None
    kind_used: dict[int, float] = {}
    kind_reset: dict[int, int] = {}
    reset_banked = False

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
        elif fn == 7 and wire == 2:
            kind: Optional[int] = None
            val: Optional[float] = None
            for sfn, sw, sv in parse(v):
                if sfn == 1 and sw == 0:
                    kind = sv
                elif sfn == 2 and sw == 5:
                    try:
                        val = float(sv)
                    except (TypeError, ValueError):
                        val = None
            if kind is not None and val is not None:
                kind_used[kind] = val
        elif fn == 8 and wire == 2:
            kind = None
            end: Optional[int] = None
            for sfn, sw, sv in parse(v):
                if sfn == 1 and sw == 0:
                    kind = sv
                elif sfn == 3 and sw == 2:
                    epoch = _sub_epoch(sv)
                    if epoch is not None:
                        end = epoch
            if kind is not None and end is not None:
                kind_reset[kind] = end
        elif fn == 11 and wire == 0:
            reset_banked = v == 1

    if used_percent is None and not kind_used and reset_epoch is None:
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
    if used_percent is not None or reset_epoch is not None:
        windows.append(
            QuotaWindow(label="Weekly", used_percent=used_percent, reset_at=_iso(reset_epoch))
        )
    kind_labels = {2: "Grok Build"}
    for kind, val in kind_used.items():
        windows.append(
            QuotaWindow(
                label=kind_labels.get(kind, f"quota kind {kind}"),
                used_percent=val,
                reset_at=_iso(kind_reset.get(kind)),
            )
        )

    details: list[str] = []
    if reset_banked:
        details.append("Reset banked: available (activate at grok.com)")

    if not windows and not details:
        return None
    return QuotaResult(label="grok", windows=windows, details=details, unavailable_reason=None)


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
        with urllib.request.urlopen(req, timeout=15) as resp:
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
        reset_iso = None
        size = data.get("windowSizeSeconds")
        if isinstance(size, (int, float)) and size > 0:
            from datetime import datetime, timezone

            reset_iso = datetime.fromtimestamp(
                time.time() + float(size), tz=timezone.utc
            ).isoformat()
        windows.append(QuotaWindow(label="2h", used_percent=used_pct, reset_at=reset_iso))

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
    try:
        cookies = _load_cookies()
    except ChromeCookieError as exc:
        return build_unavailable("grok", exc.reason)
    if not cookies:
        return build_unavailable("grok", "no-session-cookies")

    # Primary: the billing gRPC — this is the "Weekly Limit" + "Grok Build"
    # usage screen (percent used + reset + banked-reset flag). Verified
    # 1:1 against the grok.com usage panel.
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
        "cookie": cookies,
    }
    req = urllib.request.Request(_GROK_ENDPOINT, data=_EMPTY_GRPCWEB_BODY, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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
    result = _parse_grok_protobuf(raw)
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

# Grok is opt-in: reads browser cookies (Firefox grok.com, then Chrome on
# macOS). Disabled by default; enable with:
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
                return bool(settings.get("grokEnabled"))
    except Exception:
        pass
    return False


def _fetch_grok_optin() -> Optional["QuotaResult"]:
    if not _grok_enabled():
        return build_unavailable("grok", "opt-in-disabled")
    return fetch_grok_quota()


_register("grok")(_fetch_grok_optin)
