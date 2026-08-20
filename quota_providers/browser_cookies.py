"""Local browser-cookie helpers for the Grok quota provider.

Firefox stores cookies in cookies.sqlite. We copy the database first because
Firefox may keep the live file locked. Only grok.com domains are selected and
cookie values never leave this process.
"""
from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Optional


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
