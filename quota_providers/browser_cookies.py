"""Local browser-cookie helpers for the Grok quota provider.

Firefox stores cookies in cookies.sqlite. Chrome stores them encrypted in
Cookies SQLite. We copy each database first because the live browser may keep
the file locked. Only grok.com domains are selected and cookie values never
leave this process.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


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


@contextmanager
def _copied_sqlite(source: Path) -> Iterator[sqlite3.Connection]:
    fd, temp_name = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        shutil.copyfile(source, temp_name)
        conn = sqlite3.connect(temp_name)
        try:
            yield conn
        finally:
            conn.close()
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _cookie_header(pairs: Iterator[tuple[str, str]]) -> Optional[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name, value in pairs:
        if not name or not value or name in seen:
            continue
        seen.add(name)
        out.append(f"{name}={value}")
    return "; ".join(out) if out else None


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
        try:
            with _copied_sqlite(source) as conn:
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
        except (OSError, sqlite3.Error):
            continue
        header = _cookie_header((name, value) for name, value in rows)
        if header:
            return header
    return None


def _is_tcc_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 1:
        return True
    return "not permitted" in str(exc).lower()


def _chrome_user_data_roots() -> list[Path]:
    # Decrypt is macOS Keychain only. Do not scan Windows/Linux Chrome roots.
    if sys.platform != "darwin":
        return []
    path = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    try:
        return [path] if path.exists() else []
    except OSError as exc:
        if _is_tcc_error(exc):
            raise ChromeCookieError("chrome-tcc-denied") from None
        return []


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


def _safe_chrome_profile_dir(root: Path, name: str) -> Optional[Path]:
    """Join a Local State / listing name only if it stays inside ``root``."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    candidate = root / name
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    try:
        return candidate if candidate.is_dir() else None
    except OSError as exc:
        if _is_tcc_error(exc):
            raise ChromeCookieError("chrome-tcc-denied") from None
        return None


def chrome_profile_dirs(root: Path) -> list[Path]:
    names: list[str] = []
    last = _chrome_last_used_profile(root)
    if last:
        names.append(last)
    names.append("Default")
    saw_tcc = False
    try:
        extra = sorted(
            p.name
            for p in root.iterdir()
            if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile "))
        )
    except OSError as exc:
        extra = []
        if _is_tcc_error(exc):
            saw_tcc = True
    ordered: list[Path] = []
    seen = set()
    for name in names + extra:
        if name in seen:
            continue
        seen.add(name)
        try:
            path = _safe_chrome_profile_dir(root, name)
        except ChromeCookieError:
            raise
        except OSError as exc:
            if _is_tcc_error(exc):
                saw_tcc = True
            continue
        if path is not None:
            ordered.append(path)
    if not ordered and saw_tcc:
        raise ChromeCookieError("chrome-tcc-denied")
    return ordered


def chrome_cookie_dbs() -> list[Path]:
    dbs: list[Path] = []
    seen: set[Path] = set()
    saw_tcc = False
    for root in _chrome_user_data_roots():
        try:
            profiles = chrome_profile_dirs(root)
        except OSError as exc:
            if _is_tcc_error(exc):
                saw_tcc = True
            continue
        for profile in profiles:
            for candidate in (
                profile / "Network" / "Cookies",
                profile / "Cookies",
            ):
                try:
                    if candidate.is_file() and candidate not in seen:
                        seen.add(candidate)
                        dbs.append(candidate)
                except OSError as exc:
                    if _is_tcc_error(exc):
                        saw_tcc = True
    if not dbs and saw_tcc:
        raise ChromeCookieError("chrome-tcc-denied")
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


def _chrome_aes_key() -> bytes:
    password = _chrome_safe_storage_password()
    if not password:
        raise ChromeCookieError("chrome-keychain-denied")
    return _derive_chrome_key(password)


def _strip_chrome_host_hash(plaintext: bytes, host_key: Optional[str] = None) -> bytes:
    """Chrome 127+ / cookie DB v24+ prefixes SHA256(host_key) before the value."""
    if len(plaintext) < 32:
        return plaintext
    prefix, rest = plaintext[:32], plaintext[32:]
    if host_key is not None:
        expected = hashlib.sha256(host_key.encode("utf-8")).digest()
        if prefix == expected:
            return rest
    try:
        plaintext.decode("utf-8")
        return plaintext
    except UnicodeDecodeError:
        try:
            rest.decode("utf-8")
            return rest
        except UnicodeDecodeError:
            return plaintext


def decrypt_chrome_cookie_value(
    key: bytes, blob: bytes, host_key: Optional[str] = None
) -> str:
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
    plaintext = _strip_chrome_host_hash(padded[:-pad], host_key)
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("chrome-decrypt-failed") from exc


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


def _chrome_expiry_cutoff(now: float) -> int:
    return int((now + _CHROME_EPOCH_DELTA_S) * 1_000_000)


def load_chrome_grok_cookies() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    try:
        sources = chrome_cookie_dbs()
    except ChromeCookieError:
        raise
    except OSError as exc:
        if _is_tcc_error(exc):
            raise ChromeCookieError("chrome-tcc-denied") from None
        return None

    now = time.time()
    cutoff = _chrome_expiry_cutoff(now)
    key: Optional[bytes] = None
    decrypt_reason: Optional[str] = None

    for source in sources:
        try:
            with _copied_sqlite(source) as conn:
                rows = conn.execute(
                    """
                    SELECT name, host_key, encrypted_value, expires_utc
                    FROM cookies
                    WHERE (host_key = 'grok.com' OR host_key LIKE '%.grok.com')
                      AND (expires_utc IS NULL OR expires_utc <= 0 OR expires_utc > ?)
                    ORDER BY host_key, name
                    """,
                    (cutoff,),
                ).fetchall()
        except OSError as exc:
            if _is_tcc_error(exc):
                raise ChromeCookieError("chrome-tcc-denied") from None
            continue
        except sqlite3.Error:
            continue

        pairs: list[tuple[str, str]] = []
        for name, host_key, blob, expires_utc in rows:
            if not name or not _chrome_expires_ok(expires_utc, now):
                continue
            if not isinstance(blob, (bytes, bytearray)):
                continue
            if key is None:
                key = _chrome_aes_key()
            try:
                value = decrypt_chrome_cookie_value(key, bytes(blob), host_key=str(host_key or ""))
            except ChromeCookieError:
                raise
            except ValueError as exc:
                reason = str(exc) if str(exc).startswith("chrome-") else "chrome-decrypt-failed"
                decrypt_reason = reason
                continue
            except Exception:
                raise ChromeCookieError("chrome-decrypt-failed") from None
            if value:
                pairs.append((str(name), value))
        header = _cookie_header(iter(pairs))
        if header:
            return header

    if decrypt_reason:
        raise ChromeCookieError(decrypt_reason)
    return None
