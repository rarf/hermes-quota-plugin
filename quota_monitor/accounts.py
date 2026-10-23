"""Accounts view: which credential each profile uses, pool order / strategy, fallbacks, sign-in gaps.

Read side (``accounts_payload``) never writes a Hermes file: ``auth.json`` and ``config.yaml`` are read as
plain files (no ``load_pool``, which can persist normalisations). Only the safe subset of each pool row is
emitted (label, auth type, source, priority, short pool id, status); tokens, refresh tokens and API keys are
never copied into the payload.

Write side (``set_priority`` / ``set_strategy``) never edits a Hermes file itself either. Each write is
validated against the current state, then delegated to Hermes' OWN CLI in a child process:

* order:    ``hermes --profile <pool owner> auth priority <provider> <id> <position>``
* strategy: ``hermes --profile <profile> config set credential_pool_strategies.<provider> <strategy>``

and the result is re-read from disk, logged in ``quota_monitor.db`` and returned with the new state.

Hermes facts this module mirrors (``agent/credential_pool.py``, ``hermes_cli/auth.py``):

* a named profile with no rows of its own for a provider BORROWS the root's rows (``read_credential_pool``'s
  global-root fallback); a profile with its own rows owns them. Order therefore belongs to the pool OWNER and a
  reorder of the shared pool affects every profile that borrows it.
* the selection strategy is per profile: ``credential_pool_strategies.<provider>`` in that profile's config
  (default ``fill_first``).
* the fallback chain (``fallback_providers``) has no non-interactive reorder command (``hermes fallback`` is a
  picker), so it is reported read-only here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .common import hermes_home, hermes_root, iso_ts, num, profile_homes, read_config, read_json

logger = logging.getLogger(__name__)

ROOT_PROFILE = "default"
DEFAULT_STRATEGY = "fill_first"
# Hermes' SUPPORTED_POOL_STRATEGIES, in the order its own `hermes auth` menu lists them.
FALLBACK_STRATEGIES = ("fill_first", "round_robin", "least_used", "random")
CLI_TIMEOUT_S = 60
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_POOL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Row fields that may leave this module. Everything else (tokens, keys, base urls, extra blobs) stays out.
_SAFE_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{0,64}$")

# Quota-cache `unavailable_reason` -> what the user can do about it.
SIGNIN_REASONS = {"no-credentials", "not-logged-in", "no-session-cookies", "config-invalid"}
REAUTH_REASONS = {"auth-failed", "reauth-required", "token-refresh-failed", "http-401"}
OPTIN_REASONS = {"opt-in-disabled"}
RATE_REASONS = {"rate-limited", "http-429"}
ANTHROPIC_BACKOFF_FILE = "quota_anthropic_backoff.json"
ANTHROPIC_COOLDOWN_CAP_S = 6 * 3600

PROVIDERS_ACCOUNTS_PATH = "/settings?tab=providers&pview=accounts"
PROVIDERS_KEYS_PATH = "/settings?tab=providers&pview=keys"
FALLBACK_SETTINGS_PATH = "/settings?tab=config:model&page=fallbacks"

# How each quota provider gets credentials. `hermes`: a Hermes provider id for `hermes auth add <id>`
# (verified against `hermes auth add --help`); `settings`: the desktop Settings page that adds it;
# `external`: a login that happens in another tool (the widget shows it as a hint, Hermes cannot run it).
SIGNIN_HINTS: Dict[str, Dict[str, Any]] = {
    "anthropic": {"hermes": "anthropic", "settings": PROVIDERS_ACCOUNTS_PATH},
    "nous": {"hermes": "nous", "settings": PROVIDERS_ACCOUNTS_PATH},
    "openai-codex": {"hermes": "openai-codex", "settings": PROVIDERS_ACCOUNTS_PATH},
    "openrouter": {"hermes": "openrouter", "settings": PROVIDERS_KEYS_PATH},
    "kimi": {"hermes": "kimi-coding", "settings": PROVIDERS_KEYS_PATH},
    "zai": {"hermes": "zai", "settings": PROVIDERS_KEYS_PATH},
    "deepseek": {"hermes": "deepseek", "settings": PROVIDERS_KEYS_PATH},
    "minimax": {"hermes": "minimax", "settings": PROVIDERS_KEYS_PATH},
    "opencode-go": {"hermes": "opencode-go", "settings": PROVIDERS_KEYS_PATH},
    "commandcode": {"hermes": "commandcode", "settings": PROVIDERS_KEYS_PATH},
    "ollama": {"hermes": "ollama-cloud", "settings": PROVIDERS_KEYS_PATH},
    "copilot": {"external": "gh auth login", "external_tool": "GitHub CLI"},
    "gemini": {"external": "gemini", "external_tool": "Gemini CLI"},
    "cursor": {"external": "cursor-agent login", "external_tool": "Cursor Agent CLI"},
    "grok": {"external_tool": "browser", "optin": "plugins.entries.quota.settings.grokEnabled"},
    "antigravity": {"external_tool": "Antigravity", "optin": "plugins.entries.quota.settings.antigravityEnabled"},
    "experientiallabs": {"env": "EXPLABS_API_KEY", "settings": "/settings?tab=keys&key=EXPLABS_API_KEY"},
}


class AccountsError(ValueError):
    """A rejected write: ``code`` is machine readable, the message is for humans."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra


# ---------- small readers ----------

def supported_strategies() -> List[str]:
    try:
        from agent.credential_pool import SUPPORTED_POOL_STRATEGIES  # type: ignore
        known = [s for s in FALLBACK_STRATEGIES if s in SUPPORTED_POOL_STRATEGIES]
        return known + sorted(s for s in SUPPORTED_POOL_STRATEGIES if s not in known)
    except Exception:
        return list(FALLBACK_STRATEGIES)


def _auth_pool(home: Path) -> Dict[str, List[Dict[str, Any]]]:
    data = read_json(home / "auth.json")
    pool = data.get("credential_pool") if isinstance(data, dict) else None
    if not isinstance(pool, dict):
        return {}
    return {str(k): [e for e in v if isinstance(e, dict)] for k, v in pool.items() if isinstance(v, list) and v}


def _profile_name(home: Path, root: Path) -> str:
    try:
        if home.resolve() == root.resolve():
            return ROOT_PROFILE
    except OSError:
        pass
    return home.name if home.parent.name == "profiles" else ROOT_PROFILE


def active_profile() -> str:
    home = hermes_home()
    return _profile_name(home, hermes_root(home))


def _safe_text(value: Any, limit: int = 80) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if c.isprintable()).strip()
    return text[:limit] or None


def _strategies(cfg: Dict[str, Any]) -> Dict[str, str]:
    raw = cfg.get("credential_pool_strategies")
    if not isinstance(raw, dict):
        return {}
    allowed = set(supported_strategies())
    out = {}
    for k, v in raw.items():
        s = str(v or "").strip().lower()
        if s in allowed:
            out[str(k)] = s
    return out


def _model(cfg: Dict[str, Any]) -> Dict[str, Optional[str]]:
    m = cfg.get("model")
    if isinstance(m, str):
        return {"provider": None, "model": _safe_text(m, 120)}
    if not isinstance(m, dict):
        return {"provider": None, "model": None}
    return {"provider": _safe_text(m.get("provider")), "model": _safe_text(m.get("default") or m.get("model"), 120)}


def _fallbacks(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    chain = cfg.get("fallback_providers")
    if not isinstance(chain, list) or not chain:
        legacy = cfg.get("fallback_model")
        chain = [legacy] if isinstance(legacy, dict) else []
    out = []
    for fb in chain:
        if isinstance(fb, dict) and fb.get("provider") and fb.get("model"):
            # base_url / api_key / key_env never leave this module; only whether a custom route exists
            out.append({"provider": _safe_text(fb.get("provider")), "model": _safe_text(fb.get("model"), 120),
                        "custom_route": bool(fb.get("base_url"))})
        elif isinstance(fb, str) and "/" in fb:
            prov, _, model = fb.partition("/")
            out.append({"provider": _safe_text(prov), "model": _safe_text(model, 120), "custom_route": False})
    return out


def _until(raw: Dict[str, Any], provider: str, sole: bool) -> Optional[float]:
    try:
        from agent.credential_pool import PooledCredential, _exhausted_until  # type: ignore
        return _exhausted_until(PooledCredential.from_dict(provider, dict(raw)), sole_credential=sole)
    except Exception:
        pass
    if raw.get("last_status") != "exhausted":
        return None
    reset = raw.get("last_error_reset_at")
    if isinstance(reset, (int, float)):
        return float(reset) / (1000 if reset > 1e12 else 1)
    return iso_ts(reset) if isinstance(reset, str) else None


def _row(provider: str, e: Dict[str, Any], index: int, sole: bool, now: float) -> Dict[str, Any]:
    cooldowns = e.get("model_cooldowns")
    if not isinstance(cooldowns, dict):
        cooldowns = {}
    live_cd = {str(m): float(t) for m, t in cooldowns.items()
               if isinstance(t, (int, float)) and not isinstance(t, bool) and t > now}
    until = _until(e, provider, sole)
    # exhausted = Hermes skips the credential until `until`; cooldown = only some models are parked
    if e.get("last_status") == "exhausted" and (until is None or until > now):
        status = "exhausted"
    elif live_cd:
        status = "cooldown"
    else:
        status = "active"
    source = e.get("source")
    raw_id = e.get("id")
    return {
        "id": raw_id if isinstance(raw_id, str) and _POOL_ID_RE.match(raw_id) else None,
        "label": _safe_text(e.get("label")) or "#%d" % (index + 1),
        "auth_type": _safe_text(e.get("auth_type"), 24),
        "source": source if isinstance(source, str) and _SAFE_SOURCE_RE.match(source) else None,
        "priority": e.get("priority") if isinstance(e.get("priority"), int) else index,
        "position": index,
        "status": status,
        "status_until": until if status == "exhausted" else (max(live_cd.values()) if live_cd else None),
        "cooldown_models": sorted(live_cd)[:6],
        "last_error_code": e.get("last_error_code") if isinstance(e.get("last_error_code"), int) else None,
        "last_error_reason": _safe_text(e.get("last_error_reason") or e.get("failure_reason"), 60),
        "requests": e.get("request_count") if isinstance(e.get("request_count"), int) else None,
        "tried_first": False,
        "windows": [],
        "quota_match": None,
    }


def _ordered(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Hermes loads a pool sorted by priority (stable for ties)."""
    def key(item):
        i, e = item
        p = e.get("priority")
        return (p if isinstance(p, int) else 10 ** 6, i)
    return [e for _, e in sorted(enumerate(entries), key=key)]


# ---------- quota matching ----------

def _norm_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _quota_index(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{base provider: {"main": item|None, "by_label": {label: item}}}"""
    out: Dict[str, Dict[str, Any]] = {}
    for it in items or []:
        base = it.get("base") or str(it.get("provider", "")).split(":", 1)[0]
        slot = out.setdefault(base, {"main": None, "by_label": {}})
        if it.get("provider") == base:
            slot["main"] = it
        for lab in (it.get("account_label"), it.get("name") if it.get("provider") != base else None):
            if lab:
                slot["by_label"].setdefault(_norm_label(lab), it)
    return out


def _attach_quota(provider: str, rows: List[Dict[str, Any]], qidx: Dict[str, Dict[str, Any]]) -> None:
    slot = qidx.get(provider)
    if not slot:
        return
    for r in rows:
        it = slot["by_label"].get(_norm_label(r["label"]))
        if it and not it.get("unavailable") and it.get("windows"):
            r["windows"], r["quota_match"] = it["windows"], "label"
    main = slot["main"]
    if len(rows) == 1 and not rows[0]["windows"] and main and not main.get("unavailable") and main.get("windows"):
        rows[0]["windows"], rows[0]["quota_match"] = main["windows"], "sole"


def _provider_quota(provider: str, qidx: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    slot = qidx.get(provider)
    main = slot and slot["main"]
    if not main:
        return None
    return {"windows": main.get("windows") or [], "plan": _safe_text(main.get("plan"), 40),
            "unavailable": main.get("unavailable"), "stale": bool(main.get("stale")),
            "fetched_at": main.get("fetched_at")}


# ---------- unavailable providers ----------

def _anthropic_retry_at(root: Path, now: float) -> Optional[float]:
    best = None
    for home in (root, hermes_home()):
        data = read_json(home / ANTHROPIC_BACKOFF_FILE)
        if not isinstance(data, dict):
            continue
        for v in data.values():
            t = num(v)
            if t is not None and not isinstance(v, bool) and now < t <= now + ANTHROPIC_COOLDOWN_CAP_S:
                best = max(best or 0.0, t)
    return best


def reason_kind(reason: Optional[str]) -> str:
    r = (reason or "").strip().lower()
    if r in SIGNIN_REASONS:
        return "signin"
    if r in REAUTH_REASONS or r == "http-403":
        return "reauth"
    if r in OPTIN_REASONS:
        return "optin"
    if r in RATE_REASONS:
        return "rate_limited"
    return "error"


def signin_action(provider: str, kind: str) -> Dict[str, Any]:
    hint = SIGNIN_HINTS.get(provider, {})
    action: Dict[str, Any] = {"settings_path": None, "command": None, "external_tool": hint.get("external_tool"),
                              "optin_key": None, "env": hint.get("env")}
    if kind == "optin":
        key = hint.get("optin")
        if key:
            action["optin_key"] = key
            action["command"] = "hermes config set %s true" % key
        return action
    if kind not in ("signin", "reauth"):
        return action
    if hint.get("hermes"):
        action["command"] = "hermes auth add %s" % hint["hermes"]
    elif hint.get("external"):
        action["command"] = hint["external"]
    action["settings_path"] = hint.get("settings") or (PROVIDERS_ACCOUNTS_PATH if hint.get("hermes") else None)
    return action


def _unavailable(items: List[Dict[str, Any]], root: Path, now: float) -> List[Dict[str, Any]]:
    out = []
    for it in items or []:
        reason = it.get("unavailable")
        if not reason:
            continue
        key = str(it.get("provider") or "")
        base = it.get("base") or key.split(":", 1)[0]
        kind = reason_kind(reason)
        retry_at = None
        if kind == "rate_limited" and base == "anthropic":
            retry_at = _anthropic_retry_at(root, now)
        entry = {"provider": key, "base": base, "account": key.split(":", 1)[1] if ":" in key else None,
                 "account_label": _safe_text(it.get("account_label")) if ":" in key else None,
                 "reason": str(reason)[:60], "kind": kind, "retry_at": retry_at}
        entry.update(signin_action(base, kind))
        out.append(entry)
    order = {"signin": 0, "reauth": 1, "optin": 2, "rate_limited": 3, "error": 4}
    out.sort(key=lambda e: (order.get(e["kind"], 9), e["provider"]))
    return out


# ---------- the payload ----------

def _pools(homes: List[Tuple[str, Path]], root: Path, now: float, configs: Dict[str, Dict[str, Any]],
           qidx: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]:
    """One entry per (provider, owner) pool + {profile: {provider: own|shared}}."""
    root_pool = _auth_pool(root)
    own = {name: (root_pool if name == ROOT_PROFILE else _auth_pool(home)) for name, home in homes}
    providers = sorted({p for pools in own.values() for p in pools})
    pools: List[Dict[str, Any]] = []
    access: Dict[str, Dict[str, str]] = {name: {} for name, _ in homes}
    for prov in providers:
        owners = [name for name, _ in homes if own[name].get(prov)]
        for owner in owners:
            if owner == ROOT_PROFILE:
                users = [name for name, _ in homes if name == ROOT_PROFILE or not own[name].get(prov)]
            else:
                users = [owner]
            for u in users:
                access[u][prov] = "own" if u == owner else "shared"
            entries = _ordered(own[owner][prov])
            rows = [_row(prov, e, i, len(entries) == 1, now) for i, e in enumerate(entries)]
            first = next((r for r in rows if r["status"] == "active"), None)
            if first is not None:
                first["tried_first"] = True
            _attach_quota(prov, rows, qidx)
            strategies = {u: _strategies(configs.get(u, {})).get(prov, DEFAULT_STRATEGY) for u in users}
            pools.append({
                "provider": prov, "owner": owner, "shared": owner == ROOT_PROFILE and len(users) > 1,
                "used_by": users, "strategies": strategies,
                "explicit_strategy": {u: prov in _strategies(configs.get(u, {})) for u in users},
                "rows": rows, "quota": _provider_quota(prov, qidx),
                "writable": all(r["id"] for r in rows) and len(rows) > 1,
            })
    return pools, access


def accounts_payload(items: Optional[List[Dict[str, Any]]] = None, now: Optional[float] = None,
                     root: Optional[Path] = None) -> Dict[str, Any]:
    from . import quotas

    now = time.time() if now is None else now
    root = Path(root) if root is not None else hermes_root()
    homes = profile_homes(root)
    errors: Dict[str, str] = {}
    if items is None:
        try:
            items = quotas.items_from_cache()
        except Exception as e:  # the pool listing must still render without a cache
            items = []
            errors["quota_cache"] = ("%s: %s" % (type(e).__name__, e))[:200]
    configs = {name: read_config(home) for name, home in homes}
    qidx = _quota_index(items)
    pools, access = _pools(homes, root, now, configs, qidx)
    profiles = []
    for name, _home in homes:
        cfg = configs.get(name, {})
        profiles.append({
            "profile": name, "is_root": name == ROOT_PROFILE,
            "model": _model(cfg), "fallbacks": _fallbacks(cfg), "fallback_editable": False,
            "fallback_command": "hermes --profile %s fallback" % name,
            "strategies": _strategies(cfg), "pools": access.get(name, {}),
        })
    return {
        "active_profile": active_profile(), "root_profile": ROOT_PROFILE,
        "strategies": supported_strategies(), "default_strategy": DEFAULT_STRATEGY,
        "pools": pools, "profiles": profiles, "unavailable": _unavailable(items, root, now),
        "settings_paths": {"providers": PROVIDERS_ACCOUNTS_PATH, "keys": PROVIDERS_KEYS_PATH,
                           "fallbacks": FALLBACK_SETTINGS_PATH},
        "capabilities": {"set_priority": True, "set_strategy": True, "reorder_fallbacks": False,
                         "assign_account_to_profile": False},
        "errors": errors,
    }


# ---------- writes ----------

Runner = Callable[[List[str], Dict[str, str]], Tuple[int, str]]


def hermes_cli_base() -> Tuple[List[str], Dict[str, str]]:
    """How to start Hermes' own CLI from inside a Hermes process (the same re-exec ``cli.exec`` uses)."""
    try:
        import hermes_cli  # type: ignore
        agent_root = str(Path(hermes_cli.__file__).resolve().parent.parent)
        pp = os.environ.get("PYTHONPATH", "")
        return [sys.executable, "-m", "hermes_cli.main"], {
            "PYTHONPATH": agent_root + (os.pathsep + pp if pp else "")}
    except Exception:
        exe = shutil.which("hermes")
        if not exe:
            raise AccountsError("hermes-cli-missing", "the Hermes CLI could not be located")
        return [exe], {}


def _default_runner(argv: List[str], extra_env: Dict[str, str]) -> Tuple[int, str]:
    base, env_add = hermes_cli_base()
    env = dict(os.environ)
    env.update(env_add)
    env.update(extra_env)
    env.setdefault("NO_COLOR", "1")
    proc = subprocess.run(base + argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=env, timeout=CLI_TIMEOUT_S)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _scrub(text: str) -> str:
    """CLI output goes back to the widget: cut it short and drop anything token-shaped."""
    text = re.sub(r"(sk-[A-Za-z0-9_-]{6,}|[A-Za-z0-9_-]{40,})", "[redacted]", text or "")
    return text.strip()[-600:]


def _audit(action: str, profile: str, provider: str, detail: Dict[str, Any]) -> None:
    logger.info("quota accounts %s profile=%s provider=%s %s", action, profile, provider, json.dumps(detail))
    try:
        from . import store
        store.audit(action, profile, provider, detail)
    except Exception:  # the write already happened; a log failure must not report it as failed
        pass


def _check_provider(provider: Any) -> str:
    p = str(provider or "").strip()
    if not _PROVIDER_RE.match(p):
        raise AccountsError("bad-provider", "invalid provider id: %r" % (provider,))
    return p


def _check_profile(profile: Any, state: Dict[str, Any]) -> str:
    p = str(profile or "").strip().lower()
    if not _PROFILE_RE.match(p):
        raise AccountsError("bad-profile", "invalid profile name: %r" % (profile,))
    if p not in {x["profile"] for x in state["profiles"]}:
        raise AccountsError("unknown-profile", "no profile named %r on this machine" % p)
    return p


def _pool_for(state: Dict[str, Any], provider: str, owner: str) -> Optional[Dict[str, Any]]:
    return next((p for p in state["pools"] if p["provider"] == provider and p["owner"] == owner), None)


def set_priority(provider: str, target: str, priority: Any, profile: Optional[str] = None,
                 runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Move pool row ``target`` (its short pool id) of ``provider`` to zero-based ``priority`` in the pool OWNED by
    ``profile`` (default: the active profile), through ``hermes --profile <owner> auth priority``."""
    state = accounts_payload()
    provider = _check_provider(provider)
    owner = _check_profile(profile or active_profile(), state)
    pool = _pool_for(state, provider, owner)
    if pool is None:
        lender = next((p for p in state["pools"] if p["provider"] == provider and owner in p["used_by"]), None)
        if lender is not None:
            raise AccountsError(
                "borrowed-pool",
                "profile %r has no %s credentials of its own: it borrows the pool of %r. Reorder that pool "
                "instead (it applies to every profile sharing it)." % (owner, provider, lender["owner"]),
                owner=lender["owner"], used_by=lender["used_by"])
        raise AccountsError("no-pool", "profile %r has no %s credential pool" % (owner, provider))
    target = str(target or "").strip()
    rows = pool["rows"]
    match = [r for r in rows if r["id"] and r["id"] == target]
    if len(match) != 1:
        raise AccountsError("unknown-target", "no %s credential with pool id %r in %r's pool" % (provider, target, owner))
    if isinstance(priority, bool):
        raise AccountsError("bad-priority", "priority must be an integer")
    try:
        new_pos = int(str(priority).strip())
    except (TypeError, ValueError):
        raise AccountsError("bad-priority", "priority must be an integer, got %r" % (priority,))
    if not 0 <= new_pos < len(rows):
        raise AccountsError("bad-priority", "priority must be between 0 and %d" % (len(rows) - 1))
    before = [r["id"] for r in rows]
    cur_pos = before.index(target)
    affected = list(pool["used_by"])
    command = ["--profile", owner, "auth", "priority", provider, target, str(new_pos)]
    result: Dict[str, Any] = {"action": "set-priority", "provider": provider, "owner": owner, "target": target,
                              "requested": new_pos, "affected_profiles": affected, "shared": pool["shared"],
                              "command": "hermes " + " ".join(command), "before": before}
    if cur_pos == new_pos:
        result.update(changed=False, after=before, effective=cur_pos, cli_output="", state=state)
        return result
    code, output = (runner or _default_runner)(command, {})
    if code != 0:
        raise AccountsError("cli-failed", "hermes auth priority failed (exit %s): %s" % (code, _scrub(output)),
                            command=result["command"])
    new_state = accounts_payload()
    new_pool = _pool_for(new_state, provider, owner)
    after = [r["id"] for r in (new_pool or {}).get("rows", [])]
    effective = after.index(target) if target in after else None
    result.update(changed=after != before, after=after, effective=effective, cli_output=_scrub(output),
                  state=new_state)
    if effective is not None and effective != new_pos:
        result["note"] = "effective-position-differs"
    _audit("set-priority", owner, provider, {"target": target, "from": cur_pos, "to": new_pos,
                                             "effective": effective, "before": before, "after": after,
                                             "affected": affected})
    return result


def set_strategy(provider: str, strategy: str, profile: Optional[str] = None,
                 runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Set ``credential_pool_strategies.<provider>`` in ONE profile's config via ``hermes config set``."""
    state = accounts_payload()
    provider = _check_provider(provider)
    target = _check_profile(profile or active_profile(), state)
    strategy = str(strategy or "").strip().lower()
    if strategy not in state["strategies"]:
        raise AccountsError("bad-strategy", "strategy must be one of %s" % ", ".join(state["strategies"]))
    prof = next(p for p in state["profiles"] if p["profile"] == target)
    if provider not in prof["pools"]:
        raise AccountsError("no-pool", "profile %r sees no %s credentials; a strategy would have no effect"
                            % (target, provider))
    before = prof["strategies"].get(provider, DEFAULT_STRATEGY)
    key = "credential_pool_strategies." + provider.replace(".", "\\.")
    command = ["--profile", target, "config", "set", key, strategy]
    result: Dict[str, Any] = {"action": "set-strategy", "provider": provider, "profile": target,
                              "before": before, "requested": strategy, "affected_profiles": [target],
                              "command": "hermes " + " ".join(command)}
    if before == strategy and prof["strategies"].get(provider) == strategy:
        result.update(changed=False, after=before, cli_output="", state=state)
        return result
    code, output = (runner or _default_runner)(command, {})
    if code != 0:
        raise AccountsError("cli-failed", "hermes config set failed (exit %s): %s" % (code, _scrub(output)),
                            command=result["command"])
    new_state = accounts_payload()
    new_prof = next((p for p in new_state["profiles"] if p["profile"] == target), {"strategies": {}})
    after = new_prof["strategies"].get(provider, DEFAULT_STRATEGY)
    if after != strategy:
        raise AccountsError("not-applied", "Hermes reported success but %s's %s strategy is still %s"
                            % (target, provider, after), command=result["command"])
    result.update(changed=after != before, after=after, cli_output=_scrub(output), state=new_state)
    _audit("set-strategy", target, provider, {"from": before, "to": after})
    return result
