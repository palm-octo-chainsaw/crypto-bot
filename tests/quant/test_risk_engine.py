"""Tests for RiskEngine degraded-venue refuse + kill-switch."""

from quant.risk.engine import RiskEngine, refuse_if_degraded


def test_healthy_venue_allows_order():
    engine = RiskEngine()
    decision = engine.allow_order(venue="binance")
    assert decision.allowed is True


def test_degraded_venues_refuse():
    engine = RiskEngine()
    engine.mark_degraded("binance")
    decision = engine.allow_order(venue="binance")
    assert decision.allowed is False
    assert "degraded" in decision.reason


def test_degraded_required_venues_refuse():
    engine = RiskEngine()
    engine.mark_degraded("kraken")
    decision = engine.allow_order(venue="paper", required_venues=["kraken"])
    assert decision.allowed is False
    assert "kraken" in decision.reason


def test_refuse_if_degraded_helper():
    engine = RiskEngine()
    engine.mark_degraded("hyperliquid")
    decision = refuse_if_degraded(engine, "hyperliquid")
    assert decision.allowed is False


def test_clear_degraded_allows_again():
    engine = RiskEngine()
    engine.mark_degraded("binance")
    engine.clear_degraded("binance")
    assert engine.allow_order(venue="binance").allowed is True


def test_kill_switch_refuses_all():
    engine = RiskEngine()
    engine.trip_kill_switch("daily loss")
    decision = engine.allow_order(venue="paper")
    assert decision.allowed is False
    assert "daily loss" in decision.reason or "kill" in decision.reason.lower()
