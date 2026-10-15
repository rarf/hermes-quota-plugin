"""CommandCode quota fetcher using the CLI's authenticated billing routes.

Command Code's published usage-limits page documents the 5-hour and weekly
windows, while the official Command Code CLI 1.53.0 bundle is the primary
source for the request shape used here: it calls ``/alpha/whoami?limits=1``,
scopes billing and summary requests with the returned ``org.id``, and passes
``currentPeriodStart`` as ``since`` for the usage summary.  The bundle also
contains the plan catalog used below.  These are undocumented alpha routes;
unknown response shapes and plan IDs therefore fail closed instead of being
turned into invented labels or percentages.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .base import (
    GENERATED_ACCOUNT_ID_PREFIX,
    QuotaAccount,
    QuotaResult,
    QuotaWindow,
    build_unavailable,
    urlopen_no_redirect,
)
from .registry import register as _register

_PROVIDER = "commandcode"
_AUTH_PATH = os.path.join(os.path.expanduser("~"), ".commandcode", "auth.json")
_BASE = "https://api.commandcode.ai"
_UA = "command-code/1.53.0"
# Keep one provider's complete request group well below quota_cache's 20-second
# sweep budget.  The batch helpers below use daemon threads so an ignored socket
# cannot keep the CLI process alive after this deadline expires.
_REQUEST_DEADLINE_S = 10.0
_TIMEOUT = 5.0

# Upper bounds for one multi-card sweep. At most _MAX_POOL_ROWS saved
# credentials are read (the remainder becomes one typed ``config-limit`` row,
# mirroring builtin's claudeAccounts cap) and at most _MAX_CONCURRENT_FETCHES
# credential fetches run at once, so a large saved pool cannot fan out
# unbounded threads or sockets inside the shared refresh budget.
_MAX_POOL_ROWS = 8
_MAX_CONCURRENT_FETCHES = 4
_POOL_LIMIT_REASON = "config-limit"
_POOL_LIMIT_DETAIL = (
    f"Only the first {_MAX_POOL_ROWS} saved CommandCode credentials are read."
)

# planId -> (display name, monthly included credits in USD).  These IDs and
# allowances mirror the official CLI 1.53.0 usage view.  In particular,
# individual-pro and individual-pro-v1 are distinct published pools.
_PLANS: dict[str, tuple[str, float]] = {
    "individual-go": ("Go", 10.0),
    "individual-goat": ("GOAT", 70.0),
    "individual-pro": ("Pro", 30.0),
    "individual-pro-v1": ("Pro", 80.0),
    "individual-provider": ("Provider", 15.0),
    "individual-max": ("Max", 150.0),
    "individual-ultra": ("Ultra", 300.0),
    "teams-pro": ("Teams Pro", 40.0),
}


class _RequestTimeout(Exception):
    """A provider-local deadline expired before a request completed."""



def _load_api_key() -> Optional[str]:
    # Prefer the official CLI identity when present; otherwise use the same
    # credential selection as Hermes (env and credential pool).
    try:
        with open(_AUTH_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            key = data.get("apiKey")
            if isinstance(key, str) and key.strip():
                return key.strip()
    except (OSError, ValueError):
        pass

    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, _resolve_api_key_provider_secret

        config = PROVIDER_REGISTRY.get(_PROVIDER)
        if config is not None and getattr(config, "auth_type", "") == "api_key":
            key, _source = _resolve_api_key_provider_secret(_PROVIDER, config)
            if isinstance(key, str) and key.strip():
                return key.strip()
    except Exception:  # Hermes core may be absent or credential storage unavailable.
        pass
    return None


def _clean_text(value: Any, limit: int = 64) -> str:
    """Strip control characters and bound a saved row's display text.

    Dropped: every Cc (C0/C1 controls, DEL), Cf (bidi/format characters such as
    U+202E or zero-width joiners), Cs (lone surrogates) and Zl/Zp
    line/paragraph separators. Truncation happens after filtering, so it cannot
    split a surrogate pair into a lone surrogate. Display hardening only: the
    widget still escapes the resulting text.
    """
    if not isinstance(value, str):
        return ""
    text = "".join(
        char
        for char in value
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
    )
    return text.strip()[:limit]


def _pool_rows_from_store() -> list[dict[str, Any]]:
    """Enumerate saved rows straight from ``auth.json`` (read-only fallback).

    Only used when the Hermes core is not importable. The profile pool wins
    over the global one, mirroring the core's own precedence; each store's
    ``credential_pool`` is validated on its own, so a malformed profile pool is
    an empty pool instead of an error that hides a valid global one — never a
    write, never a fatal error.
    """
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home
    except Exception:  # noqa: BLE001 - core absent, nothing to read
        return []
    stores: list[tuple[Any, dict[str, Any]]] = []
    for home in (get_hermes_home(), get_default_hermes_root()):
        if home in [known for known, _ in stores]:
            continue
        try:
            with open(os.path.join(str(home), "auth.json"), "r", encoding="utf-8-sig") as fh:
                store = json.load(fh)
        except (OSError, ValueError, UnicodeError):
            store = {}
        stores.append((home, store if isinstance(store, dict) else {}))

    def rows_from(store: dict[str, Any]) -> list[dict[str, Any]]:
        # Each store's pool is validated on its own: a non-object
        # ``credential_pool`` (or a non-list provider entry) is an empty pool,
        # never an error, and never hides the other store's rows.
        pool = store.get("credential_pool")
        entries = pool.get(_PROVIDER) if isinstance(pool, dict) else None
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    local_rows = rows_from(stores[0][1]) if stores else []
    global_rows = rows_from(stores[1][1]) if len(stores) > 1 else []
    return local_rows or global_rows


def _pool_rows() -> list[dict[str, Any]]:
    """One row per saved CommandCode credential, without selecting/rotating.

    The core reader is profile-aware; the direct read is the read-only fallback
    when the core is not importable. The row source does not matter: env-backed
    rows are re-resolved through the current env/dotenv precedence in
    ``_credential_rows`` either way.
    """
    try:
        from hermes_cli.auth import read_credential_pool

        return [row for row in (read_credential_pool(_PROVIDER) or []) if isinstance(row, dict)]
    except Exception:  # noqa: BLE001 - core absent or the saved store is unreadable
        return _pool_rows_from_store()


def _env_secret(name: str) -> Optional[str]:
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv

        return get_env_value_prefer_dotenv(name)
    except Exception:  # noqa: BLE001 - core absent; the process env still counts
        return os.environ.get(name)


def _credential_rows() -> list[tuple[Optional[str], str, str]]:
    """(account id, display label, secret) for every saved row, deduplicated.

    Env-backed rows (``source`` starting with ``env:``) resolve through the
    current native env/dotenv precedence first, so a rotated environment key
    replaces a stale persisted token; the persisted token is only the fallback
    when the environment value is missing. Equality-only deduplication by
    secret: two rows that point at the same credential are one card, and no
    id/hash/suffix is ever derived here.
    """
    rows: list[tuple[Optional[str], str, str]] = []
    seen: set[str] = set()
    for entry in _pool_rows():
        secret: Optional[str] = None
        source = entry.get("source")
        if isinstance(source, str) and source.startswith("env:"):
            env_name = source.split(":", 1)[1].strip()
            value = _env_secret(env_name) if env_name else None
            if isinstance(value, str) and value.strip():
                secret = value
        if secret is None:
            persisted = entry.get("access_token") or entry.get("runtime_api_key")
            if isinstance(persisted, str) and persisted.strip():
                secret = persisted
        if secret is None:
            continue
        secret = secret.strip()
        if secret in seen:
            continue
        seen.add(secret)
        account_id = _clean_text(entry.get("id"))
        rows.append(
            (account_id or None, _clean_text(entry.get("label")) or account_id, secret)
        )
    return rows


def _get(path: str, key: str) -> Any:
    req = urllib.request.Request(
        _BASE + path,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    with urlopen_no_redirect(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))



def _with_query(path: str, **params: Optional[str]) -> str:
    encoded = [(name, value) for name, value in params.items() if value is not None]
    if not encoded:
        return path
    return f"{path}?{urllib.parse.urlencode(encoded)}"



def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)



def _iso_from_ms(value: Any) -> Optional[str]:
    ms = _number(value)
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()



def _iso_from_str(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()



def _rolling_window(block: Any, label: str) -> Optional[QuotaWindow]:
    if not isinstance(block, dict):
        return None
    used = _number(block.get("used"))
    cap = _number(block.get("cap"))
    if used is None or cap is None or cap <= 0:
        return None
    pct = max(0.0, min(100.0, used / cap * 100.0))
    return QuotaWindow(
        label=label,
        used_percent=round(pct, 2),
        reset_at=_iso_from_ms(block.get("resetAt")),
    )



def _credit_block(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("credits"), dict):
        return {}
    return payload["credits"]



def _window_limits(payload: Any) -> dict[str, Any]:
    """Accept both observed alpha response locations for rolling limits."""
    if not isinstance(payload, dict):
        return {}
    top_level = payload.get("windowLimits")
    nested = _credit_block(payload).get("windowLimits")
    if isinstance(top_level, dict) and isinstance(nested, dict):
        # The official CLI reads the nested form; retain any top-level fields
        # from older captures while letting the nested response win conflicts.
        return {**top_level, **nested}
    if isinstance(nested, dict):
        return nested
    if isinstance(top_level, dict):
        return top_level
    return {}



def _subscription_data(payload: Any) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if "planId" in payload:
        return payload
    return None



def _plan_id(credits_payload: Any, subscription: Any) -> Optional[str]:
    candidates = [subscription, _credit_block(credits_payload), credits_payload]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("planId")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None



def _cycle_window(
    credits: Any, subscription: Any, plan_id: Optional[str]
) -> tuple[Optional[QuotaWindow], list[str]]:
    """Build a cycle window only when the plan supplies a real denominator."""
    if not isinstance(credits, dict):
        return None, []
    remaining = _number(credits.get("monthlyCredits"))
    if remaining is None:
        return None, []
    plan = _PLANS.get(plan_id or "")
    if plan is None or plan[1] <= 0:
        return None, [f"Cycle credits left: ${remaining:.2f}"]
    pool = plan[1]
    if not (0.0 <= remaining <= pool):
        # A balance above the included pool may contain rollover or other
        # carry-over.  It is still useful as a balance, but not a percentage.
        return None, [f"Cycle credits left: ${remaining:.2f} (pool ${pool:.2f})"]
    used_pct = max(0.0, min(100.0, (pool - remaining) / pool * 100.0))
    reset_at = None
    if isinstance(subscription, dict):
        reset_at = _iso_from_str(subscription.get("currentPeriodEnd"))
    return (
        QuotaWindow(label="Cycle", used_percent=round(used_pct, 2), reset_at=reset_at),
        [f"${remaining:.2f} of ${pool:.2f} cycle credits left"],
    )



def _org_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("org"), dict):
        return None
    value = payload["org"].get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None



def _request_reason(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (401, 403):
            return "auth-failed"
        return f"http-{error.code}"
    if isinstance(error, _RequestTimeout):
        return "timeout"
    return f"fetch-error:{type(error).__name__}"



def _request_batch(
    requests: list[tuple[str, str]], key: str, deadline: float
) -> dict[str, tuple[Any, Optional[Exception]]]:
    """Run requests concurrently and return before the provider deadline.

    Each worker is a daemon because ``urllib`` cannot cancel an in-flight socket
    read.  The result is what the provider observes; a late worker cannot
    overwrite a timeout result after the caller has moved on.
    """
    if time.monotonic() >= deadline:
        timeout = _RequestTimeout()
        return {name: (None, timeout) for name, _path in requests}

    outcomes: dict[str, tuple[Any, Optional[Exception]]] = {}
    events: dict[str, threading.Event] = {}
    lock = threading.Lock()

    def worker(name: str, path: str, event: threading.Event) -> None:
        try:
            value = _get(path, key)
            outcome: tuple[Any, Optional[Exception]] = (value, None)
        except Exception as exc:
            outcome = (None, exc)
        with lock:
            if name not in outcomes:
                outcomes[name] = outcome
        event.set()

    for name, path in requests:
        event = threading.Event()
        events[name] = event
        threading.Thread(
            target=worker,
            args=(name, path, event),
            name=f"commandcode-{name}",
            daemon=True,
        ).start()

    for name, event in events.items():
        remaining = max(0.0, deadline - time.monotonic())
        if not event.wait(remaining):
            with lock:
                outcomes.setdefault(name, (None, _RequestTimeout()))
    return outcomes



def fetch_commandcode_quota() -> QuotaResult:
    """Registered fetcher: never raises, every failure is a typed card."""
    try:
        return _fetch()
    except Exception as exc:
        return build_unavailable(_PROVIDER, f"fetch-error:{type(exc).__name__}")



def _fetch() -> QuotaResult:
    # Multi-card: two or more saved credential rows each get their own card
    # (one saved pool row per card). A distinct CLI/core primary key is
    # included as the plain base card; when it is missing or byte-equal to a
    # saved row's credential it is deduped and the first saved row keeps the
    # base card. Zero or one rows keep the original single-card path unchanged.
    try:
        rows = _credential_rows()
    except Exception:  # noqa: BLE001 - optional enumeration must not sink a usable primary
        rows = []
    if len(rows) >= 2:
        primary = _load_api_key()
        if primary and all(primary != secret for _account_id, _label, secret in rows):
            return _fetch_cards(rows, primary=primary)
        return _fetch_cards(rows)
    key = _load_api_key()
    if not key:
        return build_unavailable(_PROVIDER, "no-credentials")
    return _fetch_card(key, time.monotonic() + _REQUEST_DEADLINE_S)


def _fetch_card(key: str, deadline: float) -> QuotaResult:
    """One credential's complete request group (raises only on bugs)."""

    # The official CLI probes whoami first, then uses its org.id for all billing
    # and usage calls.  If this auxiliary probe is unavailable, continue without
    # a guessed org scope; a definitive auth failure still stops the provider.
    whoami_path = _with_query("/alpha/whoami", limits="1")
    whoami_payload, whoami_error = _request_batch([("whoami", whoami_path)], key, deadline)["whoami"]
    if whoami_error is not None and _request_reason(whoami_error) == "auth-failed":
        return build_unavailable(_PROVIDER, "auth-failed")
    org_id = _org_id(whoami_payload)

    scoped = {"orgId": org_id}
    credits_path = _with_query("/alpha/billing/credits", **scoped)
    subscriptions_path = _with_query("/alpha/billing/subscriptions", **scoped)
    support = _request_batch(
        [("credits", credits_path), ("subscriptions", subscriptions_path)], key, deadline
    )
    credits_payload, credits_error = support["credits"]
    if credits_error is not None:
        return build_unavailable(_PROVIDER, _request_reason(credits_error))
    if not isinstance(credits_payload, dict):
        return build_unavailable(_PROVIDER, "bad-json")

    subscription_payload, _subscription_error = support["subscriptions"]
    subscription = _subscription_data(subscription_payload)

    # The CLI supplies currentPeriodStart to the summary route when the
    # subscription response contains it.  This is deliberately conditional:
    # an absent field is unknown, not a reason to invent a cycle boundary.
    since = None
    if isinstance(subscription, dict) and isinstance(subscription.get("currentPeriodStart"), str):
        since = subscription["currentPeriodStart"].strip() or None
    summary_path = _with_query("/alpha/usage/summary", orgId=org_id, since=since)
    summary_payload, _summary_error = _request_batch([("summary", summary_path)], key, deadline)[
        "summary"
    ]
    summary = summary_payload if isinstance(summary_payload, dict) else None

    windows: list[QuotaWindow] = []
    details: list[str] = []
    limits = _window_limits(credits_payload)
    if limits.get("limited") is False:
        details.append("No usage windows on this plan (pay-as-you-go)")
    else:
        for block, label in ((limits.get("fiveHour"), "5h"), (limits.get("weekly"), "Weekly")):
            window = _rolling_window(block, label)
            if window is not None:
                windows.append(window)
            if isinstance(block, dict) and block.get("exceeded") is True:
                details.append(f"{label} window exceeded - requests decline until it resets")

    credit_block = _credit_block(credits_payload)
    plan_id = _plan_id(credits_payload, subscription)
    cycle_window, cycle_details = _cycle_window(credit_block, subscription, plan_id)
    if cycle_window is not None:
        windows.append(cycle_window)
    details.extend(cycle_details)

    purchased = _number(credit_block.get("purchasedCredits"))
    if purchased is not None and purchased > 0:
        details.append(f"Top-up credits: ${purchased:.2f} (never capped)")
    free = _number(credit_block.get("freeCredits"))
    if free is not None and free > 0:
        details.append(f"Free credits: ${free:.2f}")

    if summary is not None:
        spent = _number(summary.get("totalCost"))
        count = _number(summary.get("totalCount"))
        tokens = _number(summary.get("totalTokens"))
        bits: list[str] = []
        if spent is not None:
            bits.append(f"${spent:.2f} spent")
        if count is not None:
            bits.append(f"{int(count):,} requests")
        if tokens is not None:
            bits.append(f"{tokens / 1_000_000:.1f}M tokens")
        if bits:
            details.append("Cycle so far: " + " · ".join(bits))

    if not windows and not details:
        return build_unavailable(_PROVIDER, "no-data")

    known_plan = _PLANS.get(plan_id or "")
    plan_name = known_plan[0] if known_plan is not None else None
    return QuotaResult(
        label=_PROVIDER,
        windows=windows,
        plan=plan_name,
        unavailable_reason=None,
        details=details,
    )


def _fetch_cards(
    rows: list[tuple[Optional[str], str, str]], primary: Optional[str] = None
) -> QuotaResult:
    """Fetch the base credential and every accepted saved row on one deadline.

    ``primary`` (when distinct) is the plain ``commandcode`` base card; else the
    first accepted row fills the base role and its label becomes
    ``account_label``. At most ``_MAX_POOL_ROWS`` saved rows are accepted — the
    remainder becomes one typed ``config-limit`` row, never a silent drop.
    Fetches run through a pool of at most ``_MAX_CONCURRENT_FETCHES`` workers,
    so a huge saved pool cannot fan out unbounded requests. One row's failure
    is that row's typed unavailable card and never masks a sibling.
    """
    deadline = time.monotonic() + _REQUEST_DEADLINE_S
    accepted = rows[:_MAX_POOL_ROWS]
    keys = [row[2] for row in accepted]
    if primary is not None:
        keys.insert(0, primary)
    results: list[Optional[QuotaResult]] = [None] * len(keys)
    events = [threading.Event() for _ in keys]
    lock = threading.Lock()
    pending: queue.Queue[int] = queue.Queue()
    for index in range(len(keys)):
        pending.put(index)

    def worker() -> None:
        while True:
            try:
                index = pending.get_nowait()
            except queue.Empty:
                return
            try:
                card = _fetch_card(keys[index], deadline)
            except Exception as exc:  # one row must not sink its siblings
                card = build_unavailable(_PROVIDER, f"fetch-error:{type(exc).__name__}")
            with lock:
                if results[index] is None:
                    results[index] = card
            events[index].set()

    for slot in range(min(len(keys), _MAX_CONCURRENT_FETCHES)):
        threading.Thread(
            target=worker, name=f"commandcode-row{slot}", daemon=True
        ).start()
    for event in events:
        event.wait(max(0.0, deadline - time.monotonic()))

    cards: list[QuotaResult] = []
    for index, event in enumerate(events):
        card = results[index] if event.is_set() else None
        cards.append(card if card is not None else build_unavailable(_PROVIDER, "timeout"))

    base = cards[0]
    if primary is not None:
        account_rows = accepted
    else:
        base.account_label = accepted[0][1] or None
        account_rows = accepted[1:]
    base.accounts.extend(
        QuotaAccount(
            id=account_id,
            label=label,
            windows=list(row_card.windows or []),
            plan=row_card.plan,
            unavailable_reason=row_card.unavailable_reason,
            details=list(row_card.details or []),
        )
        for (account_id, label, _key), row_card in zip(account_rows, cards[1:])
    )
    if len(rows) > len(accepted):
        base.accounts.append(
            QuotaAccount(
                id=f"{GENERATED_ACCOUNT_ID_PREFIX}limit",
                label="Additional credentials",
                unavailable_reason=_POOL_LIMIT_REASON,
                details=[_POOL_LIMIT_DETAIL],
            )
        )
    return base


_register(_PROVIDER)(fetch_commandcode_quota)
