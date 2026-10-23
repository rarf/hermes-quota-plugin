"""Routing advice and agent-facing summaries (ported from Agent Monitor's mcp_server.py).

Agent Monitor's MCP server called its HTTP API; here the same functions take the payloads built locally
(``live`` / ``health`` / ``burn`` / quota items). Agent-facing output carries numbers and status codes only:
no session titles and no upstream error text, so other sessions' content cannot steer the calling agent.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

USABLE_MODEL = ("ok", "degraded")
STATES = ("working", "needs_approval", "waiting", "stalled")
LARGE_CONTEXT = 200_000
LOOP_CALLS = 3


def when(ts: Optional[float]) -> Optional[Dict[str, Any]]:
    if not ts:
        return None
    return {"at": datetime.fromtimestamp(ts).isoformat(timespec="minutes"),
            "in_minutes": round((ts - time.time()) / 60)}


def base_provider(provider: str) -> str:
    """``anthropic:<id>`` / ``anthropic#<key>`` -> ``anthropic``."""
    return provider.replace("#", ":").split(":", 1)[0]


def usable_providers(health: Dict[str, Any]) -> Dict[str, bool]:
    """provider -> has at least one credential that is not locked (limit reached / quota window full)."""
    out = {}
    for a in health.get("accounts") or []:
        out[a["provider"]] = any(not c.get("locked") for c in a["credentials"]) if a["credentials"] else True
    return out


def quota_rows(items: List[Dict[str, Any]], burn: List[Dict[str, Any]], health: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_burn = {(b["provider"], b["window"]): b for b in burn or []}
    usable = usable_providers(health)
    rows = []
    for item in items or []:
        if item.get("unavailable"):
            continue
        windows = []
        for w in item.get("windows") or []:
            if w.get("used") is None:
                continue
            b = by_burn.get((item["provider"], w["label"]))
            eta = b.get("eta_full") if b else None
            windows.append({
                "window": w["label"], "used_pct": round(w["used"]), "left_pct": max(0, round(100 - w["used"])),
                "resets": when(w.get("reset")),
                "burn_pct_per_hour": round(b["per_hour"], 1) if b else None,
                "full": when(eta),
                "fills_before_reset": bool(eta and w.get("reset") and eta < w["reset"]),
            })
        if not windows:
            continue
        rows.append({"provider": item["provider"],
                     "usable": usable.get(item["provider"], usable.get(base_provider(item["provider"]), True))
                     if ":" not in item["provider"] else True,
                     "min_left_pct": min((w["left_pct"] for w in windows), default=None), "windows": windows})
    return rows


def speed_by_model(health: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for e in health.get("speed") or []:
        m = e.get("model")
        if m and (m not in out or e.get("source") == "hermes"):
            out[m] = {k: e.get(k) for k in ("source", "turns", "p50_s", "p90_s", "first_reply_p50_s")}
    return out


def recommend_model(items: List[Dict[str, Any]], burn: List[Dict[str, Any]], health: Dict[str, Any],
                    prefer_provider: Optional[str] = None, exclude_providers: Optional[List[str]] = None,
                    min_quota_left_pct: int = 10, top: int = 3) -> Dict[str, Any]:
    """Rank the models work can be routed to right now: healthy status, a usable account, quota headroom (at
    least ``min_quota_left_pct`` left in every window, not projected to fill before reset), proven recent use;
    a faster 24 h median turn scores higher."""
    quota: Dict[str, Dict[str, Any]] = {}
    for r in quota_rows(items, burn, health):
        # several accounts of one provider: the headroom that counts is the best usable account's
        base = base_provider(r["provider"])
        cur = quota.get(base)
        if r["usable"] and (cur is None or not cur["usable"] or (r["min_left_pct"] or 0) > (cur["min_left_pct"] or 0)):
            quota[base] = r
        elif cur is None:
            quota[base] = r
    usable = usable_providers(health)
    speed = speed_by_model(health)
    exclude = set(exclude_providers or [])
    picks, skipped = [], []
    for m in health.get("models") or []:
        name, prov = m["model"], m["provider"]
        if "safety" in name or prov in exclude or prov == "unknown":
            continue
        q = quota.get(prov)
        why_not = None
        if m["status"] not in USABLE_MODEL:
            why_not = "status %s" % m["status"]
        elif not usable.get(prov, True):
            why_not = "no usable account (limit reached)"
        elif q and q["min_left_pct"] is not None and q["min_left_pct"] < min_quota_left_pct:
            why_not = "only %s%% quota left" % q["min_left_pct"]
        elif m["calls_7d"] == 0 and not m["profiles"]:
            why_not = "never used here"
        if why_not:
            if m["calls_7d"] or m["profiles"] or m["fallback_for"]:
                skipped.append({"model": name, "provider": prov, "reason": why_not})
            continue
        score, reasons = 50.0, []
        if q and q["min_left_pct"] is not None:
            score += q["min_left_pct"] * 0.3
            reasons.append("%s%% quota left" % q["min_left_pct"])
            if any(w["fills_before_reset"] for w in q["windows"]):
                score -= 20
                reasons.append("fills before reset at current burn")
        else:
            reasons.append("no quota window reported")
        rate = m.get("error_rate") or 0
        if m["status"] == "degraded":
            score -= 25
        score -= rate * 100
        reasons.append("%s, %.0f%% errors in 24 h" % (m["status"], rate * 100)
                       if m["calls_24h"] or m["errors_24h"] else m["status"])
        score += min(m["calls_24h"], 500) / 50
        sp = speed.get(name)
        if sp and (sp.get("turns") or 0) >= 3 and sp.get("p50_s") is not None:
            score += max(0.0, 10 - sp["p50_s"] / 6)
            reasons.append("median turn %.0f s (24 h, n=%s)" % (sp["p50_s"], sp["turns"]))
        if m["profiles"]:
            reasons.append("primary model of " + ", ".join(m["profiles"]))
        if prefer_provider and prov == prefer_provider:
            score += 15
            reasons.append("preferred provider")
        picks.append({"model": name, "provider": prov, "score": round(score, 1), "reasons": reasons})
    picks.sort(key=lambda p: -p["score"])
    return {"recommended": picks[:max(1, top)], "excluded": skipped}


def model_health(health: Dict[str, Any], provider: Optional[str] = None, include_idle: bool = False) -> Dict[str, Any]:
    speed = speed_by_model(health)
    models = []
    for m in health.get("models") or []:
        if provider and m["provider"] != provider:
            continue
        if m["status"] == "idle" and not include_idle:
            continue
        err = m.get("last_error") or {}
        last_error = None
        if err:
            last_error = {"code": err.get("code"), "type": err.get("type")}
            last_error.update(when(err.get("at")) or {})
        models.append({
            "model": m["model"], "provider": m["provider"], "status": m["status"],
            "recovered": m.get("recovered", False), "calls_24h": m["calls_24h"], "errors_24h": m["errors_24h"],
            "error_rate": round(m["error_rate"], 3) if m.get("error_rate") is not None else None,
            "error_types": m.get("error_types") or {}, "last_error": last_error,
            "cooldown_until": when(m.get("cooldown_until")),
            "primary_for": m["profiles"], "fallback_for": m["fallback_for"], "speed": speed.get(m["model"]),
        })
    return {"models": models}


def fallback_chains(health: Dict[str, Any], profile: Optional[str] = None) -> Dict[str, Any]:
    chains = [c for c in health.get("chains") or [] if not profile or c["profile"] == profile]
    out = []
    for c in chains:
        c = dict(c, fallbacks=[dict(f, until=when(f.get("until"))) for f in c["fallbacks"]])
        out.append(c)
    return {"chains": out}


def running_counts(live: Dict[str, Any]) -> Dict[str, Any]:
    running = [s for s in live.get("running") or [] if s.get("status") == "running"]
    per: Dict[Any, Dict[str, Any]] = {}
    totals = dict.fromkeys(STATES, 0)
    for s in running:
        key = (s.get("kind") or "hermes", s.get("profile") or "")
        slot = per.setdefault(key, dict({"source": key[0], "profile": key[1], "sessions": 0, "subagents": 0},
                                        **dict.fromkeys(STATES, 0)))
        slot["subagents" if s.get("subagent") else "sessions"] += 1
        if not s.get("subagent") and s.get("state") in STATES:
            slot[s["state"]] += 1
            totals[s["state"]] += 1
    contexts = [s["context_tokens"] for s in running if not s.get("subagent") and s.get("context_tokens")]
    out: Dict[str, Any] = {"sessions": sum(p["sessions"] for p in per.values()),
           "subagents": sum(p["subagents"] for p in per.values())}
    out.update(totals)
    out.update({"largest_context_tokens": max(contexts, default=None),
                "possible_loops": sum(1 for s in running if (s.get("loop_max") or 0) >= LOOP_CALLS),
                "by_source": list(per.values())})
    return out


def agent_attention(live: Dict[str, Any]) -> Dict[str, Any]:
    """The attention items without titles or free text (agent-facing)."""
    rows = []
    for a in live.get("attention") or []:
        rows.append({"kind": a["kind"], "source": a.get("source"), "profile": a.get("profile"),
                     "session_id": a.get("session_id"), "since": when(a.get("since")), "value": a.get("value")})
    return {"count": len(rows), "items": rows}


def spend_summary(month_usage: Dict[str, Any], clis_daily: List[Dict[str, Any]], budget: Dict[str, Any],
                  today: Optional[date] = None) -> Dict[str, Any]:
    """Estimated spend (Hermes + agent CLIs, API-price equivalent): today, yesterday, month to date and the
    month-end projection, against the daily / monthly budget (None = no budget)."""
    today = today or date.today()

    def spend(day: Optional[str]) -> float:
        hermes = sum(v.get("cost") or 0 for d in month_usage.get("daily") or [] if day in (None, d["day"])
                     for v in d["by"].values())
        cli = sum(sum(d["cost"].values()) for d in clis_daily or [] if day in (None, d["day"]))
        return hermes + cli

    t = spend(today.isoformat())
    y = spend(date.fromordinal(today.toordinal() - 1).isoformat()) if today.day > 1 else None
    mtd = spend(None)
    nxt = date(today.year + today.month // 12, today.month % 12 + 1, 1)
    days_in_month = (nxt - today.replace(day=1)).days
    projection = ((mtd - t) / (today.day - 1) * (days_in_month - today.day) + mtd) if today.day > 1 \
        else mtd * days_in_month
    day_b = float(budget.get("daily") or 0)
    month_b = float(budget.get("monthly") or 0)

    def pct(spent: float, b: float) -> Optional[int]:
        return round(spent / b * 100) if b else None

    return {"currency": "USD", "today": round(t, 2), "yesterday": round(y, 2) if y is not None else None,
            "month_to_date": round(mtd, 2), "month_end_projection": round(projection, 2),
            "daily_budget": day_b or None, "daily_used_pct": pct(t, day_b),
            "monthly_budget": month_b or None, "monthly_used_pct": pct(mtd, month_b)}
