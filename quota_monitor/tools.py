"""Hermes agent tools (read-only) mirroring Agent Monitor's MCP server.

Answers carry numbers and status codes only: no session titles and no upstream error text reach the calling
agent, so other sessions' content cannot steer it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

TOOLSET = "quota_monitor"


def _schema(name: str, description: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "additionalProperties": False}}


def _str(description: str) -> Dict[str, Any]:
    return {"type": "string", "description": description}


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def _safe(fn):
    def handler(args: Optional[Dict[str, Any]] = None, **_kwargs: Any) -> str:
        try:
            return _dump(fn(args or {}))
        except Exception as e:  # never leak a traceback / upstream text: the error class is enough
            return _dump({"error": "quota monitor unavailable (%s)" % type(e).__name__})
    handler.__name__ = getattr(fn, "__name__", "handler")
    return handler


def _inputs() -> Dict[str, Any]:
    from . import _routing_inputs
    return _routing_inputs()


@_safe
def get_quota_status(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import advice
    i = _inputs()
    rows = advice.quota_rows(i["items"], i["burn"], i["health"])
    provider = args.get("provider")
    if provider:
        rows = [r for r in rows if r["provider"] == provider or advice.base_provider(r["provider"]) == provider]
    return {"providers": rows}


@_safe
def get_model_health(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import advice
    return advice.model_health(_inputs()["health"], args.get("provider"), bool(args.get("include_idle")))


@_safe
def get_fallback_chains(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import advice
    return advice.fallback_chains(_inputs()["health"], args.get("profile"))


@_safe
def get_budget(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import budget_payload
    out = budget_payload()
    out.pop("generated_at", None)
    out.pop("monitor_version", None)
    return out


def _live() -> Dict[str, Any]:
    from . import live
    return live.live(record_sample=False)


@_safe
def get_running_counts(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import advice
    return advice.running_counts(_live())


@_safe
def get_agent_attention(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import advice
    return advice.agent_attention(_live())


@_safe
def recommend_model(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import advice
    i = _inputs()
    exclude = args.get("exclude_providers")
    exclude_list: List[str] = [str(x) for x in exclude] if isinstance(exclude, list) else []
    try:
        min_left = int(args.get("min_quota_left_pct", 10))
    except (TypeError, ValueError):
        min_left = 10
    try:
        top = int(args.get("top", 3))
    except (TypeError, ValueError):
        top = 3
    return advice.recommend_model(i["items"], i["burn"], i["health"], args.get("prefer_provider"), exclude_list,
                                  min_left, top)


TOOLS = (
    (get_quota_status, _schema(
        "get_quota_status",
        "Remaining quota per provider from the quota plugin's cache: % used/left per window, reset time, burn rate, "
        "projected time the window fills, and whether the provider has a usable account. Read-only, local.",
        {"provider": _str("Filter to one provider (e.g. anthropic, openai-codex, copilot).")}), "📊"),
    (get_model_health, _schema(
        "get_model_health",
        "Per-model health over the last 24 h: status (ok, degraded, failing, cooldown, account_exhausted, idle), "
        "calls, error rate, error codes, cooldowns and turn speed. Upstream error text is omitted.",
        {"provider": _str("Filter to one provider."),
         "include_idle": {"type": "boolean", "description": "Also list idle models."}}), "🩺"),
    (get_fallback_chains, _schema(
        "get_fallback_chains",
        "For each Hermes profile: primary model and each fallback with its state (ok, risky, skip) and reason code "
        "(same_backend, no_credentials, locked, cooldown, error_rate).",
        {"profile": _str("Filter to one profile.")}), "🔗"),
    (get_budget, _schema(
        "get_budget",
        "Estimated spend (Hermes + agent CLIs, API-price equivalent): today, yesterday, month to date and the "
        "month-end projection, against the daily / monthly budget set in the Quota widget.", {}), "💰"),
    (get_running_counts, _schema(
        "get_running_counts",
        "How many agent sessions and subagents run right now per source and profile, with main-session states "
        "(working, needs_approval, waiting, stalled), largest context and possible tool loops. Counts only.", {}), "🏃"),
    (get_agent_attention, _schema(
        "get_agent_attention",
        "Running agent sessions that need the user: approval, finished (your turn), stalled, repeated tool calls, "
        "very large context. Most urgent first; no titles.", {}), "🔔"),
    (recommend_model, _schema(
        "recommend_model",
        "Rank the models work can be routed to right now: healthy, usable account, quota headroom (not projected "
        "to fill before reset), proven recent use, faster median turn. Call before delegating or picking a model.",
        {"prefer_provider": _str("Provider to favour."),
         "exclude_providers": {"type": "array", "items": {"type": "string"}, "description": "Providers to skip."},
         "min_quota_left_pct": {"type": "integer", "description": "Minimum % left in every window (default 10)."},
         "top": {"type": "integer", "description": "How many candidates to return (default 3)."}}), "🧭"),
)


def register_tools(ctx: Any) -> List[str]:
    """Register every tool on a Hermes plugin context; returns the names registered."""
    done = []
    register = getattr(ctx, "register_tool", None)
    if not callable(register):
        return done
    for handler, schema, emoji in TOOLS:
        try:
            register(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
                     description=schema["description"], emoji=emoji)
            done.append(schema["name"])
        except Exception:
            continue
    return done
