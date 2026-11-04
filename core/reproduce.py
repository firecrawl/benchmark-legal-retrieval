import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from core import suts
from core.canary import opened
from core.contracts import Datapoint, EvaluationType, Execution
from core.dataset import parse_dataset
from core.errors import EvalError, first_error
from core.evaluation_types import get_evaluation_type, register_evaluation_type
from core.metrics import RUNNER_OBSERVATIONS, MetricDefinition
from core.stats import holm, randomization_p
from core.summaries import Benchmark, Measurement, arm_summaries, datapoint_means, summary_fields

Summaries = dict[str, dict[str, dict[str, Any]]]

OVERALL = "overall"
MANIFEST_FILE = "MANIFEST.sha256"
PLAN_FILE = "runs/judge.json"
RESULTS_FILE = "results.json"
EXECUTIONS_FILE = "executions.jsonl"
GRADER_FILE = "grader_output.json"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RESTORE = "restore the published files, or run evals benchmark publish again"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntryPoints(_Strict):
    evaluation_types: list[str]
    scores: str | None
    judges: dict[str, str]


class PlanBenchmark(_Strict):
    dead_run_bar: float
    composite: str | None


class PlanArm(_Strict):
    arm: str
    directory: str
    control: bool


class JudgePlan(_Strict):
    """`runs/judge.json`: what the repository's verify rejudges, and how."""

    pack: str
    release: str
    evaluation_type: str
    judge: dict[str, Any]
    metrics: list[str]
    slices: list[str]
    repeats: int
    benchmark: PlanBenchmark | None
    entry_points: EntryPoints
    dataset: str
    arms: list[PlanArm]


@dataclass(frozen=True)
class StoredExecution:
    datapoint_id: str
    repeat: int
    failed: bool
    output: Any
    trace: list[dict[str, Any]] | None
    observations: dict[str, float | None]
    scores: dict[str, float]
    judge_version: str | None


@dataclass(frozen=True)
class Difference:
    arm: str
    datapoint: str
    repeat: int | None
    name: str
    stored: Any
    recomputed: Any


def replayed(kind: str, stored: StoredExecution) -> Execution:
    """The execution as the runner judged it: a failed one as the kind's empty output."""
    if stored.failed:
        return Execution(suts.empty_output(kind))
    observations = {
        name: value
        for name, value in stored.observations.items()
        if name not in RUNNER_OBSERVATIONS and value is not None
    }
    return Execution(suts.deserialize_output(kind, stored.output), stored.trace, observations)


def rejudge(
    evaluation_type: EvaluationType,
    judge: str,
    metrics: Sequence[str],
    datapoints: Mapping[str, Datapoint[Any, Any]],
    label: str,
    executions: Sequence[StoredExecution],
    differences: list[Difference],
) -> dict[int, list[Measurement]]:
    """Judge each stored execution again, naming every score that differs from the stored one."""
    measurements: dict[int, list[Measurement]] = {}
    for stored in sorted(executions, key=lambda e: (e.repeat, e.datapoint_id)):
        datapoint = datapoints.get(stored.datapoint_id)
        if datapoint is None:
            raise EvalError(f"{label}: datapoint {stored.datapoint_id!r} is not in the dataset")
        execution = replayed(evaluation_type.kind, stored)
        judgment = evaluation_type.judge(datapoint, execution, judge, metrics)
        cell = (label, stored.datapoint_id, stored.repeat)
        if stored.judge_version != judgment.version:
            differences.append(
                Difference(*cell, "judge version", stored.judge_version, judgment.version)
            )
        for name in sorted(set(stored.scores) | set(judgment.scores)):
            if stored.scores.get(name) != judgment.scores.get(name):
                differences.append(
                    Difference(*cell, name, stored.scores.get(name), judgment.scores.get(name))
                )
        measurements.setdefault(stored.repeat, []).append(
            Measurement(stored.datapoint_id, judgment.scores, stored.observations, stored.failed)
        )
    return measurements


def compare_fields(
    label: str,
    scope: str,
    name: str,
    stored: Mapping[str, Any],
    recomputed: Mapping[str, Any],
    differences: list[Difference],
) -> None:
    """Name every field of a stored summary that differs from its recomputed one."""
    for field in sorted(set(stored) | set(recomputed)):
        if stored.get(field) != recomputed.get(field):
            differences.append(
                Difference(
                    label, scope, None, f"{name} {field}", stored.get(field), recomputed.get(field)
                )
            )


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise EvalError(f"not an export: {path} is missing")
    return json.loads(path.read_text())


def read_arm(arm_dir: Path) -> list[StoredExecution]:
    """An exported arm's judged executions with their stored scores."""
    tasks = {
        (task["task_id"], task["repeat"]): task
        for task in read_json(arm_dir / GRADER_FILE)["tasks"]
    }
    path = arm_dir / EXECUTIONS_FILE
    if not path.is_file():
        raise EvalError(f"not an export: {path} is missing")
    executions: list[StoredExecution] = []
    seen: set[tuple[str, int]] = set()
    for line in path.read_text().splitlines():
        row = opened(json.loads(line))
        cell = (row["datapoint_id"], row["repeat"])
        task = tasks.get(cell)
        if task is None:
            raise EvalError(f"{path}: {cell[0]} repeat {cell[1]} has no row in {GRADER_FILE}")
        seen.add(cell)
        if task["judgment_status"] != "complete":
            continue
        executions.append(
            StoredExecution(
                row["datapoint_id"],
                row["repeat"],
                row["status"] == "failed",
                row["output"],
                row["trace"],
                task["observations"] or {},
                task["scores"],
                task["judge_version"],
            )
        )
    if missing := sorted(set(tasks) - seen):
        raise EvalError(f"{path}: {missing[0][0]} repeat {missing[0][1]} is missing")
    return executions


@dataclass(frozen=True)
class ArmNumbers:
    summaries: Summaries
    failed_share: float
    unmeasured: str | None
    p: dict[str, tuple[float | None, float | None]]


def _paired_p(
    definition: MetricDefinition, control: Sequence[Measurement], candidate: Sequence[Measurement]
) -> float | None:
    left = datapoint_means(control, definition)
    right = datapoint_means(candidate, definition)
    ids = sorted(set(left) & set(right))
    if len(ids) < 2:
        return None
    return randomization_p([left[i] for i in ids], [right[i] for i in ids])


def _family(
    definition: MetricDefinition,
    summaries: Mapping[str, Summaries],
    flat: Mapping[str, list[Measurement]],
) -> dict[str, tuple[float | None, float | None]]:
    control, *tested = flat
    dead = {
        label
        for label in flat
        if summaries[label][OVERALL][definition.name]["unmeasured"] is not None
    }
    raw: dict[str, float] = {}
    for label in tested if control not in dead else []:
        found = None if label in dead else _paired_p(definition, flat[control], flat[label])
        if found is not None:
            raw[label] = found
    adjusted = dict(zip(raw, holm(list(raw.values())), strict=True))
    return {label: (raw.get(label), adjusted.get(label)) for label in tested}


def _pairwise(
    definitions: Sequence[MetricDefinition],
    summaries: Mapping[str, Summaries],
    flat: Mapping[str, list[Measurement]],
) -> dict[str, dict[str, tuple[float | None, float | None]]]:
    p: dict[str, dict[str, tuple[float | None, float | None]]] = {label: {} for label in flat}
    for definition in definitions:
        if definition.scope == "score" and definition.aggregation == "mean":
            for label, pair in _family(definition, summaries, flat).items():
                p[label][definition.name] = pair
    return p


def published_numbers(
    slices: Sequence[str],
    repeats: int,
    benchmark: Benchmark | None,
    datapoints: Sequence[Datapoint[Any, Any]],
    definitions: Sequence[MetricDefinition],
    arms: Mapping[str, Mapping[int, list[Measurement]]],
) -> dict[str, ArmNumbers]:
    """Each arm's summaries, failed share and Holm-adjusted p against the first arm, the control."""
    summaries: dict[str, Summaries] = {}
    flat: dict[str, list[Measurement]] = {}
    for label, measurements in arms.items():
        found: Summaries = {}
        for key, definition, summary in arm_summaries(
            slices, repeats, benchmark, datapoints, definitions, measurements
        ):
            found.setdefault(key or OVERALL, {})[definition.name] = summary_fields(summary)
        summaries[label] = found
        flat[label] = [m for rows in measurements.values() for m in rows]
    p = _pairwise(definitions, summaries, flat)
    cells = len(datapoints) * repeats
    numbers: dict[str, ArmNumbers] = {}
    for label, rows in flat.items():
        failed = cells - len(rows) + sum(m.failed for m in rows)
        reasons = [
            fields["unmeasured"]
            for fields in summaries[label].get(OVERALL, {}).values()
            if fields["unmeasured"] is not None
        ]
        numbers[label] = ArmNumbers(
            summaries[label], failed / cells, reasons[0] if reasons else None, p[label]
        )
    return numbers


def published_cell(numbers: ArmNumbers, scope: str, metric: str) -> dict[str, Any] | None:
    """One results.json cell: the summary's fields, and its p values on an overall row."""
    fields = numbers.summaries.get(scope, {}).get(metric)
    if fields is None:
        return None
    raw, adjusted = numbers.p.get(metric, (None, None)) if scope == OVERALL else (None, None)
    return {**fields, "p": raw, "p_holm": adjusted}


def _load(pack: str, root: Path, reference: str) -> Any:
    file_part, _, symbol = reference.partition(":")
    target = root / file_part
    package = target.is_dir()
    file = target / "__init__.py" if package else target
    name = re.sub(r"\W", "_", f"evals_pack_{pack}_{file_part.removesuffix('.py')}")
    spec = importlib.util.spec_from_file_location(
        name, file, submodule_search_locations=[str(target)] if package else None
    )
    if spec is None or spec.loader is None or not file.is_file():
        raise EvalError(f"pack {pack!r}: cannot load {file_part}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return getattr(module, symbol)


def _register(plan: JudgePlan, root: Path) -> EvaluationType:
    pack = plan.pack
    owner = f"pack:{pack}"
    points = plan.entry_points
    for reference in points.evaluation_types:
        register_evaluation_type(_load(pack, root, reference)(), owner)
    evaluation_type = get_evaluation_type(plan.evaluation_type)
    if points.scores is not None:
        evaluation_type.register_scores(_load(pack, root, points.scores), owner)
    for name, reference in points.judges.items():
        evaluation_type.register_judge(name, _load(pack, root, reference)(), owner)
    return evaluation_type


def _manifest(directory: Path, differences: list[Difference]) -> tuple[int, str]:
    lines = (directory / MANIFEST_FILE).read_text().splitlines()
    if not lines or not lines[0].startswith("# fire-evals "):
        raise EvalError(f"{MANIFEST_FILE} has no '# fire-evals <commit>' header · {_RESTORE}")
    commit = lines[0].removeprefix("# fire-evals ").split(" ")[0]
    entries: list[tuple[str, str]] = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        if not separator or _SHA256.fullmatch(digest) is None or not name:
            raise EvalError(
                f"{MANIFEST_FILE} line {number} is not '<sha256>  <path>': {line!r} · {_RESTORE}"
            )
        entries.append((digest, name))
    for digest, name in entries:
        path = directory / name
        found = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if found != digest:
            differences.append(Difference(MANIFEST_FILE, name, None, "sha256", digest, found))
    return len(entries), commit


def _planned_arms(
    results: dict[str, Any], plan: JudgePlan, differences: list[Difference]
) -> dict[str, dict[str, Any]]:
    """The results.json arms the plan names, after checking the arm set, controls and comparison arm."""
    planned = {arm.arm: arm for arm in plan.arms}
    shown = {arm["arm"]: arm for arm in results["arms"]}
    if sorted(shown) != sorted(planned):
        differences.append(
            Difference(RESULTS_FILE, "arms", None, "arm labels", sorted(shown), sorted(planned))
        )
    against = results["pairwise"]["against"]
    if against != plan.arms[0].arm:
        differences.append(
            Difference(RESULTS_FILE, "pairwise", None, "against", against, plan.arms[0].arm)
        )
    for label in planned.keys() & shown.keys():
        control = shown[label]["control"]
        if control != planned[label].control:
            differences.append(
                Difference(label, OVERALL, None, "control", control, planned[label].control)
            )
    return {label: arm for label, arm in shown.items() if label in planned}


def _results_differences(
    results: dict[str, Any],
    plan: JudgePlan,
    numbers: Mapping[str, ArmNumbers],
    differences: list[Difference],
) -> int:
    checked = 0
    for label, arm in _planned_arms(results, plan, differences).items():
        recomputed = numbers[label]
        compare_fields(
            label,
            OVERALL,
            "public",
            {
                "failed_share": arm["failed_share"]["public"],
                "unmeasured": arm["unmeasured"]["public"],
            },
            {"failed_share": recomputed.failed_share, "unmeasured": recomputed.unmeasured},
            differences,
        )
        rows = {(row["scope"], row["metric"]): row["public"] for row in arm["rows"]}
        keys = {
            (scope, metric) for scope, found in recomputed.summaries.items() for metric in found
        }
        for scope, metric in sorted(set(rows) | keys):
            checked += 1
            stored: dict[str, Any] = rows.get((scope, metric)) or {}
            cell = published_cell(recomputed, scope, metric) or {}
            compare_fields(label, scope, metric, stored, cell, differences)
    return checked


@dataclass(frozen=True)
class Reproduction:
    pack: str
    release: str
    commit: str
    judge: str
    version: str
    released_version: str
    files: int
    executions: int
    summaries: int
    differences: list[Difference]


def reproduce(directory: Path) -> Reproduction:
    """Check a benchmark repository: its manifest, its judge, every shipped score, the public half."""
    differences: list[Difference] = []
    files, commit = _manifest(directory, differences)
    try:
        plan = JudgePlan.model_validate_json((directory / PLAN_FILE).read_text())
    except ValidationError as error:
        raise EvalError(f"{PLAN_FILE}: {first_error(error)} · {_RESTORE}") from error
    evaluation_type = _register(plan, directory / "pack")
    judge, metrics, released = plan.judge["name"], plan.metrics, plan.judge["version"]
    version = evaluation_type.judge_version(judge, metrics)
    if version != released:
        differences.append(Difference(PLAN_FILE, judge, None, "judge version", released, version))
    datapoints, _ = parse_dataset(directory / plan.dataset, evaluation_type.validate_reference)
    by_id = {datapoint.id: datapoint for datapoint in datapoints}
    executions = 0
    measurements: dict[str, dict[int, list[Measurement]]] = {}
    for arm in plan.arms:
        stored = read_arm(directory / arm.directory)
        executions += len(stored)
        measurements[arm.arm] = rejudge(
            evaluation_type, judge, metrics, by_id, arm.arm, stored, differences
        )
    block = plan.benchmark
    numbers = published_numbers(
        plan.slices,
        plan.repeats,
        None if block is None else Benchmark(block.dead_run_bar, block.composite),
        datapoints,
        [evaluation_type.metrics[name] for name in metrics],
        measurements,
    )
    results = json.loads((directory / RESULTS_FILE).read_text())
    summaries = _results_differences(results, plan, numbers, differences)
    return Reproduction(
        plan.pack,
        plan.release,
        commit,
        judge,
        version,
        released,
        files,
        executions,
        summaries,
        differences,
    )
