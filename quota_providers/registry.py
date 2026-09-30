"""Provider-quota fetcher registry (quota plugin, standalone)."""

from __future__ import annotations

import functools
from typing import Callable, Optional

from .base import QuotaResult, build_unavailable

# Provider id -> fetcher callable.  Order here is display priority.
PROVIDER_FETCHERS: dict[str, Callable[[], object]] = {}


def register(provider_id: str):
    """Register a fetcher, enforcing the fail-open contract at the seam.

    ``add-provider.md``: "Fail-open, never raise. Wrap the whole body; every
    failure path returns ``build_unavailable(...)``". Nine of fifteen fetchers
    did that; the other six could raise out of the registered callable -- a
    DNS failure in ``gemini`` escaped as ``OSError``, a non-numeric
    ``expiry_date`` as ``ValueError``. ``quota_cache._fetch_one`` caught them,
    so the sweep survived, but the provider recorded the generic
    ``fetch-error`` rather than a truthful reason, which the same guide
    forbids.

    Guarding here rather than in each body keeps the existing error handling
    readable and makes the contract hold for a provider added later. The
    reason code stays ``fetch-error`` so no currently-correct mapping changes.
    """

    def _wrap(fn: Callable[[], object]):
        @functools.wraps(fn)
        def _fail_open() -> QuotaResult:
            try:
                return fn()  # type: ignore[return-value]
            except Exception:  # noqa: BLE001 - fail-open by contract
                return build_unavailable(provider_id, "fetch-error")

        PROVIDER_FETCHERS[provider_id] = _fail_open
        return fn

    return _wrap


def get_fetcher(provider_id: str) -> Optional[Callable[[], object]]:
    return PROVIDER_FETCHERS.get(provider_id)
