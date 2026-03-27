"""LLM service — wrapper over LLM providers (factory pattern).

Currently supports: claude (OpenAI-compatible API).
Future: gemini, etc.
"""

import logging

from llm.claude import ClaudeLLM

logger = logging.getLogger(__name__)

# Default provider
_provider = None


def get_llm() -> ClaudeLLM:
    """Get the LLM provider instance (singleton)."""
    global _provider
    if _provider is None:
        logger.info("[LLM_SERVICE] Initializing LLM provider: claude")
        _provider = ClaudeLLM()
    return _provider
