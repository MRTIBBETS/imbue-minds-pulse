"""Per-token USD pricing for converting token counts into dollars.

The rates below are **mirrored verbatim** from the vendored
``vendor/mngr/libs/mngr_usage/imbue/mngr_usage/pricing.py`` table (which itself
mirrors ``apps/modal_litellm/app.py`` and litellm's price map, kept in sync by a
drift test). We copy rather than import because ``imbue.mngr_usage`` is not a
member of this repo's root uv workspace and its ``imbue.*`` namespace pulls a
dependency chain that is not installed in the root venv.

Provenance note for maintainers: if the vendored table changes, re-mirror it
here. ``pricing_test.py`` guards the arithmetic; a future reconciliation against
``mngr usage`` (once that plugin is enabled in the workspace) would catch drift.

The four billing buckets are non-overlapping -- ``input`` is the non-cached input
count, and ``cache_read`` / ``cache_creation`` are separate -- so cost is exactly
``input*p_in + cache_read*p_cr + cache_creation*p_cw + output*p_out`` with no
double counting. An unknown model returns ``None`` (visibly unpriced), never
``0`` (silently free).
"""

from typing import NamedTuple


class PerTokenPrices(NamedTuple):
    """USD price per single token for each billing bucket of one model."""

    input_cost_per_token: float
    output_cost_per_token: float
    cache_read_input_token_cost: float
    cache_creation_input_token_cost: float


_FABLE_PRICES = PerTokenPrices(0.00001, 0.00005, 0.000001, 0.0000125)
_OPUS_PRICES = PerTokenPrices(0.000005, 0.000025, 0.0000005, 0.00000625)
# Opus 4.1 and the original Opus 4 predate the Opus price drop and cost 3x.
_OPUS_LEGACY_PRICES = PerTokenPrices(0.000015, 0.000075, 0.0000015, 0.00001875)
_SONNET_PRICES = PerTokenPrices(0.000003, 0.000015, 0.0000003, 0.00000375)
_HAIKU_PRICES = PerTokenPrices(0.000001, 0.000005, 0.0000001, 0.00000125)

# OpenAI / Codex models have no cache-write surcharge, so cache_creation is 0.
_GPT5_PRICES = PerTokenPrices(0.00000125, 0.00001, 0.000000125, 0.0)
_GPT52_PRICES = PerTokenPrices(0.00000175, 0.000014, 0.000000175, 0.0)
_GPT5_MINI_PRICES = PerTokenPrices(0.00000025, 0.000002, 0.000000025, 0.0)
_CODEX_MINI_PRICES = PerTokenPrices(0.0000015, 0.000006, 0.000000375, 0.0)
_O3_PRICES = PerTokenPrices(0.000002, 0.000008, 0.0000005, 0.0)
_O4_MINI_PRICES = PerTokenPrices(0.0000011, 0.0000044, 0.000000275, 0.0)

# Canonical key is "<provider>/<model>".
MODEL_PRICING: dict[str, PerTokenPrices] = {
    "anthropic/claude-fable-5": _FABLE_PRICES,
    "anthropic/claude-opus-4-8": _OPUS_PRICES,
    "anthropic/claude-opus-4-7": _OPUS_PRICES,
    "anthropic/claude-opus-4-6": _OPUS_PRICES,
    "anthropic/claude-opus-4-5": _OPUS_PRICES,
    "anthropic/claude-opus-4-1": _OPUS_LEGACY_PRICES,
    "anthropic/claude-opus-4-20250514": _OPUS_LEGACY_PRICES,
    "anthropic/claude-sonnet-4-6": _SONNET_PRICES,
    "anthropic/claude-sonnet-4-5": _SONNET_PRICES,
    "anthropic/claude-sonnet-4-20250514": _SONNET_PRICES,
    "anthropic/claude-haiku-4-5": _HAIKU_PRICES,
    "anthropic/claude-haiku-4-5-20251001": _HAIKU_PRICES,
    "openai/gpt-5": _GPT5_PRICES,
    "openai/gpt-5.1": _GPT5_PRICES,
    "openai/gpt-5-codex": _GPT5_PRICES,
    "openai/gpt-5.1-codex": _GPT5_PRICES,
    "openai/gpt-5.1-codex-max": _GPT5_PRICES,
    "openai/gpt-5.2": _GPT52_PRICES,
    "openai/gpt-5.2-codex": _GPT52_PRICES,
    "openai/gpt-5.3-codex": _GPT52_PRICES,
    "openai/gpt-5-mini": _GPT5_MINI_PRICES,
    "openai/gpt-5.1-codex-mini": _GPT5_MINI_PRICES,
    "openai/codex-mini-latest": _CODEX_MINI_PRICES,
    "openai/o3": _O3_PRICES,
    "openai/o4-mini": _O4_MINI_PRICES,
}


def resolve_pricing_key(model: str) -> str | None:
    """Map a raw transcript model id to a MODEL_PRICING key, or None if unknown.

    Transcripts record bare ids like ``claude-opus-4-8``; the table is keyed by
    ``<provider>/<model>``. Try the id verbatim, then with the provider prefix
    inferred from the id's shape.
    """
    if model in MODEL_PRICING:
        return model
    for prefix in ("anthropic/", "openai/"):
        if prefix + model in MODEL_PRICING:
            return prefix + model
    if model.startswith("claude") and "anthropic/" + model in MODEL_PRICING:
        return "anthropic/" + model
    return None


def cache_write_1h_cost_per_token(model: str) -> float | None:
    """USD per token for a 1-hour-TTL cache write, or None if unpriced.

    Anthropic prices a 1-hour cache write at 2x the base input rate (vs 1.25x for
    the 5-minute write that ``cache_creation_input_token_cost`` already encodes).
    The vendored rate table carries only the 5-minute rate, so we derive the
    1-hour rate here rather than under-pricing long-TTL writes.
    """
    key = resolve_pricing_key(model)
    if key is None:
        return None
    return MODEL_PRICING[key].input_cost_per_token * 2.0


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    cache_creation_1h_tokens: int = 0,
) -> float | None:
    """USD cost for these token counts under ``model``, or None if unpriced.

    ``cache_creation_tokens`` is the total cache-write count; the portion at the
    1-hour TTL is passed separately as ``cache_creation_1h_tokens`` and priced at
    the 1-hour rate, with the remainder at the 5-minute rate. Callers without the
    split leave ``cache_creation_1h_tokens`` at 0, pricing everything at the
    5-minute rate (the prior behavior).
    """
    key = resolve_pricing_key(model)
    if key is None:
        return None
    p = MODEL_PRICING[key]
    cache_1h = min(cache_creation_1h_tokens, cache_creation_tokens)
    cache_5m = cache_creation_tokens - cache_1h
    return (
        input_tokens * p.input_cost_per_token
        + cache_read_tokens * p.cache_read_input_token_cost
        + cache_5m * p.cache_creation_input_token_cost
        + cache_1h * (p.input_cost_per_token * 2.0)
        + output_tokens * p.output_cost_per_token
    )
