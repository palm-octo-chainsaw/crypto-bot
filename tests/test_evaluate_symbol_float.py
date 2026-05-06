"""Target percentages render as floats with 2 decimals in /check output."""
from unittest.mock import MagicMock, patch


@patch("portfolio.Balance")
def test_evaluate_symbol_renders_fractional_target(_balance_cls):
    from portfolio import Portfolio

    p = Portfolio()
    p.targets = {"BTC": 42.5, "ETH": 57.5}
    p.summary = MagicMock()

    values = {"BTC": 50.0, "ETH": 50.0}
    p.evaluate_symbol(values, total_value=100.0)

    summaries = [call.args[0] for call in p.summary.add_summary.call_args_list]
    btc_line = next(s for s in summaries if "$BTC" in s)
    assert "Target: 42.50%" in btc_line


@patch("portfolio.Balance")
def test_evaluate_symbol_renders_rebalance_with_float_target(_balance_cls):
    from portfolio import Portfolio

    p = Portfolio()
    p.targets = {"BTC": 42.5}
    p.summary = MagicMock()

    p.evaluate_symbol(values={"BTC": 100.0}, total_value=100.0)

    rebalances = [call.args[0] for call in p.summary.add_rebalance.call_args_list]
    assert any("Target: 42.50%" in r for r in rebalances)
