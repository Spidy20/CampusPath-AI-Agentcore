import os

from strands.models.bedrock import BedrockModel

DEFAULT_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "apac.amazon.nova-pro-v1:0",
)
DEFAULT_TEMPERATURE = float(os.getenv("BEDROCK_TEMPERATURE", "0.4"))
DEFAULT_TOP_P = float(os.getenv("BEDROCK_TOP_P", "0.9"))
DEFAULT_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "4096"))


def load_model(
    *,
    model_id: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
) -> BedrockModel:
    """Get Bedrock model client using IAM credentials and optional overrides."""
    return BedrockModel(
        model_id=model_id or DEFAULT_MODEL_ID,
        temperature=DEFAULT_TEMPERATURE if temperature is None else temperature,
        top_p=DEFAULT_TOP_P if top_p is None else top_p,
        max_tokens=DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens,
    )
