"""Agent CLI usage: Claude Code, GitHub Copilot CLI (+ presence of other CLIs).

Ported from Agent Monitor's clis.py. Read-only. Claude Code transcripts are parsed incrementally (only bytes
appended since the last read) within one process; a missing ~/.claude or ~/.copilot (Linux, no CLI installed)
yields empty results, never an error.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .common import pid_alive  # noqa: F401  (re-exported for callers)

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude" / "projects"
CLAUDE_SESSIONS = HOME / ".claude" / "sessions"  # one <pid>.json per open Claude Code process
COPILOT_DB = HOME / ".copilot" / "session-store.db"


def set_home(home):
    """Point every CLI path at another home (tests)."""
    global HOME, CLAUDE_DIR, CLAUDE_SESSIONS, COPILOT_DB, COPILOT_STATE
    HOME = Path(home)
    CLAUDE_DIR = HOME / ".claude" / "projects"
    CLAUDE_SESSIONS = HOME / ".claude" / "sessions"
    COPILOT_DB = HOME / ".copilot" / "session-store.db"
    COPILOT_STATE = HOME / ".copilot" / "session-state"
    with _claude_lock:
        _claude_cache.clear()
RUNNING_WINDOW = 120  # a CLI session written to in the last N seconds counts as running
# Copilot CLI keeps no process registry that we could verify on this machine (~/.copilot is absent here), so an
# "open" Copilot session is inferred: written to within COPILOT_OPEN_WINDOW, or (best effort, unverified) a live
# `inuse.<pid>.lock` file in its session-state folder. It is not dropped after RUNNING_WINDOW (2 min).
COPILOT_OPEN_WINDOW = 900
COPILOT_STATE = HOME / ".copilot" / "session-state"

SOURCES = {
    "claude-code": "Claude Code",
    "copilot-cli": "Copilot CLI",
    "gemini-cli": "Gemini / Antigravity",
    "droid": "Factory Droid",
}


def _utc_iso(ts):
    """Naive UTC ISO string (the format Copilot's session store uses); utcfromtimestamp is deprecated."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None).isoformat()


def _iso_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------- pricing (Hermes' own table; linear, so rates are cached per model) ----------

_rates = {}
_rates_lock = threading.Lock()


def _model_rates(model, provider):
    key = (model, provider)
    with _rates_lock:
        if key in _rates:
            return _rates[key]
    rates = None
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
        per = []
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            r = estimate_usage_cost(model, CanonicalUsage(**{field: 1_000_000}), provider=provider)
            per.append(float(r.amount_usd) / 1e6 if r.amount_usd is not None else None)
        rates = None if all(p is None for p in per) else [p or 0.0 for p in per]
    except Exception:
        rates = None
    with _rates_lock:
        _rates[key] = rates
    return rates


def _cost(ev, provider="anthropic"):
    r = _model_rates(ev["model"], provider)
    if not r:
        return 0.0
    return ev["input"] * r[0] + ev["output"] * r[1] + ev["cache_read"] * r[2] + ev["cache_write"] * r[3]


# ---------- Claude Code ----------

WORKING_WINDOW = 90  # a session with output in the last N seconds is "working" (when no busy flag exists)


def claude_open_sessions():
    """{session id: {"status", "waiting_for", "since"}} of Claude Code processes that are still alive, or None if
    the registry is missing. status: busy (working on a turn), waiting (permission prompt or dialog, reason in
    waiting_for), idle / shell (turn finished, waiting for a new prompt); since = last status change."""
    if not CLAUDE_SESSIONS.is_dir():
        return None
    out = {}
    for p in CLAUDE_SESSIONS.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("sessionId") and pid_alive(d.get("pid")):
            since = d.get("statusUpdatedAt")
            out[d["sessionId"]] = {"status": d.get("status"), "waiting_for": d.get("waitingFor"),
                                   "since": since / 1000 if isinstance(since, (int, float)) else None,
                                   # where the process lives: kind "bg" = hosted by Claude Code's daemon (no window)
                                   "pid": d.get("pid"), "kind": d.get("kind"), "job": d.get("jobId")}
    return out


def loop_max(tools, since):
    """Longest run of identical consecutive tool calls (same name and input) after `since`."""
    best = run = 0
    prev = None
    for ts, key in tools:
        if ts < since:
            continue
        run = run + 1 if key == prev else 1
        prev = key
        best = max(best, run)
    return best


def claude_turns(since):
    """(model, seconds) of main-session turns finished after `since` (from transcripts already parsed)."""
    with _claude_lock:
        return [(m, sec) for cf in _claude_cache.values() if not cf.subagent
                for ts, sec, m in cf.turns if ts >= since and m]


class _ClaudeFile:
    __slots__ = ("offset", "events", "msg_index", "title", "cwd", "session", "first", "last", "subagent", "parent",
                 "ctx", "tools", "tool_ids", "turns")

    def __init__(self):
        self.offset = 0
        self.events = []
        self.msg_index = {}
        self.title = None
        self.cwd = None
        self.session = None
        self.first = None
        self.last = None
        self.subagent = False
        self.parent = None
        self.ctx = None        # prompt size of the latest request (input + cache read + cache write)
        self.tools = []        # recent tool calls: (ts, "name + input json")
        self.tool_ids = set()
        self.turns = []        # finished turns: (ts, seconds, model) from the CLI's own turn_duration lines


_claude_cache = {}
_claude_lock = threading.Lock()


def _user_text(msg):
    c = msg.get("content")
    if isinstance(c, list):
        c = next((b.get("text") for b in c if isinstance(b, dict) and b.get("type") == "text"), None)
    if isinstance(c, str):
        c = c.strip()
        if c and not c.startswith("<") and not c.startswith("[Request interrupted"):
            return c.splitlines()[0][:140]
    return None


def _read_claude_file(path: Path, st):
    cf = _claude_cache.get(path)
    if cf is None or st.st_size < cf.offset:
        cf = _ClaudeFile()
        cf.subagent = path.parent.name == "subagents"
        if cf.subagent:
            cf.parent = path.parent.parent.name  # parent session id
        _claude_cache[path] = cf
    if st.st_size == cf.offset:
        return cf
    with open(path, "rb") as f:
        f.seek(cf.offset)
        chunk = f.read(st.st_size - cf.offset)
    end = chunk.rfind(b"\n")
    if end < 0:
        return cf
    cf.offset += end + 1
    for raw in chunk[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        ts = _iso_ts(d.get("timestamp"))
        if ts:
            cf.first = cf.first or ts
            cf.last = max(cf.last or 0, ts)
        cf.session = cf.session or d.get("sessionId")
        cf.cwd = cf.cwd or d.get("cwd")
        t = d.get("type")
        if t == "system" and d.get("subtype") == "turn_duration" and isinstance(d.get("durationMs"), (int, float)):
            cf.turns.append((ts or 0, d["durationMs"] / 1000, cf.events[-1]["model"] if cf.events else None))
            cf.turns = cf.turns[-500:]
            continue
        if t == "system" and d.get("subtype") == "compact_boundary":
            cf.ctx = None  # the next request reports the compacted size
            continue
        if t in ("summary", "custom-title", "ai-title"):
            cf.title = d.get("summary") or d.get("customTitle") or d.get("title") or cf.title
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        if t == "user" and not cf.title and not d.get("isMeta"):
            cf.title = _user_text(msg)
        if t == "assistant" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in cf.tool_ids:
                    cf.tool_ids.add(b.get("id"))
                    cf.tools.append((ts or 0, f"{b.get('name')} {json.dumps(b.get('input'), sort_keys=True)}"))
            if len(cf.tools) > 300:
                cf.tools = cf.tools[-200:]
                cf.tool_ids = set(list(cf.tool_ids)[-400:])
        u = msg.get("usage")
        if t != "assistant" or not isinstance(u, dict) or not msg.get("model") or msg.get("model") == "<synthetic>":
            continue
        cf.ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        ev = {"ts": ts or 0, "model": msg["model"],
              "input": u.get("input_tokens") or 0, "output": u.get("output_tokens") or 0,
              "cache_read": u.get("cache_read_input_tokens") or 0,
              "cache_write": u.get("cache_creation_input_tokens") or 0}
        ev["cost"] = _cost(ev)
        mid = msg.get("id") or d.get("requestId")
        # a streamed message is written once per content block with the same usage: keep one
        if mid and mid in cf.msg_index:
            cf.events[cf.msg_index[mid]] = ev
        else:
            if mid:
                cf.msg_index[mid] = len(cf.events)
            cf.events.append(ev)
    return cf


def claude_sessions(since, always=()):
    """Every Claude Code transcript (main + subagent) touched since `since`, plus those of the session ids in
    `always` (open windows stay listed however long they have been quiet)."""
    out = []
    if not CLAUDE_DIR.is_dir():
        return out
    with _claude_lock:
        for proj in CLAUDE_DIR.iterdir():
            if not proj.is_dir():
                continue
            paths = list(proj.glob("*.jsonl")) + list(proj.glob("*/subagents/*.jsonl"))
            for p in paths:
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime < since and p.stem not in always and p.parent.parent.name not in always:
                    continue
                try:
                    cf = _read_claude_file(p, st)
                except OSError:
                    continue
                out.append((p, st.st_mtime, cf))
    return out


# ---------- Copilot CLI ----------

def copilot_events(start, end):
    if not COPILOT_DB.exists():
        return [], {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % quote(str(COPILOT_DB)), uri=True, timeout=3)
        c.row_factory = sqlite3.Row
        with closing(c):
            rows = c.execute(
                """SELECT e.session_id, e.model, e.agent_id, e.parent_tool_call_id,
                          coalesce(e.input_tokens,0) inp, coalesce(e.output_tokens,0) outp,
                          coalesce(e.cache_read_tokens,0) cr, coalesce(e.cache_write_tokens,0) cw,
                          coalesce(e.total_nano_aiu,0) aiu, coalesce(e.request_multiplier,0) mult, e.created_at
                   FROM assistant_usage_events e
                   WHERE e.created_at >= ? AND e.created_at < ?""",
                (_utc_iso(start), _utc_iso(end))).fetchall()
            sessions = {r["id"]: dict(r) for r in c.execute(
                "SELECT id, cwd, repository, branch, summary, created_at, updated_at FROM sessions")}
    except sqlite3.Error:
        return [], {}
    events = []
    for r in rows:
        ev = {"ts": _iso_ts(r["created_at"]) or 0, "model": r["model"], "session": r["session_id"],
              "subagent": bool(r["agent_id"] or r["parent_tool_call_id"]),
              "input": max(0, r["inp"] - r["cr"]), "output": r["outp"], "cache_read": r["cr"],
              "cache_write": r["cw"], "aiu": r["aiu"] / 1e9, "premium": r["mult"]}
        ev["cost"] = _cost(ev)
        events.append(ev)
    return events, sessions


def _copilot_lock_alive(sid):
    """Best effort (unverified here): Copilot CLI is believed to leave `inuse.<pid>.lock` in its session folder."""
    try:
        for f in (COPILOT_STATE / sid).glob("inuse.*.lock"):
            if pid_alive(f.name.split(".")[1]):
                return True
    except (OSError, IndexError):
        pass
    return False


# ---------- presence of other CLIs ----------

def _latest_mtime(root: Path, depth=3):
    best = None
    try:
        stack = [(root, 0)]
        while stack:
            d, lvl = stack.pop()
            for e in os.scandir(d):
                try:
                    m = e.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                best = max(best or 0, m)
                if e.is_dir(follow_symlinks=False) and lvl < depth:
                    stack.append((Path(e.path), lvl + 1))
    except OSError:
        pass
    return best


def presence():
    items = []
    for key, binary, root in (("claude-code", "claude", HOME / ".claude" / "projects"),
                              ("copilot-cli", "copilot", HOME / ".copilot"),
                              ("gemini-cli", "gemini", HOME / ".gemini"),
                              ("droid", "droid", HOME / ".factory")):
        installed = bool(shutil.which(binary)) or (HOME / ".local" / "bin" / binary).exists()
        if not installed and not root.exists():
            continue
        items.append({"source": key, "name": SOURCES[key], "installed": installed,
                      "tracked": key in ("claude-code", "copilot-cli"),
                      "last_activity": _latest_mtime(root, 2) if root.exists() else None})
    return items


# ---------- aggregation ----------

def _day(ts):
    return datetime.fromtimestamp(ts).date().isoformat()


def collect(start, end):
    """Normalized per-session rows + per-event list for [start, end)."""
    now = time.time()
    sessions = []
    events = []  # (ts, source, model, tokens, cost, subagent)
    opened = claude_open_sessions()

    for path, mtime, cf in claude_sessions(start, always=opened or ()):
        evs = [e for e in cf.events if start <= e["ts"] < end]
        # a closed terminal keeps the transcript mtime fresh for a while: the process registry is authoritative
        owner = cf.parent if cf.subagent else cf.session
        is_open = opened is None or owner in opened
        registered = bool(opened) and owner in opened and not cf.subagent  # open window, however quiet
        if not evs and not (now - mtime < RUNNING_WINDOW) and not registered:
            continue
        tok = sum(e["input"] + e["output"] for e in evs)
        sid = cf.session if not cf.subagent else path.stem
        reg = (opened or {}).get(owner) or {}
        status = reg.get("status")
        # working = busy on a turn (registry flag), else wrote to its transcript in the last WORKING_WINDOW
        working = is_open and (status == "busy" if status and not cf.subagent else now - mtime < WORKING_WINDOW)
        if cf.subagent or not status:
            state, since = ("working" if working else "waiting"), None
        else:
            state = {"busy": "working", "waiting": "needs_approval"}.get(status, "waiting")
            since = reg.get("since")
        sessions.append({
            "source": "claude-code", "id": sid, "parent": cf.parent if cf.subagent else None,
            "pid": None if cf.subagent else reg.get("pid"), "background": reg.get("kind") == "bg",
            "job": reg.get("job"),
            "subagent": cf.subagent, "title": cf.title, "project": cf.cwd,
            "model": evs[-1]["model"] if evs else (cf.events[-1]["model"] if cf.events else None),
            "started": cf.first, "last": max(cf.last or 0, mtime) if now - mtime < RUNNING_WINDOW else cf.last,
            # a main session whose process is alive stays listed while it waits for you (the registry says so);
            # without a registry entry fall back to "wrote recently"
            "running": is_open and ((bool(status) and not cf.subagent) or now - mtime < RUNNING_WINDOW),
            "working": working,
            "state": state, "state_since": since,
            "waiting_for": reg.get("waiting_for") if state == "needs_approval" else None,
            "context_tokens": cf.ctx, "context_estimated": False,
            "loop_max": loop_max(cf.tools, now - 1800),
            "tokens": tok, "input": sum(e["input"] for e in evs), "output": sum(e["output"] for e in evs),
            "cache_read": sum(e["cache_read"] for e in evs), "cost": sum(e["cost"] for e in evs), "calls": len(evs),
            "total_tokens": sum(e["input"] + e["output"] for e in cf.events),
            "total_cost": sum(e["cost"] for e in cf.events),
        })
        events += [(e["ts"], "claude-code", e["model"], e["input"] + e["output"], e["cost"], cf.subagent) for e in evs]

    cp_events, cp_sessions = copilot_events(start, end)
    by_sess = defaultdict(list)
    for e in cp_events:
        by_sess[e["session"]].append(e)
        events.append((e["ts"], "copilot-cli", e["model"], e["input"] + e["output"], e["cost"], e["subagent"]))
    for sid, evs in by_sess.items():
        meta = cp_sessions.get(sid, {})
        last = _iso_ts(meta.get("updated_at")) or max(e["ts"] for e in evs)
        is_open = now - last < COPILOT_OPEN_WINDOW or _copilot_lock_alive(sid)
        sessions.append({
            "source": "copilot-cli", "id": sid, "parent": None, "subagent": False,
            "title": meta.get("summary"), "project": meta.get("cwd") or meta.get("repository"),
            "model": evs[-1]["model"], "started": _iso_ts(meta.get("created_at")) or evs[0]["ts"], "last": last,
            "running": is_open,
            "working": now - last < WORKING_WINDOW,
            "state": "working" if now - last < WORKING_WINDOW else "waiting", "state_since": last,
            "waiting_for": None, "context_tokens": None, "context_estimated": False, "loop_max": 0,
            "tokens": sum(e["input"] + e["output"] for e in evs), "input": sum(e["input"] for e in evs),
            "output": sum(e["output"] for e in evs), "cache_read": sum(e["cache_read"] for e in evs),
            "cost": sum(e["cost"] for e in evs), "calls": len(evs),
            "subagent_calls": sum(e["subagent"] for e in evs),
            "premium": sum(e["premium"] for e in evs), "aiu": sum(e["aiu"] for e in evs),
        })
        sessions[-1]["total_tokens"], sessions[-1]["total_cost"] = sessions[-1]["tokens"], sessions[-1]["cost"]
    return sessions, events


def summary(start, end, days):
    sessions, events = collect(start, end)
    by_source = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "calls": 0, "sessions": 0, "subagents": 0,
                                     "input": 0, "output": 0, "cache_read": 0})
    for s in sessions:
        b = by_source[s["source"]]
        b["sessions"] += 0 if s["subagent"] else 1
        b["subagents"] += 1 if s["subagent"] else 0
        for k in ("tokens", "cost", "calls", "input", "output", "cache_read"):
            b[k] += s[k]
    daily = defaultdict(lambda: defaultdict(int))
    daily_cost = defaultdict(lambda: defaultdict(float))
    by_model = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "calls": 0, "source": None})
    for ts, src, model, tok, cost, _ in events:
        d = _day(ts)
        daily[d][src] += tok
        daily_cost[d][src] += cost
        m = by_model[(src, model)]
        m["tokens"] += tok; m["cost"] += cost; m["calls"] += 1; m["source"] = src
    return {
        "sources": [{"source": k, "name": SOURCES[k], **v} for k, v in
                    sorted(by_source.items(), key=lambda kv: -kv[1]["tokens"])],
        "daily": [{"day": d, "tokens": dict(daily.get(d, {})), "cost": dict(daily_cost.get(d, {}))} for d in days],
        "by_model": sorted(({"model": m, **v} for (_, m), v in by_model.items()), key=lambda x: -x["tokens"]),
        "sessions": sorted(sessions, key=lambda s: -(s["last"] or 0)),
        "presence": presence(),
    }


def live(minute_starts):
    """Per-minute CLI tokens (exact, from event timestamps) + running sessions."""
    start = minute_starts[0]
    sessions, events = collect(start - 900, time.time() + 60)
    per_min = {m: defaultdict(lambda: {"tokens": 0, "cost": 0.0}) for m in minute_starts}
    for ts, src, _, tok, cost, _ in events:
        m = int(ts // 60) * 60
        if m in per_min:
            per_min[m][src]["tokens"] += tok
            per_min[m][src]["cost"] += cost
    now = time.time()
    active = [s for s in sessions if s["running"] or (s["subagent"] and (s["last"] or 0) > now - 900)]
    return per_min, active
