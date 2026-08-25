"""JSON round-trip and message formatting in utils/helpers."""
import json

from utils.helpers import format_message, load_json, write_json


def test_write_json_then_load_json_round_trip(tmp_path):
    path = tmp_path / "targets.json"

    write_json(path, {"BTC": 60.0, "USDC": 40.0})

    assert load_json(path) == {"BTC": 60.0, "USDC": 40.0}
    assert json.loads(path.read_text()) == {"BTC": 60.0, "USDC": 40.0}


def test_write_json_overwrites_existing_content(tmp_path):
    path = tmp_path / "targets.json"
    write_json(path, {"BTC": 100.0})

    write_json(path, {"ETH": 100.0})

    assert load_json(path) == {"ETH": 100.0}


def test_format_message_wraps_in_a_code_block():
    assert format_message("hello") == "```\nhello```"
