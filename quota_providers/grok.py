"""Grok (X Premium / SuperGrok) quota fetcher — plugin standalone copy."""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Optional

from .base import QuotaResult, QuotaWindow, build_unavailable

_GROK_ENDPOINT = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
_EMPTY_GRPCWEB_BODY = b"\x00\x00\x00\x00\x00"  # 0x00 frame + 4-byte len(0)
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

_SESSION_PATH = os.path.join(os.path.expanduser("~"), "grok_session.json")
_RAW_DEBUG_PATH = os.path.join(os.path.expanduser("~"), "grok_last_response.bin")


def _load_cookies() -> Optional[str]:
    try:
        with open(_SESSION_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        cookies = data.get("cookies") or data
        if isinstance(cookies, dict):
            cookies = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if not cookies or not str(cookies).strip():
            return None
        return str(cookies)
    except Exception:
        return None


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

    used_percent: Optional[float] = None
    reset_epoch: Optional[int] = None

    for fn, wire, v in inner:
        if fn == 1 and wire == 5:
            try:
                used_percent = float(v)
            except (TypeError, ValueError):
                pass
        elif fn in (4, 5) and wire == 2:
            sub = parse(v)
            for sfn, sw, sv in sub:
                if sfn == 1 and sw == 0:
                    if reset_epoch is None or sv > reset_epoch:
                        reset_epoch = sv

    if used_percent is None and reset_epoch is None:
        return None

    reset_iso = None
    if reset_epoch is not None:
        try:
            from datetime import datetime, timezone

            reset_iso = datetime.fromtimestamp(reset_epoch, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            reset_iso = None

    win = QuotaWindow(label="Weekly", used_percent=used_percent, reset_at=reset_iso)
    return QuotaResult(label="grok", windows=[win], plan=None, unavailable_reason=None)


def fetch_grok_quota() -> QuotaResult:
    cookies = _load_cookies()
    if not cookies:
        return build_unavailable("grok", "no-session-cookies")
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
    try:
        with open(_RAW_DEBUG_PATH, "wb") as fh:
            fh.write(raw)
    except Exception:
        pass
    result = _parse_grok_protobuf(raw)
    if result is None:
        return build_unavailable("grok", "parse-pending")
    return result


from .registry import register as _register  # noqa: E402

_register("grok")(fetch_grok_quota)
