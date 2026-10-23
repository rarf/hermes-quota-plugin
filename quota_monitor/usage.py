"""Usage, top sessions and the kanban glance (ported from Agent Monitor's panel.py)."""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import clis
from .common import cost_expr, day_of, day_range, kanban_dbs, num, profile_homes, ro, row_cost, session_costs, \
    table_cols, usage_cost_expr
from .live import open_kanban_card

KANBAN_CARD_CAP = 50
FAILED_RECENT_S = 86400  # a card that failed within this long still shows as "recently failed"


def usage(start: float, end: float, days: List[str], homes: Optional[List[Tuple[str, Path]]] = None,
          profile: Optional[str] = None) -> Dict[str, Any]:
    """Per-model usage rows from every profile's state.db, bucketed by local day of last_seen
    (Agent Monitor ``/api/overview`` ``usage`` shape)."""
    homes = profile_homes() if homes is None else homes
    by_day_provider: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"tokens": 0, "cost": 0.0}))
    by_provider: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"tokens": 0, "input": 0, "output": 0, "cache_read": 0, "cost": 0.0, "calls": 0,
                 "sessions": set(), "mode": None})
    by_model: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "calls": 0,
                                                                "provider": None})
    by_profile: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "sessions": 0,
                                                                  "subagents": 0, "tool_calls": 0})
    totals = {"tokens": 0, "input": 0, "output": 0, "cache_read": 0, "reasoning": 0, "cost": 0.0,
              "calls": 0, "sessions": 0, "subagents": 0, "tool_calls": 0, "messages": 0}
    sessions_by_day: Dict[str, int] = defaultdict(int)

    for name, home in homes:
        if profile and name != profile:
            continue
        db = home / "state.db"
        if not db.exists():
            continue
        try:
            with ro(db) as c:
                rows = c.execute(
                    """SELECT session_id, model, coalesce(billing_provider,'') prov, billing_mode,
                              coalesce(api_call_count,0) calls, coalesce(input_tokens,0) inp,
                              coalesce(output_tokens,0) outp, coalesce(cache_read_tokens,0) cr,
                              coalesce(reasoning_tokens,0) rs, %s cost, last_seen
                       FROM session_model_usage WHERE last_seen >= ? AND last_seen < ?""" % usage_cost_expr(),
                    (start, end)).fetchall()
                costs = session_costs(c, {r["session_id"] for r in rows if not r["cost"]})
                for r in rows:
                    prov = r["prov"] or "unknown"
                    tok = r["inp"] + r["outp"]
                    cost = row_cost(r["cost"], tok, costs.get(r["session_id"]))
                    d = day_of(r["last_seen"])
                    by_day_provider[d][prov]["tokens"] += tok
                    by_day_provider[d][prov]["cost"] += cost
                    p = by_provider[prov]
                    p["tokens"] += tok
                    p["input"] += r["inp"]
                    p["output"] += r["outp"]
                    p["cache_read"] += r["cr"]
                    p["cost"] += cost
                    p["calls"] += r["calls"]
                    p["sessions"].add((name, r["session_id"]))
                    p["mode"] = p["mode"] or r["billing_mode"]
                    m = by_model[r["model"] or "?"]
                    m["tokens"] += tok
                    m["cost"] += cost
                    m["calls"] += r["calls"]
                    m["provider"] = m["provider"] or prov
                    by_profile[name]["tokens"] += tok
                    by_profile[name]["cost"] += cost
                    totals["tokens"] += tok
                    totals["input"] += r["inp"]
                    totals["output"] += r["outp"]
                    totals["cache_read"] += r["cr"]
                    totals["reasoning"] += r["rs"]
                    totals["cost"] += cost
                    totals["calls"] += r["calls"]
                for r in c.execute(
                        """SELECT started_at, parent_session_id IS NOT NULL sub,
                                  coalesce(tool_call_count,0) tools, coalesce(message_count,0) msgs
                           FROM sessions WHERE started_at >= ? AND started_at < ?""", (start, end)):
                    totals["sessions"] += 1
                    totals["subagents"] += r["sub"]
                    totals["tool_calls"] += r["tools"]
                    totals["messages"] += r["msgs"]
                    by_profile[name]["sessions"] += 1
                    by_profile[name]["subagents"] += r["sub"]
                    by_profile[name]["tool_calls"] += r["tools"]
                    sessions_by_day[day_of(r["started_at"])] += 1
        except sqlite3.Error:
            continue

    providers = sorted(by_provider, key=lambda k: -by_provider[k]["tokens"])
    return {
        "days": days,
        "totals": totals,
        "providers": providers,
        "daily": [{"day": d, "by": {p: by_day_provider[d][p] for p in providers if p in by_day_provider[d]},
                   "sessions": sessions_by_day.get(d, 0)} for d in days],
        "by_provider": [dict({k: v for k, v in by_provider[p].items() if k != "sessions"},
                             provider=p, sessions=len(by_provider[p]["sessions"])) for p in providers],
        "by_model": sorted((dict({"model": k}, **v) for k, v in by_model.items()), key=lambda x: -x["tokens"]),
        "by_profile": sorted((dict({"profile": k}, **v) for k, v in by_profile.items()), key=lambda x: -x["tokens"]),
    }


def usage_today_month(homes: Optional[List[Tuple[str, Path]]] = None, today: Optional[date] = None) -> Dict[str, Any]:
    today = today or date.today()
    s, e, d = day_range(today, today)
    ms, me, md = day_range(today.replace(day=1), today)
    return {"today": usage(s, e, d, homes), "month": usage(ms, me, md, homes)}


def top_sessions(start: float, end: float, homes: Optional[List[Tuple[str, Path]]] = None,
                 limit: int = 10) -> List[Dict[str, Any]]:
    """Most expensive sessions (Hermes, all profiles, + agent CLIs) active in [start, end)."""
    homes = profile_homes() if homes is None else homes
    rows = []
    for name, home in homes:
        db = home / "state.db"
        if not db.exists():
            continue
        try:
            with ro(db) as c:
                for s in c.execute(
                        """SELECT id, title, model, started_at, coalesce(last_activity_at, ended_at, started_at) last,
                                  coalesce(input_tokens,0)+coalesce(output_tokens,0) tok, %s cost,
                                  parent_session_id parent, coalesce(tool_call_count,0) tools
                           FROM sessions WHERE coalesce(last_activity_at, started_at) >= ? AND started_at < ?
                           ORDER BY cost DESC LIMIT ?""" % cost_expr(), (start, end, limit)):
                    rows.append({"source": "hermes", "profile": name, "id": s["id"], "title": s["title"],
                                 "model": s["model"], "started": num(s["started_at"]), "last": num(s["last"]),
                                 "tokens": s["tok"], "cost": s["cost"] or 0.0, "subagent": s["parent"] is not None,
                                 "tools": s["tools"]})
        except sqlite3.Error:
            continue
    for s in clis.collect(start, end)[0]:
        rows.append({"source": s["source"], "profile": clis.SOURCES[s["source"]], "id": s["id"], "title": s["title"],
                     "model": s["model"], "started": s["started"], "last": s["last"], "tokens": s["tokens"],
                     "cost": s["cost"] or 0.0, "subagent": s["subagent"], "tools": s["calls"]})
    rows.sort(key=lambda r: -r["cost"])
    return rows[:limit]


def card_waiting_for(status: Optional[str], block_kind: Optional[str]) -> Optional[str]:
    if status == "blocked" and block_kind == "needs_input":
        return "input"
    return "review" if status == "review" else None


def kanban_glance(root: Optional[Path] = None, now: Optional[float] = None,
                  cap: int = KANBAN_CARD_CAP) -> Dict[str, Any]:
    """{"boards": [...]}: per board the status counts + only the cards a glance needs — running, blocked,
    review, and failing / recently failed — capped per board (``truncated`` = how many were left out)."""
    now = time.time() if now is None else now
    out = []
    for board, db in kanban_dbs(root):
        if not db.exists():
            continue
        try:
            with ro(db) as c:
                cols = table_cols(c, "tasks")
                counts = {r[0]: r[1] for r in c.execute("SELECT status, count(*) FROM tasks GROUP BY status")}
                if not counts:
                    continue

                def col(name: str) -> str:
                    return name if name in cols else "NULL AS %s" % name

                bk = col("block_kind")
                blocked_by_kind: Dict[str, int] = defaultdict(int)
                for r in c.execute("SELECT %s FROM tasks WHERE status = 'blocked'" % bk):
                    blocked_by_kind[r[0] or "other"] += 1
                sel = ", ".join(["id", "title", "assignee", "status", col("priority"), "created_at",
                                 col("started_at"), col("completed_at"), col("last_heartbeat_at"),
                                 col("consecutive_failures"), col("last_failure_error"), bk])
                fail = "coalesce(consecutive_failures,0) > 0" if "consecutive_failures" in cols else "0"
                where = ("status IN ('running','blocked','review') OR (status NOT IN ('done','archived') AND %s)"
                         " OR (status = 'done' AND %s AND coalesce(completed_at,0) >= ?)" % (fail, fail))
                rows = [dict(r) for r in c.execute(
                    """SELECT %s FROM tasks WHERE %s
                       ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'blocked' THEN 1 WHEN 'review' THEN 2 ELSE 3 END,
                                coalesce(completed_at, started_at, created_at) DESC""" % (sel, where),
                    (now - FAILED_RECENT_S,))]
        except sqlite3.Error as e:
            out.append({"board": board, "counts": {}, "blocked_by_kind": {}, "cards": [], "truncated": 0,
                        "error": type(e).__name__})
            continue
        cards = rows[:cap]
        for card in cards:
            card["board"] = board
            card["waiting_for"] = card_waiting_for(card["status"], card["block_kind"])
            card["failing"] = bool(card.get("consecutive_failures"))
            card["open"] = open_kanban_card(card["id"], board, card["title"])
        out.append({"board": board, "counts": counts, "blocked_by_kind": dict(blocked_by_kind), "cards": cards,
                    "truncated": max(0, len(rows) - cap)})
    return {"boards": out}
