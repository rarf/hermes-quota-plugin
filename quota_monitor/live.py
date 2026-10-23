"""Live view (ported from Agent Monitor's panel.py ``live()``): open sessions, their state, token rate, burn,
and the "agent attention" items.

Agent Monitor sampled usage totals every 2 s in a background thread. Here each ``snapshot`` call stores one
sample in quota_monitor.db (``store.add_rate_sample``) and the rate is the delta between consecutive samples,
so the per-minute rate fills while the widget polls.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from . import clis, context, store
from .common import (cost_expr, kanban_dbs, num, pid_alive, profile_homes, read_json, ro, row_cost,
                     session_costs, table_cols, usage_cost_expr)

ACTIVE_WINDOW = 600            # a session with activity in the last N seconds counts as running
LEASED_SOURCES = ("cli", "desktop", "tui")  # surfaces that hold a lease while their window is open
SUBAGENT_OPEN_WINDOW = 3600    # a subagent of an open session, not ended, stays listed while it wrote within this
INTERACTIVE_SOURCES = ("desktop", "cli", "tui", "telegram", "slack", "discord")
STALL_AFTER = 600              # a turn that claims to be running but wrote nothing for this long is "stalled"
STALL_AFTER_TOOL = 1800        # ... but a tool call still in flight (a build, a test run) gets this long
APPROVAL_MARK = "approval"     # Hermes states a pending approval only through its activity label
RATE_GAP_MAX = 300             # two samples further apart than this carry no per-minute rate (widget was closed)
LARGE_CONTEXT = 200_000        # tokens; above this a session should compact or start fresh
LOOP_CALLS = 3                 # identical consecutive tool calls (last 30 min) that count as a possible loop
SPEED_TTL = 300                # model speed is cached this long (the SQL walks a day of messages)
KEY_SEP = chr(31)              # joins profile / provider / session / model in a rate-sample key
KANBAN_PROMPT = re.compile(r"kanban task (t_[0-9a-f]+)")
HERMES_APPS = (Path("/Applications/Hermes.app"), Path.home() / "Applications" / "Hermes.app")
HERMES_KANBAN_URL = "hermes://open/kanban"
NO_OPEN = {"kind": "none", "url": None, "command": None, "label": ""}


# ---------- leases ----------

def open_leases(home: Path) -> Dict[str, Any]:
    """{session id: pid} of the chat surfaces open right now (Hermes' lease registry, live pid only)."""
    data = read_json(home / "runtime" / "active_sessions.json")
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {}
    return {e.get("session_id"): e.get("pid") for e in entries
            if isinstance(e, dict) and e.get("session_id") and pid_alive(e.get("pid"))}


def main_is_open(source: Optional[str], sid: str, last: Optional[float], opened: set, now: float) -> bool:
    if source in LEASED_SOURCES:
        return sid in opened
    return sid in opened or (last or 0) > now - ACTIVE_WINDOW


# ---------- open actions + process facts ----------

def hermes_desktop_installed() -> bool:
    return sys.platform == "darwin" and any(p.exists() for p in HERMES_APPS)


def _label(text: Any, fallback: str) -> str:
    t = " ".join(str(text or "").split()) or fallback
    return t if len(t) <= 80 else t[:79] + "…"


def open_hermes_session(profile: str, sid: str, title: Any) -> Dict[str, Any]:
    cmd = "hermes " + ("-p %s " % shlex.quote(profile) if profile and profile != "default" else "") \
          + "--resume %s" % shlex.quote(sid)
    url = "hermes://open/%s" % quote(sid, safe="") if hermes_desktop_installed() else None
    return {"kind": "hermes-session", "url": url, "command": cmd, "label": _label(title, sid)}


def open_kanban_card(card_id: str, board: str, title: Any, session_cmd: Optional[str] = None,
                     session_url: Optional[str] = None) -> Dict[str, Any]:
    cmd = "hermes kanban " + ("--board %s " % shlex.quote(board) if board and board != "default" else "") \
          + "show %s" % shlex.quote(card_id)
    out = {"kind": "kanban-card", "url": HERMES_KANBAN_URL if hermes_desktop_installed() else None,
           "command": cmd, "label": _label(title, card_id), "card_id": card_id}
    if session_cmd:
        out["session_command"] = session_cmd
        out["session_url"] = session_url
    return out


def open_cli_session(kind: str, sid: str, project: Optional[str], title: Any) -> Dict[str, Any]:
    binary = "claude" if kind == "claude-code" else "copilot"
    cmd = "%s --resume %s" % (binary, shlex.quote(sid))
    if kind == "claude-code" and project:
        cmd = "cd %s && %s" % (shlex.quote(project), cmd)
    return {"kind": kind, "url": None, "command": cmd, "label": _label(title, sid)}


APP_RE = re.compile(r"^(/.*?\.app)/Contents/")


def parse_ps(text: str) -> Dict[int, Tuple[int, Optional[str], str]]:
    table = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        tty = None if parts[2] in ("??", "?", "-") else parts[2]
        table[int(parts[0])] = (int(parts[1]), tty, parts[3] if len(parts) > 3 else "")
    return table


def proc_table() -> Dict[int, Tuple[int, Optional[str], str]]:
    """Process list from ``ps`` ({} when ps is missing or fails — e.g. a minimal Linux container)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,tty=,command="], capture_output=True, text=True,
                             timeout=5).stdout
        return parse_ps(out)
    except (OSError, subprocess.SubprocessError):
        return {}


def process_info(pid: Any, table: Dict[int, Tuple[int, Optional[str], str]]) -> Optional[Dict[str, Any]]:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if not pid or pid not in table or not pid_alive(pid):
        return None
    info: Dict[str, Any] = {"pid": pid, "tty": table[pid][1], "app": None, "app_pid": None, "background": False}
    cur, hops = pid, 0
    while cur in table and hops < 25:
        ppid, _, cmd = table[cur]
        if "bg-pty-host" in cmd or "--bg-spare" in cmd:
            info["background"] = True
        m = APP_RE.match(cmd)
        if m and info["app"] is None:
            info["app"], info["app_pid"] = m.group(1), cur
        if ppid <= 1 or ppid == cur:
            break
        cur, hops = ppid, hops + 1
    return info


def with_process(o: Dict[str, Any], pid: Any, table: Dict, attach: Optional[str] = None) -> Dict[str, Any]:
    info = process_info(pid, table) if pid else None
    o.update({"running": bool(info), "pid": info["pid"] if info else None, "tty": info["tty"] if info else None,
              "app": info["app"] if info else None, "app_pid": info["app_pid"] if info else None,
              "background": bool(info and info["background"]), "attach": attach})
    return o


def _attach_for(row: Optional[Dict[str, Any]], sid: str) -> Optional[str]:
    if row and row.get("_bg"):
        return "claude attach %s" % shlex.quote(row.get("_job") or sid[:8])
    return None


def _open_for(r: Dict[str, Any], by_id: Dict, table: Dict) -> Dict[str, Any]:
    if r.get("kanban"):
        k = r["kanban"]
        card_only = r.get("card_only")
        sess = None if card_only else open_hermes_session(r["profile"], r["id"], r["title"])
        o = open_kanban_card(k["id"], k["board"], k["title"], sess and sess["command"], sess and sess["url"])
        return with_process(o, None if card_only else r.get("_pid"), table)
    if r["subagent"]:
        parent = by_id.get((r["profile"], r["parent"]))
        ppid = parent.get("_pid") if parent else None
        if r["kind"] == "hermes" and r.get("_psource"):
            return with_process(open_hermes_session(r["profile"], r["parent"], r.get("parent_title")), ppid, table)
        if r["kind"] == "claude-code" and r["parent"]:
            o = open_cli_session("claude-code", r["parent"], (parent and parent.get("_project")) or r.get("_project"),
                                 parent and parent.get("title"))
            return with_process(o, ppid, table, _attach_for(parent, r["parent"]))
        return dict(NO_OPEN)
    if r["kind"] == "hermes":
        return with_process(open_hermes_session(r["profile"], r["id"], r["title"]), r.get("_pid"), table)
    if r["kind"] in ("claude-code", "copilot-cli"):
        attach = _attach_for(r, r["id"]) if r["kind"] == "claude-code" else None
        return with_process(open_cli_session(r["kind"], r["id"], r.get("_project"), r["title"]), r.get("_pid"),
                            table, attach)
    return dict(NO_OPEN)


# ---------- kanban cards (running rows) ----------

def kanban_cards(root: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    out = {}
    for board, db in kanban_dbs(root):
        if not db.exists():
            continue
        try:
            with ro(db) as c:
                cols = table_cols(c, "tasks")
                bk = "block_kind" if "block_kind" in cols else "NULL AS block_kind"
                wp = "worker_pid" if "worker_pid" in cols else "NULL AS worker_pid"
                for r in c.execute("""SELECT id, title, assignee, status, started_at, last_heartbeat_at, %s, %s
                                      FROM tasks WHERE status = 'running'""" % (wp, bk)):
                    out[r["id"]] = {"id": r["id"], "board": board, "title": r["title"], "assignee": r["assignee"],
                                    "status": r["status"], "started_at": num(r["started_at"]),
                                    "heartbeat": num(r["last_heartbeat_at"]), "worker_pid": r["worker_pid"],
                                    "block_kind": r["block_kind"]}
        except sqlite3.Error:
            continue
    return out


def match_kanban_sessions(rows: List[Dict[str, Any]], cards: Dict[str, Dict[str, Any]]) -> set:
    """Pair kanban worker sessions with their card: lease pid == worker_pid, then the task id in the first user
    message, then same assignee + title + start time. A card pairs with at most one session."""
    taken: set = set()

    def take(r: Dict[str, Any], card: Dict[str, Any]) -> None:
        r["kanban"] = {"id": card["id"], "board": card["board"], "status": card["status"], "title": card["title"],
                       "block_kind": card["block_kind"]}
        r["kanban_card"], r["kanban_status"] = card["title"], card["status"]
        taken.add(card["id"])

    work = [r for r in rows if r.get("origin") == "kanban" and not r["subagent"]]
    left = []
    for r in work:
        pid = r.get("_pid")
        card = next((c for c in cards.values() if pid and c["worker_pid"] == pid and c["id"] not in taken
                     and c["status"] == "running"), None)
        if card:
            take(r, card)
        else:
            left.append(r)
    rest = []
    for r in left:
        m = KANBAN_PROMPT.search(r.get("_first_user") or "")
        card = cards.get(m.group(1)) if m else None
        if card and card["id"] not in taken:
            take(r, card)
        else:
            rest.append(r)
    for r in rest:
        card = next((c for c in cards.values() if c["id"] not in taken and c["status"] == "running"
                     and c["assignee"] == r["profile"] and c["title"] == r["title"]
                     and (r["started"] or 0) >= (c["started_at"] or 0) - 120), None)
        if card:
            take(r, card)
    return taken


def card_only_row(card: Dict[str, Any], now: float) -> Dict[str, Any]:
    alive = bool(card["worker_pid"]) and pid_alive(card["worker_pid"])
    last = max(card["heartbeat"] or 0, card["started_at"] or 0) or None
    return {"id": card["id"], "card_only": True, "parent": None, "parent_title": None, "profile": card["assignee"],
            "title": card["title"], "model": None, "provider": "", "started": card["started_at"], "last": last,
            "subagent": False, "status": "running", "end_reason": None, "working": alive, "activity": None,
            "tokens": 0, "cost": 0.0, "tools": 0, "messages": 0, "parent_model": None, "origin": "kanban",
            "interactive": False, "state": "working" if alive else "stalled", "state_since": None if alive else last,
            "waiting_for": None, "context_tokens": None, "context_estimated": True, "context_max": None,
            "context_percent": None, "loop_max": 0, "long_tool_call": None, "kind": "hermes", "source_name": "Hermes",
            "kanban": {"id": card["id"], "board": card["board"], "status": card["status"], "title": card["title"],
                       "block_kind": card["block_kind"]},
            "kanban_card": card["title"], "kanban_status": card["status"]}


# ---------- token rate ----------

def usage_totals_now(homes: List[Tuple[str, Path]], now: Optional[float] = None) -> Dict[str, List[float]]:
    """Cumulative tokens/cost per "profile|provider|session|model" of sessions touched in the last 2 h."""
    since = (time.time() if now is None else now) - 7200
    totals: Dict[str, List[float]] = {}
    for name, home in homes:
        db = home / "state.db"
        if not db.exists():
            continue
        try:
            with ro(db) as c:
                rows = c.execute(
                    """SELECT session_id, model, coalesce(nullif(billing_provider,''),'unknown') prov,
                              coalesce(input_tokens,0)+coalesce(output_tokens,0) tok, %s cost
                       FROM session_model_usage WHERE last_seen >= ?""" % usage_cost_expr(), (since,)).fetchall()
                costs = session_costs(c, {r["session_id"] for r in rows if not r["cost"]})
                for r in rows:
                    key = KEY_SEP.join((name, r["prov"], r["session_id"], r["model"] or ""))
                    totals[key] = [r["tok"], row_cost(r["cost"], r["tok"], costs.get(r["session_id"]))]
        except sqlite3.Error:
            continue
    return totals


def token_rate(samples: List[Tuple[float, Dict[str, List[float]]]], now: Optional[float] = None) -> Dict[str, Any]:
    """Per-minute token/cost deltas by provider from consecutive samples (pairs further apart than RATE_GAP_MAX
    are skipped: the delta of a long gap cannot be placed in one minute)."""
    now = time.time() if now is None else now
    minutes: Dict[int, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: {"tokens": 0, "cost": 0.0}))
    by_profile: Dict[str, Dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0})
    for (t0, a), (t1, b) in zip(samples, samples[1:]):
        if t1 - t0 > RATE_GAP_MAX:
            continue
        m = int(t1 // 60) * 60
        for key, (tok, cost) in b.items():
            ptok, pcost = a.get(key, (0, 0.0))
            dt, dc = max(0, tok - ptok), max(0.0, cost - pcost)
            if dt or dc:
                profile, prov = key.split(KEY_SEP)[:2]
                minutes[m][prov]["tokens"] += dt
                minutes[m][prov]["cost"] += dc
                if t1 >= now - 3600:
                    by_profile[profile]["tokens"] += dt
                    by_profile[profile]["cost"] += dc
    gaps = sorted(t1 - t0 for (t0, _), (t1, _) in zip(samples, samples[1:]))
    every = int(round(gaps[len(gaps) // 2])) if gaps else None
    return {"since": samples[0][0] if samples else None, "minutes": minutes, "by_profile": by_profile,
            "every": every}


# ---------- one Hermes session's state ----------

def hermes_state(c: sqlite3.Connection, sid: str, act: Optional[str], now: float,
                 model_config: Any = None) -> Dict[str, Any]:
    """state / since / context size / loop for one open Hermes session (same rules as Agent Monitor; the
    context size prefers Hermes' persisted usage anchor, see ``context.used_tokens``)."""
    last = c.execute("""SELECT role, tool_calls IS NULL final, timestamp, tool_calls FROM messages
                        WHERE session_id = ? AND active = 1 ORDER BY id DESC LIMIT 1""", (sid,)).fetchone()
    pending_tool = None
    if last and last["role"] == "assistant" and last["tool_calls"]:
        try:
            names = [(t.get("function") or {}).get("name") for t in json.loads(last["tool_calls"])]
            pending_tool = ", ".join(n for n in names if n) or "tool"
        except (ValueError, AttributeError, TypeError):
            pending_tool = "tool"
    stall_after = STALL_AFTER_TOOL if pending_tool else STALL_AFTER
    try:
        lease = c.execute("SELECT 1 FROM session_turn_leases WHERE conversation_id = ? AND expires_at > ?",
                          (sid, now)).fetchone() is not None
    except sqlite3.Error:
        lease = False
    last_ts = num(last["timestamp"]) if last else None
    quiet = now - (last_ts or now)
    if act and APPROVAL_MARK in act.lower():
        state, since = "needs_approval", None
    elif lease or act:
        state, since = ("stalled", last_ts) if quiet > stall_after else ("working", None)
    elif last and last["role"] == "assistant" and last["final"]:
        state, since = "waiting", last_ts
    elif last and quiet > stall_after:
        state, since = "stalled", last_ts
    else:
        state, since = "working", None
    ctx_tokens, ctx_estimated = context.used_tokens(c, sid, model_config)
    try:
        loop = c.execute("""WITH calls AS (
                              SELECT a.id msg, CAST(tc.key AS INT) idx, json_extract(tc.value, '$.function.name') name,
                                     json_extract(tc.value, '$.function.arguments') args
                              FROM messages a, json_each(a.tool_calls) tc
                              WHERE a.session_id = ? AND a.role = 'assistant' AND a.tool_calls IS NOT NULL
                                AND a.timestamp > ?),
                            seq AS (SELECT *, ROW_NUMBER() OVER (ORDER BY msg, idx) rn,
                                    ROW_NUMBER() OVER (PARTITION BY name, args ORDER BY msg, idx) rk FROM calls)
                            SELECT MAX(n) FROM (SELECT COUNT(*) n FROM seq GROUP BY name, args, rn - rk)""",
                         (sid, now - 1800)).fetchone()[0] or 0
    except sqlite3.Error:  # no JSON1 / window functions in this sqlite build
        loop = 0
    return {"state": state, "state_since": since, "context_tokens": ctx_tokens,
            "context_estimated": ctx_estimated, "loop_max": loop,
            "waiting_for": "approval" if state == "needs_approval" else None,
            "long_tool_call": pending_tool if state in ("working", "stalled") and quiet > 60 else None}


# ---------- model speed (cached) ----------

def model_speed(homes: List[Tuple[str, Path]], since: float) -> List[Dict[str, Any]]:
    rows: Dict[Tuple[str, str], Dict[str, List[float]]] = defaultdict(lambda: {"dur": [], "first": []})
    for _, home in homes:
        db = home / "state.db"
        if not db.exists():
            continue
        try:
            with ro(db) as c:
                for r in c.execute("""
                    WITH m AS (SELECT session_id, id, role, timestamp, tool_calls IS NULL final,
                                      role = 'user' AND display_kind IS NULL prompt,
                                      MAX(CASE WHEN role = 'user' THEN id END)
                                        OVER (PARTITION BY session_id ORDER BY id ROWS UNBOUNDED PRECEDING) turn
                               FROM messages
                               WHERE timestamp > ? AND _compressed_summary = 0 AND (active = 1 OR compacted = 1)),
                    t AS (SELECT session_id, turn,
                                 MAX(CASE WHEN id = turn THEN timestamp END) t0,
                                 MAX(CASE WHEN id = turn THEN prompt END) is_prompt,
                                 MIN(CASE WHEN role = 'assistant' THEN timestamp END) t_first,
                                 MAX(CASE WHEN role = 'assistant' AND final THEN timestamp END) t_final
                          FROM m WHERE turn IS NOT NULL GROUP BY session_id, turn)
                    SELECT s.model, t_final - t0 dur, t_first - t0 first FROM t JOIN sessions s ON s.id = t.session_id
                    WHERE t0 > ? AND is_prompt AND t_final IS NOT NULL AND s.model IS NOT NULL""",
                                   (since - 3600, since)):
                    rows[("hermes", r["model"])]["dur"].append(r["dur"])
                    if r["first"] is not None:
                        rows[("hermes", r["model"])]["first"].append(r["first"])
        except sqlite3.Error:
            continue
    for model, sec in clis.claude_turns(since):
        rows[("claude-code", model)]["dur"].append(sec)

    def pct(xs: List[float], q: float) -> Optional[float]:
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))], 1) if xs else None

    return sorted(({"source": src, "model": m, "turns": len(v["dur"]), "p50_s": pct(v["dur"], 0.5),
                    "p90_s": pct(v["dur"], 0.9), "first_reply_p50_s": pct(v["first"], 0.5)}
                   for (src, m), v in rows.items() if v["dur"]), key=lambda x: -x["turns"])


def model_speed_cached(homes: List[Tuple[str, Path]]) -> List[Dict[str, Any]]:
    cached = store.kv_get("model_speed", max_age=SPEED_TTL)
    if isinstance(cached, list):
        return cached
    data = model_speed(homes, time.time() - 86400)
    try:
        store.kv_set("model_speed", data)
    except sqlite3.Error:
        pass
    return data


# ---------- counts + attention ----------

def live_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    main = [r for r in rows if r["status"] == "running" and not r["subagent"]]

    def n(st: str) -> int:
        return sum(1 for r in main if r["state"] == st)

    return {"open": len(main), "working": n("working"), "needs_approval": n("needs_approval"),
            "your_turn": n("waiting"), "stalled": n("stalled"),
            "subagents": sum(1 for r in rows if r["status"] == "running" and r["subagent"])}


def attention(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Agent Bar's "agent attention" rules over the running main sessions, most urgent first.
    your_turn only for interactive sources: an unattended worker (kanban, cron) never waits for you."""
    order = {"approval": 0, "stalled": 1, "your_turn": 2, "loop": 3, "big_context": 4}
    out = []
    for r in rows:
        if r.get("status") != "running" or r.get("subagent"):
            continue
        source = r.get("kind") or "hermes"
        base = {"session_id": r.get("id"), "profile": r.get("profile"), "title": r.get("title"), "source": source}
        state = r.get("state")
        items = []
        if state == "needs_approval":
            items.append(("approval", r.get("state_since"), r.get("waiting_detail") or "approval", None))
        elif state == "waiting" and r.get("interactive"):
            items.append(("your_turn", r.get("state_since"), "finished", None))
        elif state == "stalled":
            tool = r.get("long_tool_call")
            items.append(("stalled", r.get("state_since"), ("tool:%s" % tool) if tool else "no_progress", None))
        loops = r.get("loop_max") or 0
        if loops >= LOOP_CALLS:
            items.append(("loop", None, "%d identical tool calls" % loops, loops))
        ctx = r.get("context_tokens") or 0
        if ctx >= LARGE_CONTEXT:
            items.append(("big_context", None, "%d tokens" % ctx, ctx))
        for kind, since, detail, value in items:
            out.append(dict(base, id="%s:%s:%s:%s" % (kind, source, r.get("profile") or "", r.get("id")),
                            kind=kind, since=since, detail=detail, value=value))
    out.sort(key=lambda a: (order[a["kind"]], a["since"] or float("inf")))
    return out


# ---------- live ----------

def live(homes: Optional[List[Tuple[str, Path]]] = None, now: Optional[float] = None,
         record_sample: bool = True) -> Dict[str, Any]:
    homes = profile_homes() if homes is None else homes
    now = time.time() if now is None else now
    cur = int(now // 60) * 60
    mins = [cur - 60 * i for i in range(59, -1, -1)]
    activity = {m: {"assistant": 0, "tool": 0, "user": 0} for m in mins}
    running: List[Dict[str, Any]] = []
    for name, home in homes:
        db = home / "state.db"
        if not db.exists():
            continue
        try:
            leases = open_leases(home)
            opened = set(leases)
            with ro(db) as c:
                recent = [r[0] for r in c.execute(
                    "SELECT id FROM sessions WHERE coalesce(last_activity_at, started_at) >= ?", (mins[0],))]
                for sid in recent:
                    for r in c.execute("""SELECT timestamp, role FROM messages
                                          WHERE session_id = ? AND timestamp >= ?""", (sid, mins[0])):
                        m = int(r[0] // 60) * 60
                        if m in activity and r[1] in activity[m]:
                            activity[m][r[1]] += 1
                held = list(opened)
                hold = ",".join("?" * len(held))
                in_open = "id IN (%s)" % hold if held else "0"
                scols = table_cols(c, "sessions")
                mcfg = "model_config" if "model_config" in scols else "NULL AS model_config"
                burl = "billing_base_url" if "billing_base_url" in scols else "'' AS billing_base_url"
                sub_open = "parent_session_id IN (%s)" % hold if held else "0"
                for s in c.execute(
                        """SELECT id, parent_session_id parent, title, model, started_at, ended_at, end_reason,
                                  (SELECT p.title FROM sessions p WHERE p.id = sessions.parent_session_id) ptitle,
                                  coalesce(last_activity_at, started_at) t, last_activity_description act,
                                  coalesce(input_tokens,0)+coalesce(output_tokens,0) tok, %s cost,
                                  coalesce(tool_call_count,0) tools, coalesce(message_count,0) msgs,
                                  coalesce(billing_provider,'') prov, source, %s, %s,
                                  (SELECT p.source FROM sessions p WHERE p.id = sessions.parent_session_id) psource,
                                  (SELECT p.model FROM sessions p WHERE p.id = sessions.parent_session_id) pmodel
                           FROM sessions
                           WHERE (ended_at IS NULL AND (coalesce(last_activity_at, started_at) > ? OR %s))
                              OR (ended_at IS NULL AND %s AND coalesce(last_activity_at, started_at) > ?)
                              OR (parent_session_id IS NOT NULL AND coalesce(ended_at, last_activity_at) > ?)"""
                        % (cost_expr(), mcfg, burl, in_open, sub_open),
                        (now - ACTIVE_WINDOW, *held, *held, now - SUBAGENT_OPEN_WINDOW, now - 900)):
                    t = num(s["t"]) or 0
                    if s["parent"] is None:
                        if s["source"] in LEASED_SOURCES and s["id"] not in opened:
                            continue  # a closed window leaves ended_at unset until Hermes reaps it
                        is_running = s["ended_at"] is None and main_is_open(s["source"], s["id"], t, opened, now)
                    else:
                        is_running = s["ended_at"] is None and (
                            t > now - ACTIVE_WINDOW or (s["parent"] in opened and t > now - SUBAGENT_OPEN_WINDOW))
                        if s["psource"] in LEASED_SOURCES and s["parent"] not in opened:
                            is_running = False
                    extra = (hermes_state(c, s["id"], s["act"], now, s["model_config"])
                             if is_running and s["parent"] is None else
                             {"state": "working" if is_running else "ended", "state_since": None,
                              "context_tokens": None, "context_estimated": True, "loop_max": 0,
                              "waiting_for": None, "long_tool_call": None})
                    extra["context_max"] = (context.context_max(home, s["model"], s["prov"], s["billing_base_url"])
                                            if extra["context_tokens"] else None)
                    extra["context_percent"] = context.context_percent(extra["context_tokens"], extra["context_max"])
                    extra["origin"] = s["source"]
                    extra["interactive"] = s["source"] in INTERACTIVE_SOURCES
                    row = dict(extra)
                    row.update({
                        "id": s["id"], "parent": s["parent"], "parent_title": s["ptitle"], "profile": name,
                        "title": s["title"], "model": s["model"], "provider": s["prov"],
                        "started": num(s["started_at"]), "last": t, "subagent": s["parent"] is not None,
                        "status": "running" if is_running else "ended",
                        "end_reason": None if is_running else (s["end_reason"] or None),
                        "working": extra["state"] == "working" if s["parent"] is None
                        else is_running and t > now - clis.WORKING_WINDOW,
                        "activity": s["act"], "tokens": s["tok"], "cost": s["cost"],
                        "tools": s["tools"], "messages": s["msgs"], "parent_model": s["pmodel"],
                        "kanban_card": None, "kanban_status": None, "kanban": None,
                        "kind": "hermes", "source_name": "Hermes"})
                    row["_pid"] = leases.get(s["id"])
                    if s["source"] == "kanban" and s["parent"] is None:
                        fu = c.execute("""SELECT content FROM messages WHERE session_id = ? AND role = 'user'
                                          ORDER BY id LIMIT 1""", (s["id"],)).fetchone()
                        row["_first_user"] = (fu[0] or "")[:400] if fu else ""
                    row["_psource"] = s["psource"]
                    running.append(row)
        except sqlite3.Error:
            continue

    # token rate: one sample per call, persisted
    samples = store.rate_samples()
    if record_sample:
        snap = usage_totals_now(homes, now)
        try:
            store.add_rate_sample(snap, now)
            samples = [s for s in samples if s[0] < now] + [(now, snap)]
        except sqlite3.Error:
            pass
    rate = token_rate(samples, now)
    cli_minutes, cli_active = clis.live(mins)
    merged: Dict[int, Dict[str, Dict[str, float]]] = {}
    for m in mins:
        by: Dict[str, Dict[str, float]] = {}
        hm = rate["minutes"].get(m, {})
        if hm:
            by["hermes"] = {"tokens": sum(v["tokens"] for v in hm.values()),
                            "cost": sum(v["cost"] for v in hm.values())}
        for src, v in cli_minutes.get(m, {}).items():
            if v["tokens"] or v["cost"]:
                by[src] = dict(v)
        merged[m] = by
    providers = [k for k in ("hermes", "claude-code", "copilot-cli") if any(k in by for by in merged.values())]

    def window_sum(sec: int, src: Optional[str] = None) -> int:
        return sum(v["tokens"] for m, by in merged.items() if m >= cur - sec + 60
                   for k, v in by.items() if src is None or k == src)

    def window_cost(sec: int, src: Optional[str] = None) -> float:
        return sum(v["cost"] for m, by in merged.items() if m >= cur - sec + 60
                   for k, v in by.items() if src is None or k == src)

    for s in cli_active:
        running.append({"id": s["id"], "parent": s["parent"], "parent_title": None,
                        "profile": clis.SOURCES[s["source"]], "source": s["source"], "kind": s["source"],
                        "source_name": clis.SOURCES[s["source"]], "title": s["title"], "model": s["model"],
                        "provider": "", "started": s["started"], "last": s["last"], "subagent": s["subagent"],
                        "status": "running" if s["running"] else "ended",
                        "end_reason": None if s["running"] else "agent_close", "_project": s["project"],
                        "_pid": s.get("pid"), "_bg": s.get("background"), "_job": s.get("job"),
                        "long_tool_call": None, "kanban": None, "activity": s["project"],
                        "working": bool(s.get("working")), "origin": s["source"], "interactive": True,
                        "state": s.get("state") if s["running"] else "ended", "state_since": s.get("state_since"),
                        "waiting_for": "approval" if s.get("state") == "needs_approval" else None,
                        "waiting_detail": s.get("waiting_for"), "context_tokens": s.get("context_tokens"),
                        "context_estimated": s.get("context_estimated", False), "context_max": None,
                        "context_percent": None, "loop_max": s.get("loop_max", 0),
                        "tokens": s["total_tokens"], "cost": s["total_cost"], "tools": s["calls"], "messages": 0,
                        "parent_model": None, "kanban_card": None, "kanban_status": None})

    cards = kanban_cards(homes[0][1] if homes else None)
    taken = match_kanban_sessions([r for r in running if r["status"] == "running"], cards)
    for card in cards.values():
        if card["status"] == "running" and card["id"] not in taken:
            running.append(card_only_row(card, now))
    by_id = {(r["profile"], r["id"]): r for r in running}
    table = proc_table()
    for r in running:
        r["open"] = _open_for(r, by_id, table)
        # Agent Monitor attributed a running session to one of several Claude accounts through its own pool
        # routing; the plugin has no such routing, so the keys stay for shape compatibility.
        r["account_provider"], r["account"] = None, None
        for k in [k for k in r if k.startswith("_")]:
            del r[k]

    running.sort(key=lambda r: -(r["last"] or 0))
    return {
        "now": now, "sampling_since": rate["since"], "sample_every": rate["every"],
        "activity": [dict({"t": m}, **activity[m]) for m in mins],
        "tokens": [{"t": m, "by": merged[m]} for m in mins], "providers": providers,
        "by_profile": sorted([dict({"profile": k}, **v) for k, v in rate["by_profile"].items()] +
                             [{"profile": clis.SOURCES[k], "tokens": window_sum(3600, k),
                               "cost": window_cost(3600, k)}
                              for k in ("claude-code", "copilot-cli") if window_sum(3600, k)],
                             key=lambda x: -x["tokens"]),
        "last_1m": window_sum(60), "last_5m": window_sum(300), "last_60m": window_sum(3600),
        "by_source_60m": {k: window_sum(3600, k) for k in providers},
        "cost_60m": window_cost(3600),
        "running": running, "counts": live_counts(running), "attention": attention(running),
    }
