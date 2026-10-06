"""Shared exporter base + registry for the roster exports catalog."""
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)


# An async generator function: call it with a db session, iterate for rows.
# First yielded row is the header.
RowGenerator = Callable[[AsyncSession], AsyncIterator[list[str]]]


@dataclass
class Exporter:
    id: str
    name: str
    for_app: str
    description: str
    filename_pattern: str
    generator: RowGenerator
    format: str = "csv"
    tags: list[str] = field(default_factory=list)


_registry: list[Exporter] = []

_discovery_callbacks: list[Callable[[AsyncSession], Awaitable[Any]]] = []
_discovered = False


def register(exporter: Exporter) -> None:
    if any(e.id == exporter.id for e in _registry):
        raise ValueError(f"Exporter id '{exporter.id}' already registered")
    _registry.append(exporter)


def register_discovery(cb: Callable[[AsyncSession], Awaitable[Any]]) -> None:
    """Register an async callback that runs once (per process) before
    the catalog is first served. Callback should call `register(...)`
    with whatever exporters it discovers from live DB state."""
    _discovery_callbacks.append(cb)


async def ensure_discovered(db: AsyncSession) -> None:
    """Idempotent: runs each registered discovery callback exactly once."""
    global _discovered
    if _discovered:
        return
    _discovered = True
    for cb in _discovery_callbacks:
        try:
            await cb(db)
        except Exception as e:
            logger.warning(f"exporter discovery failed ({cb.__name__}): {e}")


def get_all() -> list[Exporter]:
    return sorted(_registry, key=lambda e: e.name.lower())


def get_by_id(exporter_id: str) -> Exporter | None:
    return next((e for e in _registry if e.id == exporter_id), None)
