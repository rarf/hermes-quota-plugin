"""Context window of an open Hermes session: how full it is (read-only, fail-open).

``used``: Hermes persists the provider-reported prompt size of the last API call on the session row
(``sessions.model_config._usage_anchor``: ``prompt_tokens`` + ``completion_tokens`` over the first
``base_count`` messages). Like Hermes' own ``anchored_context_tokens`` the figure is that anchor plus a rough
chars / 4 for the messages appended since. Without a usable anchor (old Hermes, a fresh or just-compressed
session) it falls back to chars / 4 over the whole active transcript, flagged as estimated.

``max``: the profile's ``model.context_length`` pin when it applies to the session's model, else Hermes' own
model metadata from LOCAL sources only (the Codex OAuth static table, then
``agent.context_pin.advertised_context_length``: Hermes' persisted context cache, the models.dev disk cache and
the hardcoded catalog). Never a network probe and never a cache write. Unknown -> ``None``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .common import read_config

_WINDOWS: Dict[Tuple[str, str, str], Optional[int]] = {}
_PINS: Dict[str, Tuple[Optional[str], Optional[int]]] = {}
CODEX_PROVIDERS = ("openai-codex",)
CHARS_PER_TOKEN = 4


def _positive_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _hermes_window(model: str, provider: str, base_url: str) -> Optional[int]:
    """Hermes' model metadata, local sources only. Any import / lookup failure -> None."""
    try:
        from agent import model_metadata as mm  # type: ignore
    except Exception:
        return None
    if provider in CODEX_PROVIDERS:
        try:
            ctx, _src = mm._resolve_codex_oauth_context_length_with_source(model)  # no token -> static table
            codex = _positive_int(ctx)
            if codex:
                return codex
        except Exception:
            pass
    try:
        from agent.context_pin import advertised_context_length  # type: ignore
        return _positive_int(advertised_context_length(model, base_url or ""))
    except Exception:
        return None


def profile_pin(home: Path) -> Tuple[Optional[str], Optional[int]]:
    """(configured model, model.context_length) of one profile's config.yaml, cached per process."""
    key = str(home)
    if key not in _PINS:
        cfg = read_config(home)
        m = cfg.get("model") if isinstance(cfg, dict) else None
        if isinstance(m, dict):
            name = m.get("default") or m.get("model")
            _PINS[key] = (str(name) if name else None, _positive_int(m.get("context_length")))
        else:
            _PINS[key] = (str(m) if isinstance(m, str) and m else None, None)
    return _PINS[key]


def context_max(home: Path, model: Optional[str], provider: Optional[str] = None,
                base_url: Optional[str] = None) -> Optional[int]:
    if not model:
        return None
    pinned_model, pin = profile_pin(home)
    if pin and (not pinned_model or pinned_model == model):
        return pin
    key = (str(model), str(provider or ""), str(base_url or ""))
    if key not in _WINDOWS:
        try:
            _WINDOWS[key] = _hermes_window(*key)
        except Exception:
            _WINDOWS[key] = None
    return _WINDOWS[key]


def context_percent(used: Optional[int], cap: Optional[int]) -> Optional[float]:
    if not used or not cap:
        return None
    return round(min(100.0, 100.0 * used / cap), 1)


_SIZE = "LENGTH(COALESCE(api_content, content, '')) + COALESCE(LENGTH(tool_calls), 0) + COALESCE(LENGTH(reasoning_content), 0)"


def used_tokens(c: sqlite3.Connection, sid: str, model_config: Any) -> Tuple[Optional[int], bool]:
    """(tokens in the context, estimated?) for one session."""
    anchor = None
    try:
        cfg = json.loads(model_config) if isinstance(model_config, str) and model_config else None
        anchor = cfg.get("_usage_anchor") if isinstance(cfg, dict) else None
    except (ValueError, TypeError):
        anchor = None
    if isinstance(anchor, dict):
        prompt = _positive_int(anchor.get("prompt_tokens"))
        done = _positive_int(anchor.get("completion_tokens")) or 0
        base = anchor.get("base_count")
        if prompt and isinstance(base, int) and not isinstance(base, bool) and base >= 0:
            n = c.execute("SELECT COUNT(*) FROM messages WHERE session_id = ? AND active = 1", (sid,)).fetchone()[0]
            if base <= n:
                tail = c.execute("""SELECT SUM(%s) FROM (SELECT * FROM messages WHERE session_id = ? AND active = 1
                                    ORDER BY id LIMIT -1 OFFSET ?)""" % _SIZE, (sid, base)).fetchone()[0] or 0
                return prompt + done + int(tail / CHARS_PER_TOKEN), False
    size = c.execute("SELECT SUM(%s) FROM messages WHERE session_id = ? AND active = 1" % _SIZE,
                     (sid,)).fetchone()[0] or 0
    return (int(size / CHARS_PER_TOKEN) if size else None), True


def clear_cache() -> None:
    _WINDOWS.clear()
    _PINS.clear()
