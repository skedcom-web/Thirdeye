"""Adapter registry: maps the `adapter` column on a source to an implementation."""

from __future__ import annotations

from .base import Adapter, DiscoveredLink, PageResult
from .generic_links import GenericLinksAdapter
from .tn_go_portal import TnGoPortalAdapter

_ADAPTERS: dict[str, type] = {
    GenericLinksAdapter.name: GenericLinksAdapter,
    TnGoPortalAdapter.name: TnGoPortalAdapter,
}


def get_adapter(name: str) -> Adapter:
    try:
        return _ADAPTERS[name]()  # type: ignore[return-value]
    except KeyError:
        raise LookupError(
            f"unknown adapter {name!r}; available: {', '.join(sorted(_ADAPTERS))}"
        ) from None


def available() -> list[str]:
    return sorted(_ADAPTERS)


__all__ = [
    "Adapter",
    "DiscoveredLink",
    "PageResult",
    "get_adapter",
    "available",
]
