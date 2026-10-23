"""Account & model health (ported from Agent Monitor's health.py).

Read-only. Combines Hermes' credential pool (per-credential status, last error, exhausted-until,
per-model cooldowns), the profile configs (primary + fallback models), session_model_usage
(successful calls) and the API-failure lines in each profile's logs/errors.log.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .common import hermes_home, hermes_root, iso_ts, read_config, read_json

DAY = 86400
RECENT_ERROR = 3600  # an error newer than the last success within this window marks a model failing
RECOVERED_AFTER = 6 * 3600  # an error burst this old, with successful calls after it, no longer counts
DEGRADED_RATE = 0.05  # 24h error share (errors / attempts) from which a model counts as degraded

# 2026-09-29 12:50:22,860 WARNING [sid] agent.conversation_loop: API call failed (attempt 1/1) error_type=X ...
_FAIL = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+ .*?API call failed \(attempt [^)]*\)(.*)$")
_FIELD = re.compile(r"\b(error_type|provider|model)=(\S+)")
_CODE = re.compile(r"HTTP (\d{3})")


# ---------- error log (parsed incrementally per file) ----------

class _LogState:
    __slots__ = ("offset", "inode", "events")

    def __init__(self):
        self.offset = 0
        self.inode = None
        self.events = []  # (ts, provider, model, error_type, code, summary)


_logs = {}
_logs_lock = threading.Lock()


def _read_log(path: Path):
    try:
        st = path.stat()
    except OSError:
        return []
    ls = _logs.get(path)
    if ls is None or ls.inode != st.st_ino or st.st_size < ls.offset:
        ls = _LogState()
        ls.inode = st.st_ino
        _logs[path] = ls
    if st.st_size > ls.offset:
        with open(path, "rb") as f:
            f.seek(ls.offset)
            chunk = f.read(st.st_size - ls.offset)
        end = chunk.rfind(b"\n")
        if end >= 0:
            ls.offset += end + 1
            for raw in chunk[:end].decode("utf-8", "replace").split("\n"):
                m = _FAIL.match(raw)
                if not m:
                    continue
                fields = dict(_FIELD.findall(m.group(2)))
                if "provider" not in fields or "model" not in fields:
                    continue
                summary = m.group(2).split("summary=", 1)[1].strip() if "summary=" in m.group(2) else ""
                code = _CODE.search(summary)
                ls.events.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp(),
                                  fields["provider"], fields["model"], fields.get("error_type"),
                                  int(code.group(1)) if code else None, summary[:220]))
    cutoff = time.time() - 7 * DAY
    ls.events = [e for e in ls.events if e[0] >= cutoff]
    return ls.events


def _errors(homes):
    with _logs_lock:
        out = []
        for _, home in homes:
            out += _read_log(home / "logs" / "errors.log")
        return out


# ---------- config ----------

def _config(home: Path):
    return read_config(home)


# ---------- accounts ----------

def _auth_pool(home: Path) -> dict:
    data = read_json(home / "auth.json")
    pool = data.get("credential_pool") if isinstance(data, dict) else None
    return pool if isinstance(pool, dict) else {}


def read_pools(home: Optional[Path] = None) -> dict:
    """{provider: [raw credential dicts]} of the active home, the root's pool filling in providers the profile
    has no entries for (Hermes' own fallback rule). Read straight from auth.json: Hermes' ``load_pool`` may
    persist normalisations, and this module must never write a Hermes file."""
    home = Path(home) if home is not None else hermes_home()
    pool = {k: v for k, v in _auth_pool(home).items() if isinstance(v, list)}
    root = hermes_root(home)
    if root != home:
        for k, v in _auth_pool(root).items():
            if isinstance(v, list) and v and not pool.get(k):
                pool[k] = v
    return pool


def _exhausted_until(provider: str, raw: dict, sole: bool) -> Optional[float]:
    try:
        from agent.credential_pool import PooledCredential, _exhausted_until as hermes_until
        return hermes_until(PooledCredential.from_dict(provider, dict(raw)), sole_credential=sole)
    except Exception:
        pass
    if raw.get("last_status") != "exhausted":
        return None
    reset = raw.get("last_error_reset_at")
    if isinstance(reset, (int, float)):
        return float(reset) / (1000 if reset > 1e12 else 1)
    if isinstance(reset, str):
        return iso_ts(reset)
    return None


def _accounts(quota_items, pools=None):
    """Per provider: its pooled credentials (status, lock, cooldowns; never a token) + the quota reading of the
    provider's main cache row. Extra plugin account rows (``anthropic:<id>``) are separate logins outside the
    Hermes pool, so they do not lock pool credentials."""
    snap = {}
    for q in quota_items or []:
        if ":" not in q["provider"]:
            snap[q["provider"]] = q
    pools = read_pools() if pools is None else pools
    now = time.time()
    out = {}
    for prov in sorted(set(pools) | set(snap)):
        entries = [e for e in pools.get(prov) or [] if isinstance(e, dict)]
        creds = []
        for e in entries:
            cooldowns = {m: t for m, t in (e.get("model_cooldowns") or {}).items()
                         if isinstance(t, (int, float)) and t > now} if isinstance(e.get("model_cooldowns"), dict) else {}
            exp = e.get("expires_at_ms")
            creds.append({
                # Agent Monitor verified which Anthropic account each credential belongs to; the plugin does not
                # (no identity calls), so these stay None for shape compatibility.
                "account_key": None, "verification": None, "reason": None, "http": None,
                "label": e.get("label"), "source": e.get("source"), "auth_type": e.get("auth_type"),
                "priority": e.get("priority"), "status": e.get("last_status"), "status_at": e.get("last_status_at"),
                "error_code": e.get("last_error_code"),
                "error_reason": e.get("last_error_reason") or e.get("failure_reason"),
                "error_message": e.get("last_error_message"),
                "exhausted_until": _exhausted_until(prov, e, len(entries) == 1),
                "requests": e.get("request_count"),
                "expires_at": exp / 1000 if isinstance(exp, (int, float)) and exp else None,
                "model_cooldowns": cooldowns,
            })
        q = snap.get(prov, {})
        out[prov] = {"provider": prov, "credentials": creds, "plan": q.get("plan"),
                     "accounts_count": None, "credentials_count": len(creds), "account_keys": [],
                     "unavailable": q.get("unavailable"), "details": q.get("details") or [],
                     "unavailable_code": None, "unavailable_http": None,
                     "stale": bool(q.get("stale")), "fetched_at": q.get("fetched_at"), "retry_at": None,
                     "windows": q.get("windows") or [], "profiles": [], "fallback_for": []}
    return out


# ---------- models ----------

def _usage(homes, since):
    """(provider, model) -> calls/tokens/cost since `since` + last success ever (last 7 days)."""
    use = defaultdict(lambda: {"calls": 0, "tokens": 0, "cost": 0.0, "last_ok": None, "calls_7d": 0})
    for _, home in homes:
        db = home / "state.db"
        if not db.exists():
            continue
        try:
            c = sqlite3.connect("file:%s?mode=ro" % quote(str(db)), uri=True, timeout=3)
            try:
                for r in c.execute(
                        """SELECT coalesce(nullif(billing_provider,''),'unknown'), model, last_seen,
                                  api_call_count, input_tokens + output_tokens,
                                  CASE WHEN actual_cost_usd > 0 THEN actual_cost_usd ELSE estimated_cost_usd END
                           FROM session_model_usage WHERE last_seen >= ?""", (time.time() - 7 * DAY,)):
                    u = use[(r[0], r[1])]
                    u["calls_7d"] += r[3] or 0
                    u["last_ok"] = max(u["last_ok"] or 0, r[2] or 0) or None
                    if (r[2] or 0) >= since:
                        u["calls"] += r[3] or 0
                        u["tokens"] += r[4] or 0
                        u["cost"] += r[5] or 0.0
            finally:
                c.close()
        except sqlite3.Error:
            continue
    return use


def _chains(chains_cfg, accounts, models, now):
    """Per profile: the primary model and each fallback with whether Hermes could actually use it.
    Mirrors the skips Hermes logs ("resolves to the same backend", "credential pool is exhausted")."""
    out = []
    for name, prov, model_name, fallbacks in chains_cfg:
        if not prov or not model_name:
            continue
        primary = models.get((prov, model_name), {})
        entries = []
        for fb in fallbacks:
            fp, fm = fb.get("provider"), fb.get("model")
            if not fp or not fm:
                continue
            creds = accounts.get(fp, {}).get("credentials") or []
            mm = models.get((fp, fm), {})
            # reason codes are rendered (and translated) by the clients
            until = rate = None
            if fp == prov and not fb.get("base_url"):
                state, reason = "skip", "same_backend"
            elif not creds:
                state, reason = "skip", "no_credentials"
            elif all(c.get("locked") for c in creds):
                until = min((c["exhausted_until"] for c in creds if c.get("exhausted_until")), default=None)
                state, reason = "skip", "locked"
            elif mm.get("cooldown_until"):
                state, reason, until = "skip", "cooldown", mm["cooldown_until"]
            elif mm.get("status") in ("failing", "degraded"):
                state, reason, rate = "risky", "error_rate", mm.get("error_rate") or 0
            else:
                state, reason = "ok", None
            entries.append({"provider": fp, "model": fm, "state": state, "reason": reason, "until": until, "rate": rate})
        out.append({"profile": name, "provider": prov, "model": model_name,
                    "status": primary.get("status", "idle"), "fallbacks": entries,
                    "usable": sum(e["state"] != "skip" for e in entries)})
    return out


def health(homes, quota_items, pools=None):
    homes = list(homes)
    now = time.time()
    accounts = _accounts(quota_items, pools)

    models = {}

    def model(prov, name):
        key = (prov, name)
        if key not in models:
            models[key] = {"provider": prov, "model": name, "profiles": [], "fallback_for": [],
                           "calls_24h": 0, "tokens_24h": 0, "cost_24h": 0.0, "calls_7d": 0, "last_ok": None,
                           "errors_24h": 0, "errors_7d": 0, "last_error": None, "error_types": {},
                           "cooldown_until": None}
        return models[key]

    chains_cfg = []
    for name, home in homes:
        cfg = _config(home)
        m = cfg.get("model") or {}
        chains_cfg.append((name, m.get("provider"), m.get("default"),
                           [fb for fb in cfg.get("fallback_providers") or [] if isinstance(fb, dict)]))
        if m.get("provider") and m.get("default"):
            model(m["provider"], m["default"])["profiles"].append(name)
            if m["provider"] in accounts:
                accounts[m["provider"]]["profiles"].append(name)
        for i, fb in enumerate(cfg.get("fallback_providers") or []):
            if isinstance(fb, dict) and fb.get("provider") and fb.get("model"):
                model(fb["provider"], fb["model"])["fallback_for"].append(f"{name} #{i + 1}")
                if fb["provider"] in accounts and name not in accounts[fb["provider"]]["fallback_for"]:
                    accounts[fb["provider"]]["fallback_for"].append(name)

    for (prov, name), u in _usage(homes, now - DAY).items():
        if not u["calls_7d"]:
            continue
        mm = model(prov, name)
        mm.update(calls_24h=u["calls"], tokens_24h=u["tokens"], cost_24h=u["cost"], calls_7d=u["calls_7d"],
                  last_ok=u["last_ok"])

    for ts, prov, name, etype, code, summary in _errors(homes):
        mm = model(prov, name)
        mm["errors_7d"] += 1
        if ts >= now - DAY:
            mm["errors_24h"] += 1
            label = f"HTTP {code}" if code else (etype or "error")
            mm["error_types"][label] = mm["error_types"].get(label, 0) + 1
        if not mm["last_error"] or ts > mm["last_error"]["at"]:
            mm["last_error"] = {"at": ts, "type": etype, "code": code, "summary": summary}

    for prov, acc in accounts.items():
        for cred in acc["credentials"]:
            for name, until in cred["model_cooldowns"].items():
                mm = model(prov, name)
                mm["cooldown_until"] = max(mm["cooldown_until"] or 0, until)

    for acc in accounts.values():
        # a provider-reported window at 100 % blocks every request until it resets, whatever the pool says
        # (per credential when each account reports its own windows)
        for cred in acc["credentials"]:
            own = cred.pop("windows", None)
            # (a window whose reset has passed is an old reading of a window that no longer exists: never locks)
            full = [w for w in (own if own is not None else acc["windows"])
                    if (w.get("used") or 0) >= 100 and (not w.get("reset") or w["reset"] > now)]
            # an exhaustion whose lock has lapsed is retried by Hermes on the next request
            cred["locked"] = bool(full) or (
                cred["status"] == "exhausted" and (cred["exhausted_until"] is None or cred["exhausted_until"] > now))
            if full and not (cred["exhausted_until"] and cred["exhausted_until"] > now):
                cred["exhausted_until"] = min((w["reset"] for w in full if w.get("reset")), default=cred["exhausted_until"])

    # keep what matters: configured models, anything used in the last 7 days, fresh errors, cooldowns
    models = {k: m for k, m in models.items()
              if m["profiles"] or m["fallback_for"] or m["calls_7d"] or m["errors_24h"] or m["cooldown_until"]}

    for (prov, _), mm in models.items():
        creds = accounts.get(prov, {}).get("credentials") or []
        all_out = bool(creds) and all(c["locked"] for c in creds)
        tried = mm["calls_24h"] + mm["errors_24h"]
        mm["error_rate"] = mm["errors_24h"] / tried if tried else None
        err, ok = mm["last_error"], mm["last_ok"] or 0
        if mm["cooldown_until"]:
            mm["status"] = "cooldown"
        elif all_out:
            mm["status"] = "account_exhausted"
        elif err and err["at"] > ok and err["at"] >= now - RECENT_ERROR:
            mm["status"] = "failing"
        elif mm["errors_24h"] and (mm["error_rate"] or 0) >= DEGRADED_RATE and not (err and ok > err["at"] and err["at"] < now - RECOVERED_AFTER):
            mm["status"] = "degraded"
        elif ok >= now - DAY:
            mm["status"] = "ok"
        else:
            mm["status"] = "idle"

    for mm in models.values():
        # errors in the last 24h, but calls have succeeded since the last one
        err = mm["last_error"]
        mm["recovered"] = bool(mm["errors_24h"] and err and (mm["last_ok"] or 0) > err["at"]
                               and err["at"] < now - RECOVERED_AFTER)

    chains = _chains(chains_cfg, accounts, models, now)

    order = {"failing": 0, "account_exhausted": 1, "cooldown": 2, "degraded": 3, "ok": 4, "idle": 5}
    return {
        "now": now,
        "accounts": list(accounts.values()),
        "chains": chains,
        "models": sorted(models.values(), key=lambda m: (order[m["status"]], -(m["calls_24h"] or 0), m["model"])),
    }
