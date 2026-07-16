"""Unit tests for token->USD pricing, including the cache-write TTL split."""

from minds_pulse.pricing import cache_write_1h_cost_per_token
from minds_pulse.pricing import compute_cost
from minds_pulse.pricing import resolve_pricing_key

_PER_M = 1_000_000


def test_resolve_pricing_key_infers_provider_prefix() -> None:
    assert resolve_pricing_key("claude-opus-4-8") == "anthropic/claude-opus-4-8"
    assert resolve_pricing_key("gpt-5.2-codex") == "openai/gpt-5.2-codex"
    assert resolve_pricing_key("anthropic/claude-opus-4-8") == "anthropic/claude-opus-4-8"


def test_unknown_model_is_unpriced_not_free() -> None:
    assert resolve_pricing_key("some-unreleased-model") is None
    assert compute_cost("some-unreleased-model", 1000, 1000, 0, 0) is None


def test_opus_per_bucket_rates() -> None:
    # One million tokens in a single bucket resolves to that bucket's per-Mtok rate.
    assert compute_cost("claude-opus-4-8", _PER_M, 0, 0, 0) == 5.0
    assert compute_cost("claude-opus-4-8", 0, _PER_M, 0, 0) == 25.0
    assert compute_cost("claude-opus-4-8", 0, 0, _PER_M, 0) == 0.5  # cache read


def test_cache_write_5m_vs_1h_split() -> None:
    # With no 1h portion, the whole cache-write is at the 5-minute rate (1.25x input).
    assert compute_cost("claude-opus-4-8", 0, 0, 0, _PER_M) == 6.25
    # All 1h -> 2x input rate.
    assert compute_cost("claude-opus-4-8", 0, 0, 0, _PER_M, _PER_M) == 10.0
    # A mix is priced proportionally: 600k @ 5m + 400k @ 1h.
    mixed = compute_cost("claude-opus-4-8", 0, 0, 0, _PER_M, 400_000)
    assert mixed == 600_000 * 6.25e-6 + 400_000 * 10e-6


def test_1h_portion_is_clamped_to_total() -> None:
    # A 1h count larger than the total cache-write must not over-charge.
    clamped = compute_cost("claude-opus-4-8", 0, 0, 0, 1000, 5000)
    all_1h = compute_cost("claude-opus-4-8", 0, 0, 0, 1000, 1000)
    assert clamped == all_1h


def test_cache_write_1h_rate_is_double_input() -> None:
    assert cache_write_1h_cost_per_token("claude-opus-4-8") == 5e-6 * 2
    assert cache_write_1h_cost_per_token("unknown-model") is None
