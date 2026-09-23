"""Execution router: PAPER → PaperBroker; LIVE from automation hard-fails.

LIVE trading through this overlay is LOCKED. Automation must never call
exchange create_market_order (or any live order API) from here.
"""

from __future__ import annotations

from typing import Optional

from quant.config import ExecutionMode, QuantConfig, load_config
from quant.execution.paper import PaperBroker


class LiveExecutionLockedError(RuntimeError):
    """Raised when automation attempts LIVE execution via the quant overlay."""


class ExecutionRouter:
    """Route orders by ``EXECUTION_MODE``.

    - PAPER: returns / uses ``PaperBroker`` (in-memory, no network).
    - LIVE: raises ``LiveExecutionLockedError`` (RuntimeError) /
      ``NotImplementedError``. Never places live orders.
    """

    def __init__(
        self,
        config: Optional[QuantConfig] = None,
        paper_broker: Optional[PaperBroker] = None,
    ) -> None:
        self.config = config or load_config()
        starting = self.config.paper_equity
        self._paper = paper_broker or PaperBroker(
            cash=starting,
            starting_equity=starting,
        )

    @property
    def mode(self) -> ExecutionMode:
        return self.config.execution_mode

    def _refuse_live(self) -> None:
        """Hard-fail LIVE path — never call create_market_order."""
        raise LiveExecutionLockedError(
            "LIVE execution is locked for the quant overlay; "
            "automation must not place live orders. Set EXECUTION_MODE=PAPER."
        )

    def broker(self) -> PaperBroker:
        """Return the paper broker when mode is PAPER; else raise."""
        if self.mode is ExecutionMode.PAPER:
            return self._paper
        self._refuse_live()
        raise AssertionError("unreachable")  # pragma: no cover

    def submit(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
    ) -> None:
        """Submit an order through the active backend.

        PAPER: no-op skeleton (fills land in PR2).
        LIVE: always raises NotImplementedError / LiveExecutionLockedError;
        never calls create_market_order.
        """
        _ = (symbol, side, quantity, price)
        if self.mode is ExecutionMode.PAPER:
            _ = self.broker()
            return None
        # Intentionally unimplemented — do not add live order calls here.
        raise NotImplementedError(
            "quant LIVE backend is intentionally unimplemented; "
            "automation must never place live orders via ExecutionRouter"
        )
