from app.providers.base import Provider, ProviderError
from app.providers.llm import LlmProvider
from app.providers.mock import MockProvider

_PROVIDERS: dict[str, Provider] = {
    "mock": MockProvider(),
    "llm": LlmProvider(),
}

DEFAULT_PROVIDER = "mock"


def get_provider(name: str) -> Provider:
    return _PROVIDERS[name]


def known(name: str) -> bool:
    return name in _PROVIDERS


__all__ = ["Provider", "ProviderError", "get_provider", "known", "DEFAULT_PROVIDER"]
