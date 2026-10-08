"""Shared types + helpers for quota fetchers (quota plugin, standalone)."""

from __future__ import annotations

import urllib.request
import time
from dataclasses import dataclass, field
from typing import Optional


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a bearer credential is never replayed to another host.

    ``urllib``'s default ``HTTPRedirectHandler`` re-sends the ``Authorization``
    and ``Cookie`` headers to the redirect target, so a 302 from a provider
    endpoint would hand the credential to whichever host named in ``Location``.
    Every authenticated provider request must go through :func:`urlopen_no_redirect`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


#: Opener that never follows a redirect. Use instead of ``urllib.request.urlopen``
#: for every request that carries a credential.
urlopen_no_redirect = urllib.request.build_opener(NoRedirectHandler()).open


@dataclass
class QuotaWindow:
    """One billing window (session / weekly / monthly) for a provider."""

    label: str
    used_percent: Optional[float] = None  # provider-reported *used* fraction 0..100
    reset_at: Optional[str] = None  # ISO-8601 UTC timestamp

    def remaining_pct(self) -> Optional[int]:
        if self.used_percent is None:
            return None
        try:
            rem = 100.0 - float(self.used_percent)
        except (TypeError, ValueError):
            return None
        rem = max(0.0, min(100.0, rem))
        return int(round(rem))


@dataclass
class AccountBalance:
    """Account money, not a key cap or a percentage denominator.

    Decimal strings preserve the precision returned by the provider.
    """

    currency: str
    total_balance: str
    granted_balance: Optional[str] = None
    topped_up_balance: Optional[str] = None


@dataclass
class QuotaAccount:
    """One additional account for a provider (e.g. a second Claude login).

    Rendered as its own row — keyed ``<provider_id>:<id>`` by the cache — so
    one account's failure never masks another's windows. ``id`` is a stable,
    user-chosen identifier; ``label`` is its display name. Everything else
    mirrors a :class:`QuotaResult` so the existing render paths need no
    account-specific code.
    """

    id: str
    label: str = ""
    windows: list[QuotaWindow] = field(default_factory=list)
    plan: Optional[str] = None
    unavailable_reason: Optional[str] = None
    details: list[str] = field(default_factory=list)


@dataclass
class QuotaResult:
    """Normalized quota for one provider, ready to cache."""

    label: str
    windows: list[QuotaWindow] = field(default_factory=list)
    plan: Optional[str] = None
    unavailable_reason: Optional[str] = None
    # Extra provider facts shown under the windows in the widget (e.g. Codex
    # "Credits balance: $12.50", "You have 2 resets banked").
    details: list[str] = field(default_factory=list)
    account_balances: list[AccountBalance] = field(default_factory=list)
    api_calls_available: Optional[bool] = None
    # Additional accounts for this provider. Generic: any fetcher may attach
    # them and the cache expands each into a sibling provider row, so every
    # consumer sees a plain list of provider records.
    accounts: list[QuotaAccount] = field(default_factory=list)

    def has_data(self) -> bool:
        return (bool(self.windows) or bool(self.details) or bool(self.account_balances)) and self.unavailable_reason is None


def build_unavailable(label: str, reason: str) -> QuotaResult:
    return QuotaResult(label=label, windows=[], plan=None, unavailable_reason=reason)


def opt_in_flag(value: object) -> bool:
    """Strictly parse a boolean-ish opt-in setting.

    ``bool("false")`` is True, so a YAML/JSON value written as the *string*
    ``"false"`` — or ``"no"``, ``"0"``, ``"off"`` — read as an opt-IN. For
    ``grokEnabled`` that means reading and shipping browser session cookies
    after the user explicitly opted out. Only a real bool, or one of the
    recognised affirmative strings, enables a sensitive source.

    Mirrors the allow/deny sets the env-var path in ``grok._grok_enabled``
    and ``minimax._video_enabled`` already use.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False
class Deadline:
    """Monotonic budget for serial requests and retry/backoff scheduling.

    ``quota_cache.REFRESH_BUDGET_S`` (20s) limits how long a cache refresh
    waits for the provider workers; unfinished providers are recorded as
    ``timeout``. The cache uses daemon workers and cannot cancel a request that
    is still blocked in I/O.

    ``slice()`` caps the timeout passed to the next request, but this is not a
    hard wall-clock deadline for that request. In particular, ``urllib``'s
    socket timeout limits an individual blocking socket operation/inactivity;
    a peer that keeps trickling bytes can keep ``response.read()`` alive beyond
    the remaining budget. Do not treat this class as request cancellation.
    It prevents starting work with no budget left and reduces serial request
    and retry timeouts; it does not bound an active trickling response.
    """

    def __init__(self, budget: float, clock=None) -> None:
        # Resolved at call time, not bound as a default argument, so a test can
        # patch time.monotonic after import and still drive the clock.
        self._clock = clock if clock is not None else time.monotonic
        self._expires = self._clock() + max(0.0, float(budget))

    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self._expires - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def slice(self, cap: float) -> float:
        """The timeout to hand the next request: the smaller of ``cap`` and
        what is left. Returns 0.0 once spent, which fails fast rather than
        blocking for the full per-request timeout."""
        return min(float(cap), self.remaining())
