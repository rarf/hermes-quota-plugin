"""Shared types + helpers for quota fetchers (quota plugin, standalone)."""

from __future__ import annotations

import urllib.request
import time
from dataclasses import dataclass, field
from typing import Optional, Union


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


# -- Usage provenance ---------------------------------------------------------
# Where a set of usage figures came from, and what they actually measure. This
# is deliberately a small closed vocabulary: a consumer (CLI, widget) can rely
# on the exact strings, and an adapter cannot smuggle a claim (a plan, a budget
# identity) in free text.
#
# Plan provenance distinguishes a plan the *provider* reported from a plan the
# user typed into a display label. A user label is never a verified plan, and a
# plan is never inferred from an email address (an ``.edu`` domain says nothing
# about a subscription tier).
PLAN_SOURCE_PROVIDER_RESPONSE = "provider-response"
PLAN_SOURCE_LOCAL_CREDENTIAL = "local-credential"
PLAN_SOURCE_USER_LABEL = "user-label"
PLAN_SOURCE_UNKNOWN = "unknown"

# How usage is paid for. A subscription allowance (a per-window percentage) is
# not the same thing as a pay-as-you-go balance or spend, and neither is a
# total across accounts.
BILLING_SUBSCRIPTION = "subscription"
BILLING_PAY_AS_YOU_GO = "pay-as-you-go"
BILLING_MIXED = "mixed"
BILLING_UNKNOWN = "unknown"

# What the reported figures measure.
METER_SUBSCRIPTION_ALLOWANCE = "subscription allowance"
METER_CREDITS_BALANCE = "credits balance"
METER_CHAT_QUERIES = "chat queries"
METER_UNKNOWN = "unknown"

#: Plan sources that count as *verified*: the value is taken from provider
#: response metadata or a local credential field. A display label never is.
_VERIFIED_PLAN_SOURCES = frozenset(
    {PLAN_SOURCE_PROVIDER_RESPONSE, PLAN_SOURCE_LOCAL_CREDENTIAL}
)

# The closed vocabularies. A consumer (CLI, widget, cache) can rely on these
# exact strings and nothing else: anything outside them is *unknown*, so a
# stale or hand-edited cache cannot smuggle a claim (``verified-by-email``,
# ``free-tier``, ``tokens``) in as status.
_PLAN_SOURCES = frozenset({
    PLAN_SOURCE_PROVIDER_RESPONSE, PLAN_SOURCE_LOCAL_CREDENTIAL,
    PLAN_SOURCE_USER_LABEL, PLAN_SOURCE_UNKNOWN,
})
_BILLING_MODES = frozenset({
    BILLING_SUBSCRIPTION, BILLING_PAY_AS_YOU_GO, BILLING_MIXED, BILLING_UNKNOWN,
})
_METERS = frozenset({
    METER_SUBSCRIPTION_ALLOWANCE, METER_CREDITS_BALANCE,
    METER_CHAT_QUERIES, METER_UNKNOWN,
})

_PROVENANCE_TEXT_MAX = 200


def _normalize_plan_source(value: object) -> str:
    return value if isinstance(value, str) and value in _PLAN_SOURCES else PLAN_SOURCE_UNKNOWN


def _normalize_billing_mode(value: object) -> str:
    return value if isinstance(value, str) and value in _BILLING_MODES else BILLING_UNKNOWN


def _normalize_meter(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value if value in _METERS else METER_UNKNOWN


def _provenance_text(value: Optional[object]) -> Optional[str]:
    """Normalize one provenance string: strip control chars, bound length.

    Kept deliberately dumb: it never invents a value, and it never rewrites a
    value into a claim. ``None`` and blanks stay ``None``.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable() or ch == " ").strip()
    if not cleaned:
        return None
    return cleaned[:_PROVENANCE_TEXT_MAX]


def _provenance_identity(value: Optional[object]) -> Optional[str]:
    """A provenance *identity* string: like :func:`_provenance_text`, but any
    value containing ``@`` is dropped.

    The provenance contract promises that an email never reaches the cache or
    the UI, and that the display label it carries is not a verified identity.
    Enforcing this at serialization (not only in the provider-side constructor)
    keeps the invariant true no matter how a :class:`Provenance` was built.
    """
    text = _provenance_text(value)
    if text is None or "@" in text:
        return None
    return text


@dataclass
class Provenance:
    """Normalized account / plan / billing / meter provenance for one record.

    ``account`` is the row's *display* account label where one exists; it is
    never a verified identity and never an email. ``plan_source`` says where a
    plan name came from (see the ``PLAN_SOURCE_*`` constants); ``billing_mode``
    distinguishes a subscription allowance from pay-as-you-go money;
    ``meter`` names what the numbers measure; ``source``/``scope`` name the
    endpoint and the budget the numbers cover; ``note`` carries an explicit
    honesty caveat (for example, that a browser-session allowance does not
    share a budget with a different provider identity).
    """

    source: Optional[str] = None
    scope: Optional[str] = None
    account: Optional[str] = None
    plan_source: str = PLAN_SOURCE_UNKNOWN
    billing_mode: str = BILLING_UNKNOWN
    meter: Optional[str] = None
    note: Optional[str] = None

    def plan_is_verified(self) -> bool:
        """Whether the plan is provider/credential metadata, not a user label."""
        return self.plan_source in _VERIFIED_PLAN_SOURCES


def provenance_to_dict(prov: Optional[Provenance]) -> dict[str, Optional[str]]:
    """Serialize a :class:`Provenance` to a cache-safe plain dict.

    Every value passes through the normalization helpers, so a control
    character, an unbounded string, an email-shaped identity, or an
    out-of-vocabulary plan/billing/meter value cannot reach the cache or the UI.
    """
    if not isinstance(prov, Provenance):
        prov = Provenance()
    return {
        "source": _provenance_text(prov.source),
        "scope": _provenance_text(prov.scope),
        "account": _provenance_identity(prov.account),
        "plan_source": _normalize_plan_source(prov.plan_source),
        "billing_mode": _normalize_billing_mode(prov.billing_mode),
        "meter": _normalize_meter(prov.meter),
        "note": _provenance_text(prov.note),
    }


def provenance_from_dict(data: Optional[object]) -> Provenance:
    """Rebuild a :class:`Provenance` from a cache dict; unknown shapes are blank.

    Plan source / billing mode / meter are enforced against the closed
    vocabulary (anything outside is ``unknown``), and an email-shaped account
    is dropped, so a stale or hand-edited cache is normalized on read.
    """
    if not isinstance(data, dict):
        return Provenance()
    return Provenance(
        source=_provenance_text(data.get("source")),
        scope=_provenance_text(data.get("scope")),
        account=_provenance_identity(data.get("account")),
        plan_source=_normalize_plan_source(data.get("plan_source")),
        billing_mode=_normalize_billing_mode(data.get("billing_mode")),
        meter=_normalize_meter(data.get("meter")),
        note=_provenance_text(data.get("note")),
    )


def normalize_provenance_dict(data: Optional[object]) -> dict[str, Optional[str]]:
    """Normalize an arbitrary provenance dict for read: closed vocab + no email.

    This is the read-side companion to :func:`provenance_to_dict`; a consumer
    (cache loader, CLI, widget feed) applies it so a stale or hand-edited cache
    is normalized before anything is shown. Unknown shapes become a blank
    ``unknown`` provenance rather than a claim.
    """
    return provenance_to_dict(provenance_from_dict(data))


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
    provenance: Provenance = field(default_factory=Provenance)

    def has_data(self) -> bool:
        return (bool(self.windows) or bool(self.details)) and self.unavailable_reason is None


#: Prefix the cache gives an account row that has no usable user ``id`` (a
#: malformed entry, or a fetcher that omitted one). It is only a hint: the
#: cache still disambiguates any real collision with a deterministic suffix, so
#: a generated row and a configured row can never silently hide each other.
GENERATED_ACCOUNT_ID_PREFIX = "__invalid-"


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
    # Additional provider accounts: Claude uses QuotaAccount rows with stable IDs;
    # Codex uses nested QuotaResult records for saved-account display.
    accounts: list[Union[QuotaAccount, QuotaResult]] = field(default_factory=list)
    # Explicit label for legacy single-account provider records (Codex).
    account_label: Optional[str] = None
    # Normalized account/plan/billing/meter provenance (see Provenance).
    provenance: Provenance = field(default_factory=Provenance)

    def has_data(self) -> bool:
        return (
            bool(self.windows)
            or bool(self.details)
            or bool(self.account_balances)
            or any(a.has_data() for a in self.accounts)
        ) and self.unavailable_reason is None


def build_unavailable(
    label: str,
    reason: str,
    details: Optional[list[str]] = None,
) -> QuotaResult:
    """Unavailable record; optional details carry per-credential diagnostics."""
    return QuotaResult(
        label=label,
        windows=[],
        plan=None,
        unavailable_reason=reason,
        details=details or [],
    )


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
