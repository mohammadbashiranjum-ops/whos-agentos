"""
App Settings
============
Shared runtime objects for the platform.
"""

from os import getenv

from agno.models.openai import OpenAIResponses
from agno.models.openai.like import OpenAILike

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_OPENAI_MODEL = "gpt-5.6"


def active_model_provider() -> tuple[str, str]:
    """Return the configured core provider/model without exposing credentials."""
    if getenv("GROQ_API_KEY"):
        return "groq", getenv("AGENTOS_GROQ_MODEL", DEFAULT_GROQ_MODEL)
    if getenv("OPENAI_API_KEY"):
        return "openai", getenv("AGENTOS_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    return "unconfigured", ""


def default_model() -> OpenAILike | OpenAIResponses:
    """Fresh model instance per agent; prefer the already-provisioned Groq lane."""
    provider, model_id = active_model_provider()
    if provider == "groq":
        return OpenAILike(
            id=model_id,
            api_key=getenv("GROQ_API_KEY"),
            base_url=GROQ_BASE_URL,
        )
    return OpenAIResponses(id=model_id or DEFAULT_OPENAI_MODEL)
