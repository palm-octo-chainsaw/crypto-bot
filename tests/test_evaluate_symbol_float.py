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


@patch("portfolio.Balance")
def test_evaluate_symbol_leaves_dust_out_of_the_summary(_balance_cls):
    """Under 0.05% both held and targeted is noise; a 0% holding still to be bought is not."""
    from portfolio import Portfolio

    p = Portfolio()
    p.targets = {"BTC": 0.0, "SOL": 60.0, "NEAR": 40.0, "PAXG": 0.04}
    p.summary = MagicMock()

    p.evaluate_symbol(values={"BTC": 0.04, "SOL": 99.92, "NEAR": 0.0, "PAXG": 0.04},
                      total_value=100.0)

    summaries = " ".join(call.args[0] for call in p.summary.add_summary.call_args_list)
    assert "$SOL" in summaries
    assert "$NEAR" in summaries
    assert "$BTC" not in summaries
    assert "$PAXG" not in summaries
