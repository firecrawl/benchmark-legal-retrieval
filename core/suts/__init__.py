from typing import Any, Protocol

from core.errors import EvalError
from core.suts import agent, record, search


class Kind(Protocol):
    def resolve_request(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def serialize_output(self, output: Any) -> Any: ...

    def deserialize_output(self, stored: Any) -> Any: ...

    def is_empty(self, output: Any) -> bool: ...

    def empty_output(self) -> Any: ...


KINDS: dict[str, Kind] = {search.KIND: search, agent.KIND: agent, record.KIND: record}

__all__ = [
    "KINDS",
    "Kind",
    "deserialize_output",
    "empty_output",
]


def _kind(kind: str) -> Kind:
    package = KINDS.get(kind)
    if package is None:
        raise EvalError(f"unknown sut kind: {kind!r}")
    return package


def deserialize_output(kind: str, stored: Any) -> Any:
    return _kind(kind).deserialize_output(stored)


def empty_output(kind: str) -> Any:
    return _kind(kind).empty_output()
