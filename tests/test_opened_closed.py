"""What a trade row records as opened and closed (data.database.opened_closed)."""
import pytest

from data.database import opened_closed


@pytest.mark.parametrize("symbol, side, direction, expected", [
    ("ETH/USDC", "buy", "long", ("ETH", None)),
    ("ETH/USDC", "sell", "long", (None, "ETH")),
    ("ETH/BTC", "buy", "long", ("ETH", "BTC")),
    ("ETH/BTC", "sell", "long", ("BTC", "ETH")),
    # An inverted pair: buying USDC with XYZ closes XYZ.
    ("USDC/XYZ", "buy", "long", (None, "XYZ")),
    ("SOL/USDC:USDC", "buy", "long", ("SOL", None)),
    ("SOL/USDC:USDC", "sell", "long", (None, "SOL")),
    ("SOL/USDC:USDC", "sell", "short", ("SOL", None)),
    ("SOL/USDC:USDC", "buy", "short", (None, "SOL")),
    ("KSHIB/USDC:USDC", "sell", "short", ("KSHIB", None)),
])
def test_opened_closed(symbol, side, direction, expected):
    assert opened_closed(symbol, side, direction) == expected


@pytest.mark.parametrize("symbol, side", [("", "buy"), ("ETH/USDC", ""), ("PAXG", "sell")])
def test_opened_closed_is_empty_without_a_pair_and_a_side(symbol, side):
    assert opened_closed(symbol, side, "long") == (None, None)
