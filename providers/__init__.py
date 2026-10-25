"""Data providers. Each one returns plain dicts so sync.py doesn't depend on any SDK.

To add a provider (SimpleFIN, Teller, ...), implement the same methods as PlaidProvider
and raise ProviderError with a short error code on failure.
"""
from .plaid import PlaidProvider, ProviderError

__all__ = ["PlaidProvider", "ProviderError"]