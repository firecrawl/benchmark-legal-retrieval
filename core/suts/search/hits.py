from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError


@dataclass(frozen=True)
class Hit:
    url: str
    relevance: float | None = None
    id: str | None = None


class _Stored(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    relevance: float | None = None
    id: str | None = None


_STORED = TypeAdapter(list[_Stored])


def parse_hits(output: Any) -> list[Hit] | None:
    try:
        rows = _STORED.validate_python(output)
    except ValidationError:
        return None
    return [Hit(row.url, row.relevance, row.id) for row in rows]
