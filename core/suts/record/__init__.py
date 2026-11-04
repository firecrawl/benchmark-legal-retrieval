from typing import Any, cast

from core.errors import EvalError

KIND = "record"

__all__ = [
    "KIND",
    "deserialize_output",
    "empty_output",
]


def deserialize_output(stored: Any) -> dict[str, Any]:
    if not isinstance(stored, dict):
        raise EvalError(f"stored record output is not an object: {stored!r}")
    return cast(dict[str, Any], stored)


def empty_output() -> dict[str, Any]:
    return {}
