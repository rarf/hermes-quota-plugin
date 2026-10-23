"""Shared helpers for the quota monitor: Hermes paths, read-only sqlite, small parsers.

Every Hermes file is opened read-only (sqlite ``mode=ro`` URIs, plain reads for json/yaml). The only
files this package writes are its own ``quota_monitor.db`` / ``quota_monitor.json`` (see ``store``).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

MONITOR_VERSION = 1


# ---------- Hermes paths ----------

def hermes_home() -> Path:
    """The active Hermes home (a profile dir when Hermes runs with ``--profile``)."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        env = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(env).expanduser() if env else Path.home() / ".hermes"


def hermes_root(home: Optional[Path] = None) -> Path:
    """The Hermes root that holds ``profiles/``: ``<root>/profiles/<name>`` -> ``<root>``."""
    home = Path(home) if home is not None else hermes_home()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def profile_homes(root: Optional[Path] = None) -> List[Tuple[str, Path]]:
    """[("default", root), (name, root/profiles/name), ...] — every profile on this machine."""
    root = Path(root) if root is not None else hermes_root()
    out = [("default", root)]
    profiles = root / "profiles"
    try:
        if profiles.is_dir():
            for p in sorted(profiles.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    out.append((p.name, p))
    except OSError:
        pass
    return out


def kanban_dbs(root: Optional[Path] = None) -> List[Tuple[str, Path]]:
    root = Path(root) if root is not None else hermes_root()
    dbs = [("default", root / "kanban.db")]
    boards = root / "kanban" / "boards"
    try:
        if boards.is_dir():
            dbs += [(b.name, b / "kanban.db") for b in sorted(boards.iterdir()) if (b / "kanban.db").exists()]
    except OSError:
        pass
    return dbs


# ---------- sqlite ----------

@contextmanager
def ro(db: Path) -> Iterator[sqlite3.Connection]:
    """Read-only connection that is always closed (sqlite3's own ``with`` only commits)."""
    conn = sqlite3.connect("file:%s?mode=ro" % quote(str(db)), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def table_cols(c: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}


# ---------- small parsers ----------

def num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def iso_ts(s: Any) -> Optional[float]:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def day_range(d_from: date, d_to: date) -> Tuple[float, float, List[str]]:
    """Local [start, end) timestamps for the inclusive day range + the list of ISO days."""
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    start = datetime.combine(d_from, datetime.min.time()).timestamp()
    end = datetime.combine(d_to + timedelta(days=1), datetime.min.time()).timestamp()
    days = [(d_from + timedelta(days=i)).isoformat() for i in range((d_to - d_from).days + 1)]
    return start, end, days


def read_config(home: Path) -> Dict[str, Any]:
    """A profile's config.yaml (read-only), {} when unreadable or no YAML parser is available."""
    try:
        text = (home / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        from ruamel.yaml import YAML
        data = YAML(typ="safe").load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        # ImportError, or a ruamel that cannot load (e.g. its modules were re-imported next to stale copies and
        # the safe constructor lost its tags): try the next parser instead of reporting an empty config.
        pass
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    except Exception:
        return {}
    # No YAML library (a bare interpreter outside Hermes' venv): JSON is YAML,
    # else a small block-style subset that covers the keys the monitor reads.
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    try:
        data = _mini_yaml(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _mini_scalar(raw: str) -> Any:
    v = raw.strip()
    if not v or v in ("~", "null", "Null", "NULL"):
        return None
    if v[0] in "'\"" and len(v) >= 2 and v[-1] == v[0]:
        return v[1:-1]
    if v in ("true", "True", "TRUE"):
        return True
    if v in ("false", "False", "FALSE"):
        return False
    if v[0] in "[{":
        return _flow(v)
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _flow(text: str) -> Any:
    """A YAML flow collection (``{a: b, "c": [1, 2]}``): JSON plus plain unquoted scalars and keys.

    ruamel keeps a JSON-written config.yaml in flow style and adds new keys unquoted."""
    i = 0
    n = len(text)

    def ws() -> None:
        nonlocal i
        while i < n and text[i] in " \t\r\n":
            i += 1

    def scalar(stops: str) -> Any:
        nonlocal i
        ws()
        if i < n and text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            raw = text[i:j + 1]
            i = j + 1
            return json.loads(raw)
        if i < n and text[i] == "'":
            j = text.index("'", i + 1)
            raw = text[i + 1:j]
            i = j + 1
            return raw
        j = i
        while j < n and text[j] not in stops and not (text[j] == ":" and (j + 1 >= n or text[j + 1] in " \t,]}")):
            j += 1
        raw = text[i:j]
        i = j
        return _mini_scalar(raw) if raw.strip() else None

    def value() -> Any:
        nonlocal i
        ws()
        if i < n and text[i] == "{":
            i += 1
            out: Dict[str, Any] = {}
            ws()
            if i < n and text[i] == "}":
                i += 1
                return out
            while True:
                key = scalar(",}")
                ws()
                if i >= n or text[i] != ":":
                    raise ValueError("flow mapping without ':'")
                i += 1
                out[str(key)] = value()
                ws()
                if i < n and text[i] == ",":
                    i += 1
                    ws()
                    if i < n and text[i] == "}":
                        i += 1
                        return out
                    continue
                if i < n and text[i] == "}":
                    i += 1
                    return out
                raise ValueError("unterminated flow mapping")
        if i < n and text[i] == "[":
            i += 1
            items: List[Any] = []
            ws()
            if i < n and text[i] == "]":
                i += 1
                return items
            while True:
                items.append(value())
                ws()
                if i < n and text[i] == ",":
                    i += 1
                    ws()
                    if i < n and text[i] == "]":
                        i += 1
                        return items
                    continue
                if i < n and text[i] == "]":
                    i += 1
                    return items
                raise ValueError("unterminated flow sequence")
        return scalar(",]}")

    data = value()
    ws()
    if i != n:
        raise ValueError("trailing flow YAML")
    return data


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line.rstrip()


def _mini_yaml(text: str) -> Any:
    """Block mappings, ``- `` lists (scalars or mappings) and plain scalars; no anchors/flow/multiline."""
    body = "\n".join(_strip_comment(raw) for raw in text.splitlines()).strip()
    if body.startswith(("{", "[")):
        return _flow(body)
    lines = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if line.strip() and line.strip() not in ("---", "..."):
            lines.append((len(line) - len(line.lstrip(" ")), line.strip()))
    pos = 0

    def block(indent: int) -> Any:
        nonlocal pos
        if pos >= len(lines):
            return None
        if lines[pos][1].startswith("- ") or lines[pos][1] == "-":
            out_list: List[Any] = []
            while pos < len(lines) and lines[pos][0] == indent and (lines[pos][1].startswith("- ") or lines[pos][1] == "-"):
                item = lines[pos][1][1:].strip()
                pos += 1
                if not item:
                    out_list.append(block(lines[pos][0]) if pos < len(lines) and lines[pos][0] > indent else None)
                elif re.match(r"^[^'\"\s][^:]*:(\s|$)", item):
                    # "- key: value" opens a mapping whose other keys sit 2 deeper
                    lines.insert(pos, (indent + 2, item))
                    out_list.append(block(indent + 2))
                else:
                    out_list.append(_mini_scalar(item))
            return out_list
        out: Dict[str, Any] = {}
        while pos < len(lines) and lines[pos][0] == indent:
            content = lines[pos][1]
            m = re.match(r"^(['\"]?)(.+?)\1:(\s+(.*))?$", content)
            if not m:
                raise ValueError("unsupported YAML line")
            key, value = m.group(2), (m.group(4) or "").strip()
            pos += 1
            if value:
                out[key] = _mini_scalar(value)
            elif pos < len(lines) and (lines[pos][0] > indent or (lines[pos][0] == indent and lines[pos][1].startswith("-"))):
                out[key] = block(lines[pos][0])
            else:
                out[key] = None
        return out

    if not lines:
        return {}
    data = block(lines[0][0])
    if pos != len(lines):
        raise ValueError("unsupported YAML layout")
    return data


def read_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


def cost_expr(prefix: str = "") -> str:
    return "coalesce(%sactual_cost_usd, %sestimated_cost_usd, 0)" % (prefix, prefix)


def usage_cost_expr(prefix: str = "") -> str:
    """Cost of a ``session_model_usage`` row. Its cost columns are NOT NULL DEFAULT 0, so a plain coalesce would
    always pick ``actual_cost_usd`` = 0; 0 means "unknown" here and falls through to the session split."""
    return "coalesce(nullif(%sactual_cost_usd, 0), nullif(%sestimated_cost_usd, 0), 0)" % (prefix, prefix)


def session_costs(c: sqlite3.Connection, ids: Any = None) -> Dict[str, Tuple[float, int]]:
    """{session_id: (cost, tokens)} — model usage rows can carry no cost, so a session's cost is split across
    its models by token share."""
    sql = "SELECT id, %s cost, coalesce(input_tokens,0)+coalesce(output_tokens,0) tok FROM sessions" % cost_expr()
    params: List[Any] = []
    if ids is not None:
        params = list(ids)
        if not params:
            return {}
        out: Dict[str, Tuple[float, int]] = {}
        for i in range(0, len(params), 500):
            part = params[i:i + 500]
            for r in c.execute(sql + " WHERE id IN (%s)" % ",".join("?" * len(part)), part):
                out[r["id"]] = (r["cost"], r["tok"])
        return out
    return {r["id"]: (r["cost"], r["tok"]) for r in c.execute(sql)}


def row_cost(row_cost_value: Any, row_tok: int, sess: Optional[Tuple[float, int]]) -> float:
    if row_cost_value:
        return row_cost_value
    if not sess or not sess[0]:
        return 0.0
    cost, tok = sess
    return cost * (row_tok / tok) if tok else 0.0
