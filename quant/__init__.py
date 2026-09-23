"""Paper quant overlay scaffold (PAPER-only v1).

This package is intentionally isolated from live trading paths
(``portfolio.py``, ``data/trading.py``). Automation must never place
live orders through this overlay.
"""

__all__ = ["config"]
