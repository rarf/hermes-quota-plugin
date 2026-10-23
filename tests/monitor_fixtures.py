"""Synthetic Hermes homes for the quota monitor tests (stdlib only, no real data).

The tables mirror the columns Hermes' own ``state.db`` / ``kanban.db`` carry (the subset the monitor reads plus
their neighbours), so a query that drifts from the real schema fails here first.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# hermes_constants is a Hermes runtime module: stub it with the env-driven resolution Hermes itself uses, so the
# tests never touch the real HERMES_HOME.
_stub = types.ModuleType("hermes_constants")
_stub.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME") or tempfile.gettempdir())
sys.modules.setdefault("hermes_constants", _stub)

PKG = "quota_plugin_monitor_under_test"
if PKG not in sys.modules:
    _pkg = types.ModuleType(PKG)
    _pkg.__path__ = [str(ROOT)]
    sys.modules[PKG] = _pkg


def load(name: str):
    return importlib.import_module("%s.%s" % (PKG, name))


STATE_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, model TEXT, model_config TEXT,
    parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
    message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0, cwd TEXT, billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT,
    estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT, title TEXT,
    last_activity_at REAL, last_activity_description TEXT, api_call_count INTEGER DEFAULT 0, profile_name TEXT,
    archived INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id), role TEXT NOT NULL,
    content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL, token_count INTEGER,
    finish_reason TEXT, reasoning TEXT, reasoning_content TEXT, _compressed_summary INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0, api_content TEXT, display_kind TEXT
);
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '', billing_mode TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0, cost_status TEXT,
    cost_source TEXT, first_seen REAL, last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);
CREATE TABLE session_turn_leases (
    conversation_id TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at REAL NOT NULL, expires_at REAL NOT NULL
);
"""

KANBAN_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, status TEXT NOT NULL,
    priority INTEGER DEFAULT 0, created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
    completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch', consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER, last_failure_error TEXT, last_heartbeat_at INTEGER, session_id TEXT, block_kind TEXT
);
"""


class Home:
    """A temp Hermes root (+ optional profiles) with synthetic state.db / kanban.db files."""

    def __init__(self, profiles: Iterable[str] = ()) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "hermes"
        self.root.mkdir()
        self.cli_home = Path(self._tmp.name) / "user"
        self.cli_home.mkdir()
        self.homes = {"default": self.root}
        for p in profiles:
            h = self.root / "profiles" / p
            h.mkdir(parents=True)
            self.homes[p] = h
        self._prev = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.root)
        self.clis = load("quota_monitor.clis")
        self.clis.set_home(self.cli_home)

    def close(self) -> None:
        if self._prev is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._prev
        self._tmp.cleanup()

    # ---- state.db ----
    @contextlib.contextmanager
    def state(self, profile: str = "default") -> Iterator[sqlite3.Connection]:
        db = self.homes[profile] / "state.db"
        fresh = not db.exists()
        c = sqlite3.connect(str(db))
        try:
            with c:
                if fresh:
                    c.executescript(STATE_SCHEMA)
                yield c
        finally:
            c.close()

    def session(self, sid: str, source: str = "desktop", profile: str = "default", *, started: float,
                last: Optional[float] = None, title: str = "synthetic task", model: str = "model-a",
                provider: str = "anthropic", parent: Optional[str] = None, ended: Optional[float] = None,
                act: Optional[str] = None, tokens: int = 1000, cost: float = 0.5, tools: int = 0) -> None:
        with self.state(profile) as c:
            c.execute("""INSERT INTO sessions (id, source, model, parent_session_id, started_at, ended_at, title,
                         last_activity_at, last_activity_description, input_tokens, output_tokens,
                         estimated_cost_usd, billing_provider, tool_call_count, message_count)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (sid, source, model, parent, started, ended, title, last, act, tokens // 2, tokens // 2,
                       cost, provider, tools, 0))

    def message(self, sid: str, role: str, ts: float, content: str = "hello", tool_calls: Any = None,
                profile: str = "default", display_kind: Optional[str] = None) -> None:
        with self.state(profile) as c:
            c.execute("""INSERT INTO messages (session_id, role, content, tool_calls, timestamp, display_kind)
                         VALUES (?,?,?,?,?,?)""",
                      (sid, role, content, json.dumps(tool_calls) if tool_calls is not None else None, ts,
                       display_kind))

    def usage_row(self, sid: str, *, model: str = "model-a", provider: str = "anthropic", tokens: int = 1000,
                  cost: float = 0.5, calls: int = 1, last_seen: float, profile: str = "default") -> None:
        with self.state(profile) as c:
            c.execute("""INSERT OR REPLACE INTO session_model_usage (session_id, model, billing_provider,
                         api_call_count, input_tokens, output_tokens, estimated_cost_usd, first_seen, last_seen)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (sid, model, provider, calls, tokens // 2, tokens - tokens // 2, cost, last_seen - 60,
                       last_seen))

    def lease(self, sid: str, pid: int, profile: str = "default") -> None:
        path = self.homes[profile] / "runtime" / "active_sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text()) if path.exists() else {"entries": []}
        data["entries"].append({"session_id": sid, "pid": pid, "surface": "desktop", "started_at": time.time()})
        path.write_text(json.dumps(data))

    # ---- kanban.db ----
    def card(self, cid: str, status: str, *, title: str = "synthetic card", assignee: str = "work",
             created: Optional[float] = None, started: Optional[float] = None, completed: Optional[float] = None,
             failures: int = 0, block_kind: Optional[str] = None, worker_pid: Optional[int] = None,
             board: Optional[str] = None) -> None:
        db = self.root / "kanban.db" if not board else self.root / "kanban" / "boards" / board / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        fresh = not db.exists()
        c = sqlite3.connect(str(db))
        with c:
            if fresh:
                c.executescript(KANBAN_SCHEMA)
            c.execute("""INSERT INTO tasks (id, title, assignee, status, created_at, started_at, completed_at,
                         consecutive_failures, block_kind, worker_pid) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (cid, title, assignee, status, int(created or time.time()), started, completed, failures,
                       block_kind, worker_pid))
        c.close()

    # ---- plugin files ----
    def quota_cache(self, providers: Dict[str, Any], fetched_at: str) -> None:
        (self.root / "quota_cache.json").write_text(json.dumps({"fetched_at": fetched_at, "providers": providers}))

    def write(self, rel: str, text: str, profile: str = "default") -> Path:
        path = self.homes[profile] / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


def iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def anthropic_record(session_used: float, week_used: float, reset_session: float, reset_week: float) -> Dict[str, Any]:
    return {"label": "anthropic", "plan": None, "unavailable_reason": None, "details": [],
            "windows": [{"label": "Current session", "used_percent": session_used, "reset_at": iso(reset_session)},
                        {"label": "Current week", "used_percent": week_used, "reset_at": iso(reset_week)}]}


def walk_keys(o: Any) -> List[str]:
    out: List[str] = []
    if isinstance(o, dict):
        for k, v in o.items():
            out.append(str(k))
            out += walk_keys(v)
    elif isinstance(o, list):
        for v in o:
            out += walk_keys(v)
    return out
