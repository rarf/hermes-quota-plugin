"""Pluggable per-provider quota fetchers (quota plugin, standalone copy).

Each provider has its own fetch module registered in ``PROVIDER_FETCHERS`` (see
``registry.py``).  A fetcher takes no arguments and returns a ``QuotaResult``
(or ``None`` when the provider has no usable data).  The cache builder
(``quota_cache``) iterates the registry so adding a new provider is just: write a
fetcher + register it — no changes to the cache orchestration.  Import fetcher
modules for their registration side effects.

A fetcher MUST be fail-open: on any error it returns a ``QuotaResult`` with
``unavailable_reason`` set (never raises), so one broken provider can't abort
the whole cache refresh.
"""

from __future__ import annotations

from .base import QuotaResult, QuotaWindow, build_unavailable
from .registry import PROVIDER_FETCHERS, register, get_fetcher

from . import grok  # noqa: F401
from . import builtin  # noqa: F401
from . import kimi  # noqa: F401
from . import gemini  # noqa: F401
from . import opencode_go  # noqa: F401
from . import copilot  # noqa: F401

__all__ = [
    "PROVIDER_FETCHERS",
    "register",
    "get_fetcher",
    "QuotaResult",
    "QuotaWindow",
    "build_unavailable",
]
