"""Shared types + helpers for quota fetchers (quota plugin, standalone)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


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
class QuotaResult:
    """Normalized quota for one provider, ready to cache."""

    label: str
    windows: list[QuotaWindow] = field(default_factory=list)
    plan: Optional[str] = None
    unavailable_reason: Optional[str] = None

    def has_data(self) -> bool:
        return bool(self.windows) and self.unavailable_reason is None


def build_unavailable(label: str, reason: str) -> QuotaResult:
    return QuotaResult(label=label, windows=[], plan=None, unavailable_reason=reason)
