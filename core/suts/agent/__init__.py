from typing import Any

from core.suts.agent.citations import Citation

KIND = "agent"

__all__ = [
    "KIND",
    "deserialize_output",
]


def deserialize_output(stored: list[dict[str, Any]]) -> list[Citation]:
    return [Citation(**citation) for citation in stored]


def empty_output() -> list[Citation]:
    return []
