"""Risk engine interface + kill-switch / degraded-venue refuse stub.

Documented caps (enforced in later PRs; kill-switch / degraded only in v1)
--------------------------------------------------------------------------
- max_position_pct: per-symbol notional / equity
- max_gross_exposure_pct: sum of abs notionals / equity
- max_daily_loss_pct: trip kill-switch when paper PnL breaches
- max_leverage: 1.0 (spot) for v1

When any required venue is marked degraded, ``allow_order`` refuses.
Automation must not size or route new risk while degraded or kill-switched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Set

from quant.config import QuantConfig, RiskCaps, load_config


@dataclass
class RiskDecision:
    """Result of a pre-trade risk check."""

    allowed: bool
    reason: str = ""


@dataclass
class RiskEngine:
    """Minimal risk gate for PR1.

    Caps from ``RiskCaps`` are stored for documentation and future use;
    v1 only enforces ``kill_switch`` and degraded-venue refusal.
    """

    caps: RiskCaps = field(default_factory=RiskCaps)
    kill_switch: bool = False
    degraded_venues: Set[str] = field(default_factory=set)
    _kill_reason: str = field(default="", repr=False)

    @classmethod
    def from_config(cls, config: Optional[QuantConfig] = None) -> "RiskEngine":
        cfg = config or load_config()
        return cls(caps=cfg.risk_caps)

    def mark_degraded(self, venue: str) -> None:
        """Flag a venue as degraded (failed/stale reads)."""
        self.degraded_venues.add(venue)

    def clear_degraded(self, venue: str) -> None:
        """Clear degraded flag for a venue."""
        self.degraded_venues.discard(venue)

    def trip_kill_switch(self, reason: str = "") -> None:
        """Engage kill-switch — all new orders refused."""
        self.kill_switch = True
        self._kill_reason = reason

    def reset_kill_switch(self) -> None:
        """Clear kill-switch (manual / ops only)."""
        self.kill_switch = False
        self._kill_reason = ""

    def allow_order(
        self,
        *,
        venue: str = "paper",
        symbol: str = "",
        notional: float = 0.0,
        required_venues: Optional[Iterable[str]] = None,
    ) -> RiskDecision:
        """Refuse if kill-switched or a relevant venue is degraded.

        ``notional`` / ``symbol`` are accepted for forward-compat with
        cap checks; unused in PR1 beyond the stub signature.
        """
        _ = (symbol, notional)
        if self.kill_switch:
            reason = self._kill_reason or "kill_switch engaged"
            return RiskDecision(allowed=False, reason=reason)

        check: Set[str] = {venue}
        if required_venues is not None:
            check |= set(required_venues)
        degraded_hit = self.degraded_venues & check
        if degraded_hit:
            return RiskDecision(
                allowed=False,
                reason=f"venue degraded: {', '.join(sorted(degraded_hit))}",
            )
        return RiskDecision(allowed=True, reason="ok")


def refuse_if_degraded(engine: RiskEngine, venue: str) -> RiskDecision:
    """Return a refusing decision when ``venue`` is marked degraded."""
    return engine.allow_order(venue=venue)
