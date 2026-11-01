"""Picks the data source: the real eBay Browse API, or the demo simulator when no keys are set."""
from __future__ import annotations

from .config import settings

_provider = None


def get():
    global _provider
    if _provider is None:
        if settings.demo_mode:
            from .demo import DemoProvider

            _provider = DemoProvider()
        else:
            from .ebay import EbayClient

            _provider = EbayClient()
    return _provider
