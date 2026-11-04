from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from core.contracts import Datapoint
from core.metrics import MetricDefinition, MetricSummary, Uncertainty, summarize
from core.stats import Interval, arm_interval, slice_mean, slice_mean_interval, unmeasured


@dataclass(frozen=True)
class Measurement:
    datapoint_id: str
    scores: dict[str, float]
    observations: dict[str, float | None]
    failed: bool = False


@dataclass(frozen=True)
class Benchmark:
    dead_run_bar: float
    composite: str | None


def summary_fields(summary: MetricSummary) -> dict[str, Any]:
    interval = summary.uncertainty
    bounded = interval is not None and interval.low is not None
    return {
        "value": summary.value,
        "count": summary.count,
        "repeats": summary.repeats,
        "ci95_half_width": interval.half_width if interval is not None and not bounded else None,
        "ci95_low": interval.low if interval is not None else None,
        "ci95_high": interval.high if interval is not None else None,
        "unmeasured": summary.unmeasured,
    }


def _value(measurement: Measurement, definition: MetricDefinition) -> float | None:
    source = measurement.scores if definition.scope == "score" else measurement.observations
    return source.get(definition.measurement)


def datapoint_means(
    measurements: Sequence[Measurement], definition: MetricDefinition
) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for measurement in measurements:
        value = _value(measurement, definition)
        if value is not None:
            values.setdefault(measurement.datapoint_id, []).append(value)
    return {datapoint: sum(found) / len(found) for datapoint, found in values.items()}


def _interval(summary: MetricSummary, interval: Interval) -> MetricSummary:
    uncertainty = Uncertainty(
        (interval.high - interval.low) / 2,
        method=interval.method,
        target="datapoint_mean",
        low=interval.low,
        high=interval.high,
    )
    return replace(summary, uncertainty=uncertainty)


Selection = tuple[tuple[str, str] | None, list[list[Measurement]], int]


def _benchmark_summary(
    definition: MetricDefinition,
    summary: MetricSummary,
    rows: Sequence[Sequence[Measurement]],
    cells: int,
    benchmark: Benchmark,
) -> MetricSummary:
    flat = [m for repeat in rows for m in repeat]
    failed = cells - len(flat) + sum(m.failed for m in flat)
    reason = unmeasured(failed, cells, benchmark.dead_run_bar)
    if reason is not None:
        return MetricSummary(None, summary.count, summary.repeats, None, reason)
    if definition.aggregation != "mean" or not summary.count:
        return summary
    return _interval(summary, arm_interval(list(datapoint_means(flat, definition).values())))


def _selections(
    slices: Sequence[str],
    repeats: int,
    datapoints: Sequence[Datapoint[Any, Any]],
    measurements: Mapping[int, list[Measurement]],
) -> list[Selection]:
    metadata = {datapoint.id: datapoint.metadata for datapoint in datapoints}
    groups: list[tuple[tuple[str, str] | None, set[str]]] = [(None, set(metadata))]
    for dimension in slices:
        by_label: dict[str, set[str]] = {}
        for datapoint_id, labels in metadata.items():
            if dimension in labels:
                by_label.setdefault(str(labels[dimension]), set()).add(datapoint_id)
        groups.extend(((dimension, label), by_label[label]) for label in sorted(by_label))
    passes = [measurements[repeat] for repeat in sorted(measurements)]
    return [
        (
            selected,
            [[m for m in rows if m.datapoint_id in chosen] for rows in passes],
            len(chosen) * repeats,
        )
        for selected, chosen in groups
    ]


def _composite(
    definition: MetricDefinition,
    selections: Sequence[Selection],
    benchmark: Benchmark,
    repeats: int,
) -> MetricSummary | None:
    groups: dict[str, list[float]] = {}
    dead: list[str] = []
    count = 0
    for selected, found, cells in selections:
        if selected is None or selected[0] != benchmark.composite:
            continue
        flat = [m for repeat in found for m in repeat]
        groups[selected[1]] = list(datapoint_means(flat, definition).values())
        count += sum(1 for m in flat if _value(m, definition) is not None)
        failed = cells - len(flat) + sum(m.failed for m in flat)
        if unmeasured(failed, cells, benchmark.dead_run_bar) is not None:
            dead.append(f"{selected[0]}={selected[1]}")
    if dead:
        return MetricSummary(None, count, repeats, None, f"{', '.join(dead)} unmeasured")
    if not groups or not all(groups.values()):
        return None
    summary = MetricSummary(slice_mean(groups), count, repeats)
    return _interval(summary, slice_mean_interval(groups))


def arm_summaries(
    slices: Sequence[str],
    repeats: int,
    benchmark: Benchmark | None,
    datapoints: Sequence[Datapoint[Any, Any]],
    definitions: Sequence[MetricDefinition],
    measurements: Mapping[int, list[Measurement]],
) -> list[tuple[str, MetricDefinition, MetricSummary]]:
    """Every metric summary of one arm, keyed by slice ('' overall), from its measurements."""
    selections = _selections(slices, repeats, datapoints, measurements)
    summaries: list[tuple[str, MetricDefinition, MetricSummary]] = []
    for definition in definitions:
        for selected, rows, cells in selections:
            per_repeat = [
                [v for m in repeat if (v := _value(m, definition)) is not None] for repeat in rows
            ]
            summary = summarize(definition, per_repeat)
            if benchmark is not None:
                summary = _benchmark_summary(definition, summary, rows, cells, benchmark)
            key = "" if selected is None else f"{selected[0]}={selected[1]}"
            summaries.append((key, definition, summary))
        if benchmark is not None and benchmark.composite and definition.aggregation == "mean":
            composite = _composite(definition, selections, benchmark, repeats)
            if composite is not None:
                summaries.append((f"slice_mean({benchmark.composite})", definition, composite))
    return summaries
