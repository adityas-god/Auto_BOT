"""
AI Provider Registry.
Allows registering, discovering, and dynamically retrieving AI vision providers.
"""

from typing import Dict, List, Optional
from ai_engine.providers.base_provider import BaseVisionAIProvider
from ai_engine.providers.gemini_provider import GeminiVisionProvider
from ai_engine.providers.openai_provider import OpenAIVisionProvider


_PROVIDERS: Dict[str, BaseVisionAIProvider] = {}


def register_provider(provider: BaseVisionAIProvider) -> None:
    """Registers an AI provider instance."""
    _PROVIDERS[provider.name.lower()] = provider


def get_provider(name: Optional[str] = None) -> Optional[BaseVisionAIProvider]:
    """Retrieves a provider by name, defaulting to 'gemini'."""
    target = (name or "gemini").strip().lower()
    return _PROVIDERS.get(target)


def list_providers() -> List[str]:
    """Returns list of registered provider names."""
    return list(_PROVIDERS.keys())


# Auto-register built-in providers
register_provider(GeminiVisionProvider())
register_provider(OpenAIVisionProvider())

__all__ = [
    "BaseVisionAIProvider",
    "GeminiVisionProvider",
    "OpenAIVisionProvider",
    "register_provider",
    "get_provider",
    "list_providers"
]
