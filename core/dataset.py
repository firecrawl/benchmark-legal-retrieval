import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.canary import opened
from core.contracts import Datapoint
from core.errors import EvalError

type ParseRow[Input, Reference] = Callable[[dict[str, Any]], Datapoint[Input, Reference]]


@dataclass(frozen=True)
class RowError:
    line: int
    message: str


def _rows[Input, Reference](
    path: Path, parse_row: ParseRow[Input, Reference]
) -> Iterator[tuple[int, Datapoint[Input, Reference] | RowError]]:
    if not path.is_file():
        raise EvalError(f"dataset not found: {path}")
    ids: set[str] = set()
    rows = 0
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        rows += 1
        try:
            datapoint = parse_row(opened(json.loads(line)))
        except ValidationError as error:
            yield line_no, RowError(line_no, _describe(error))
            continue
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            yield line_no, RowError(line_no, str(error))
            continue
        if datapoint.id in ids:
            yield line_no, RowError(line_no, f"duplicate datapoint id: {datapoint.id!r}")
            continue
        ids.add(datapoint.id)
        yield line_no, datapoint
    if rows == 0:
        raise EvalError(f"dataset is empty: {path}")


def _describe(error: ValidationError) -> str:
    parts: list[str] = []
    for detail in error.errors():
        location = ".".join(str(piece) for piece in detail["loc"])
        parts.append(f"{location}: {detail['msg']}" if location else detail["msg"])
    return "; ".join(parts)


def parse_dataset[Input, Reference](
    path: Path, parse_row: ParseRow[Input, Reference]
) -> tuple[list[Datapoint[Input, Reference]], str]:
    if not path.is_file():
        raise EvalError(f"dataset not found: {path}")
    dataset_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    datapoints: list[Datapoint[Input, Reference]] = []
    for line_no, row in _rows(path, parse_row):
        if isinstance(row, RowError):
            raise EvalError(f"bad dataset row at {path}:{line_no}: {row.message}")
        datapoints.append(row)
    return datapoints, dataset_hash
