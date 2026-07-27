"""Model pricing — cost per token for known models.

Prices are in USD per 1M tokens (the standard billing unit). The lookup
is prefix-based: ``claude-sonnet-4-`` matches any variant of Sonnet 4.

Prices sourced from provider pricing pages as of 2025-05.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Cost per million tokens (USD)."""

    input_per_m: float
    output_per_m: float


# Ordered longest-prefix-first so more-specific entries win.
_PRICING_TABLE: tuple[tuple[str, ModelPrice], ...] = (
    # Anthropic
    ("claude-opus-4", ModelPrice(input_per_m=15.0, output_per_m=75.0)),
    ("claude-sonnet-4", ModelPrice(input_per_m=3.0, output_per_m=15.0)),
    ("claude-haiku-4", ModelPrice(input_per_m=0.80, output_per_m=4.0)),
    ("claude-3-5-sonnet", ModelPrice(input_per_m=3.0, output_per_m=15.0)),
    ("claude-3-5-haiku", ModelPrice(input_per_m=0.80, output_per_m=4.0)),
    ("claude-3-opus", ModelPrice(input_per_m=15.0, output_per_m=75.0)),
    ("claude-3-sonnet", ModelPrice(input_per_m=3.0, output_per_m=15.0)),
    ("claude-3-haiku", ModelPrice(input_per_m=0.25, output_per_m=1.25)),
    # OpenAI
    ("gpt-4o-mini", ModelPrice(input_per_m=0.15, output_per_m=0.60)),
    ("gpt-4o", ModelPrice(input_per_m=2.50, output_per_m=10.0)),
    ("gpt-4-turbo", ModelPrice(input_per_m=10.0, output_per_m=30.0)),
    ("gpt-4", ModelPrice(input_per_m=30.0, output_per_m=60.0)),
    ("o4-mini", ModelPrice(input_per_m=1.10, output_per_m=4.40)),
    ("o3-mini", ModelPrice(input_per_m=1.10, output_per_m=4.40)),
    ("o3", ModelPrice(input_per_m=10.0, output_per_m=40.0)),
    ("o1-mini", ModelPrice(input_per_m=3.0, output_per_m=12.0)),
    ("o1", ModelPrice(input_per_m=15.0, output_per_m=60.0)),
)


def get_price(model: str) -> ModelPrice | None:
    """Look up pricing for a model by prefix match. Returns None if unknown."""
    for prefix, price in _PRICING_TABLE:
        if model.startswith(prefix):
            return price
    return None


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Compute USD cost for a request. Returns None if model pricing is unknown."""
    price = get_price(model)
    if price is None:
        return None
    return (input_tokens * price.input_per_m + output_tokens * price.output_per_m) / 1_000_000
