"""Model selection and cost estimation for Claude API calls."""

from __future__ import annotations

import re

# Keywords that trigger each model tier
OPUS_KEYWORDS = [
    "architecture", "system design", "complex", "enterprise",
    "security audit", "scalable infrastructure", "distributed system",
]

HAIKU_KEYWORDS = [
    "summarize", "format", "translate", "fix typo", "list the",
    "what is", "explain briefly", "define", "spell check",
]

# Correct model IDs from Anthropic docs (March 2026)
MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-5-20250929"
MODEL_OPUS   = "claude-opus-4-5-20251101"

# Pricing per 1M tokens (USD)
PRICING = {
    MODEL_HAIKU:  {"input": 1.0,  "output": 5.0},
    MODEL_SONNET: {"input": 3.0,  "output": 15.0},
    MODEL_OPUS:   {"input": 5.0,  "output": 25.0},
}

# Friendly aliases
MODEL_ALIASES = {
    MODEL_HAIKU:  "Haiku 4.5",
    MODEL_SONNET: "Sonnet 4.5",
    MODEL_OPUS:   "Opus 4.5",
}


def route_model(prompt: str) -> tuple[str, str]:
    """Select the best Claude model based on the prompt content.

    Returns:
        (model_id, reason) tuple
    """
    prompt_lower = prompt.lower()

    # Check for opus-level complexity
    for keyword in OPUS_KEYWORDS:
        if keyword in prompt_lower:
            return (
                MODEL_OPUS,
                f"Complex task detected (matched: '{keyword}') → using Opus",
            )

    # Check for haiku-level simplicity
    for keyword in HAIKU_KEYWORDS:
        if keyword in prompt_lower:
            return (
                MODEL_HAIKU,
                f"Simple task detected (matched: '{keyword}') → using Haiku",
            )

    # Default to sonnet
    return (
        MODEL_SONNET,
        "Standard task → using Sonnet",
    )


def estimate_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Estimate the API call cost in USD.

    Args:
        input_tokens: Number of input tokens used
        output_tokens: Number of output tokens used
        model: The model ID string

    Returns:
        Estimated cost in USD
    """
    pricing = None
    for model_id, prices in PRICING.items():
        if model_id in model or model in model_id:
            pricing = prices
            break

    if pricing is None:
        pricing = PRICING[MODEL_SONNET]

    input_cost  = (input_tokens  / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    return round(input_cost + output_cost, 6)


def get_model_alias(model: str) -> str:
    """Get a friendly name for a model ID."""
    for model_id, alias in MODEL_ALIASES.items():
        if model_id in model or model in model_id:
            return alias
    return model
