"""Local browser-cookie helpers for the Grok quota provider.

Firefox stores cookies in cookies.sqlite. Chrome stores them encrypted in
Cookies SQLite. We copy each database first because the live browser may keep
the file locked. Only grok.com domains are selected and cookie values never
leave this process.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional


class ChromeCookieError(Exception):
    """Typed Chrome import failure. ``reason`` is a kebab-case unavailable_reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Chrome expires_utc is microseconds since 1601-01-01 UTC.
_CHROME_EPOCH_DELTA_S = 11_644_473_600
_CHROME_KEY_SALT = b"saltysalt"
_CHROME_KEY_ITERS = 1003
_CHROME_CBC_IV = b" " * 16


def _firefox_cookie_dbs() -> list[Path]:
    roots: list[Path] = []
    # Windows: %APPDATA%\Mozilla\Firefox\Profiles
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Mozilla" / "Firefox" / "Profiles")
    # macOS: ~/Library/Application Support/Firefox/Profiles
    roots.append(Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles")
    # Linux: ~/.mozilla/firefox
    roots.append(Path.home() / ".mozilla" / "firefox")
    dbs: list[Path] = []
    for root in roots:
        dbs.extend(Path(p) for p in glob.glob(str(root / "*" / "cookies.sqlite")))
    return dbs


def load_firefox_grok_cookies() -> Optional[str]:
    now = int(time.time())
    for source in _firefox_cookie_dbs():
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as fh:
                temp_name = fh.name
            shutil.copy2(source, temp_name)
            conn = sqlite3.connect(temp_name)
            try:
                rows = conn.execute(
                    """
                    SELECT name, value
                    FROM moz_cookies
                    WHERE (host = 'grok.com' OR host LIKE '%.grok.com')
                      AND (expiry = 0 OR expiry > ?)
                    ORDER BY host, path, name
                    """,
                    (now,),
                ).fetchall()
            finally:
                conn.close()
            pairs = []
            seen = set()
            for name, value in rows:
                if not name or name in seen:
                    continue
                seen.add(name)
                pairs.append(f"{name}={value}")
            if pairs:
                return "; ".join(pairs)
        except (OSError, sqlite3.Error):
            continue
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
    return None


def _chrome_user_data_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()
    roots.append(home / "Library" / "Application Support" / "Google" / "Chrome")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Google" / "Chrome" / "User Data")
    roots.append(home / ".config" / "google-chrome")
    found: list[Path] = []
    for path in roots:
        try:
            if path.exists():
                found.append(path)
        except OSError:
            continue
    return found


def _chrome_last_used_profile(root: Path) -> Optional[str]:
    local_state = root / "Local State"
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    profile = data.get("profile") if isinstance(data, dict) else None
    if not isinstance(profile, dict):
        return None
    last = profile.get("last_used") or profile.get("last_used_profile_directory")
    return last if isinstance(last, str) and last else None


def chrome_profile_dirs(root: Path) -> list[Path]:
    names: list[str] = []
    last = _chrome_last_used_profile(root)
    if last:
        names.append(last)
    names.append("Default")
    try:
        extra = sorted(
            p.name
            for p in root.iterdir()
            if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile "))
        )
    except OSError:
        extra = [f"Profile {i}" for i in range(1, 9)]
    ordered: list[Path] = []
    seen = set()
    for name in names + extra:
        if name in seen:
            continue
        seen.add(name)
        path = root / name
        try:
            if path.is_dir():
                ordered.append(path)
        except OSError:
            continue
    return ordered


def chrome_cookie_dbs() -> list[Path]:
    dbs: list[Path] = []
    seen: set[Path] = set()
    for root in _chrome_user_data_roots():
        for profile in chrome_profile_dirs(root):
            for candidate in (
                profile / "Network" / "Cookies",
                profile / "Cookies",
            ):
                try:
                    if candidate.is_file() and candidate not in seen:
                        seen.add(candidate)
                        dbs.append(candidate)
                except OSError:
                    continue
    return dbs


def _chrome_safe_storage_password() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-w",
                "-s",
                "Chrome Safe Storage",
                "-a",
                "Chrome",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    password = (completed.stdout or "").strip()
    return password or None


def _derive_chrome_key(password: str) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError:
        raise ChromeCookieError("chrome-crypto-missing") from None
    return PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=_CHROME_KEY_SALT,
        iterations=_CHROME_KEY_ITERS,
    ).derive(password.encode("utf-8"))


def decrypt_chrome_cookie_value(key: bytes, blob: bytes) -> str:
    if blob.startswith(b"v20"):
        raise ValueError("chrome-app-bound")
    if not blob.startswith((b"v10", b"v11")):
        raise ValueError("chrome-unknown-prefix")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise ChromeCookieError("chrome-crypto-missing") from None
    ciphertext = blob[3:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(_CHROME_CBC_IV)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        raise ValueError("chrome-decrypt-failed")
    pad = padded[-1]
    if pad < 1 or pad > 16 or padded[-pad:] != bytes([pad] * pad):
        raise ValueError("chrome-decrypt-failed")
    return padded[:-pad].decode("utf-8")


def _chrome_expires_ok(expires_utc: object, now: float) -> bool:
    if expires_utc in (None, 0):
        return True
    try:
        expiry = int(str(expires_utc))
    except (TypeError, ValueError):
        return False
    if expiry <= 0:
        return True
    unix = (expiry / 1_000_000) - _CHROME_EPOCH_DELTA_S
    return unix > now


def _is_tcc_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 1:
        return True
    return "not permitted" in str(exc).lower()


def load_chrome_grok_cookies() -> Optional[str]:
    try:
        sources = chrome_cookie_dbs()
    except OSError as exc:
        if _is_tcc_error(exc):
            raise ChromeCookieError("chrome-tcc-denied") from None
        return None

    now = time.time()
    saw_tcc = False
    saw_grok_rows = False
    decrypt_reason: Optional[str] = None

    for source in sources:
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as fh:
                temp_name = fh.name
            shutil.copy2(source, temp_name)
            conn = sqlite3.connect(temp_name)
            try:
                rows = conn.execute(
                    """
                    SELECT name, encrypted_value, expires_utc
                    FROM cookies
                    WHERE host_key = 'grok.com' OR host_key LIKE '%.grok.com'
                    ORDER BY host_key, name
                    """
                ).fetchall()
            finally:
                conn.close()
        except OSError as exc:
            if _is_tcc_error(exc):
                saw_tcc = True
            continue
        except sqlite3.Error:
            continue
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

        live_rows = []
        for name, blob, expires_utc in rows:
            if not name or not _chrome_expires_ok(expires_utc, now):
                continue
            if not isinstance(blob, (bytes, bytearray)):
                continue
            live_rows.append((str(name), bytes(blob)))
        if not live_rows:
            continue
        saw_grok_rows = True

        password = _chrome_safe_storage_password()
        if not password:
            raise ChromeCookieError("chrome-keychain-denied")
        key = _derive_chrome_key(password)

        pairs: list[str] = []
        seen: set[str] = set()
        for name, blob in live_rows:
            if name in seen:
                continue
            try:
                value = decrypt_chrome_cookie_value(key, blob)
            except ChromeCookieError:
                raise
            except ValueError as exc:
                reason = str(exc) if str(exc).startswith("chrome-") else "chrome-decrypt-failed"
                decrypt_reason = reason
                continue
            except Exception:
                decrypt_reason = "chrome-decrypt-failed"
                continue
            if not value:
                continue
            seen.add(name)
            pairs.append(f"{name}={value}")
        if pairs:
            return "; ".join(pairs)

    if decrypt_reason:
        raise ChromeCookieError(decrypt_reason)
    if saw_tcc and not saw_grok_rows:
        raise ChromeCookieError("chrome-tcc-denied")
    if not sources:
        roots = _chrome_user_data_roots()
        if roots:
            probe = roots[0] / "Default" / "Cookies"
            try:
                probe.open("rb").close()
            except OSError as exc:
                if _is_tcc_error(exc):
                    raise ChromeCookieError("chrome-tcc-denied") from None
    return None
