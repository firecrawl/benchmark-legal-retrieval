from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    measurement: str
    aggregation: Literal["mean", "sum", "p50", "p95"] = "mean"
    scope: Literal["score", "observation"] = "score"
    unit: str = "ratio"
    direction: Literal["maximize", "minimize", "none"] = "maximize"
    version: str = "1"
    cutoff: int | None = None
    gate: dict[str, float] | None = None
