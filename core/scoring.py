import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict
from typing import Any, Protocol, runtime_checkable

from core.contracts import Datapoint, Execution, JudgeSpec, Judgment, ScoreFunction, ScoreTable
from core.errors import EvalError
from core.metrics import MetricDefinition


def score_function_name(measurement: str) -> str:
    return measurement.partition("@")[0]


def score_names(metrics: Mapping[str, MetricDefinition]) -> tuple[str, ...]:
    names = [d.measurement for d in metrics.values() if d.scope == "score"]
    return tuple(dict.fromkeys(names))


def check_score_table(
    metrics: Mapping[str, MetricDefinition], scores: Sequence[str], owner: str
) -> None:
    for definition in metrics.values():
        if definition.scope != "score":
            continue
        base = score_function_name(definition.measurement)
        expected = base if definition.cutoff is None else f"{base}@{definition.cutoff}"
        if definition.measurement != expected:
            raise EvalError(
                f"{owner}: metric {definition.name!r} measures {definition.measurement!r} "
                f"but its cutoff is {definition.cutoff!r}; name it {expected!r}"
            )
        if definition.measurement not in scores:
            raise EvalError(
                f"{owner}: metric {definition.name!r} needs score {definition.measurement!r}, "
                f"which is not produced (produced: {list(scores)})"
            )


def check_table(table: ScoreTable[Any], owner: str) -> None:
    check_score_table(
        table.metrics,
        tuple(
            definition.measurement
            for definition in table.metrics.values()
            if definition.scope == "score"
            and score_function_name(definition.measurement) in table.functions
        ),
        owner,
    )


def scores_version(matcher: str, metrics: Iterable[MetricDefinition]) -> str:
    digest = hashlib.sha256(matcher.encode())
    for definition in sorted((m for m in metrics if m.scope == "score"), key=lambda m: m.name):
        digest.update(f"\0{definition.name}:{definition.measurement}:{definition.version}".encode())
    return digest.hexdigest()[:12]


def score_all(
    metrics: Mapping[str, MetricDefinition],
    functions: Mapping[str, ScoreFunction[Any]],
    evidence: Any,
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for definition in metrics.values():
        if definition.scope != "score" or definition.measurement in scores:
            continue
        function = functions.get(score_function_name(definition.measurement))
        if function is None:
            raise EvalError(
                f"metric {definition.name!r}: no score function for {definition.measurement!r}"
            )
        value = function(evidence, definition.cutoff)
        if value is not None:
            scores[definition.measurement] = value
    return scores


@runtime_checkable
class CustomJudge(Protocol):
    version: str
    scores: tuple[str, ...]
    metrics: Mapping[str, MetricDefinition]

    def judge(self, datapoint: Datapoint[Any, Any], execution: Execution) -> Judgment: ...


@runtime_checkable
class ReadingJudge(CustomJudge, Protocol):
    async def read(
        self, datapoint: Datapoint[Any, Any], execution: Execution
    ) -> list[dict[str, Any]]: ...


type Match = Callable[[Datapoint[Any, Any], Execution], Any]


class DeterministicJudge:
    name = "deterministic"

    def __init__(
        self, matcher: str, match: Match, metrics: Mapping[str, MetricDefinition], description: str
    ) -> None:
        self.matcher = matcher
        self.match = match
        self.metrics = metrics
        self.description = description
        self.functions: dict[str, ScoreFunction[Any]] = {}
        self.scores: tuple[str, ...] = ()

    def add(self, table: ScoreTable[Any]) -> None:
        self.functions.update(table.functions)
        self.scores = tuple(dict.fromkeys(self.scores + score_names(table.metrics)))

    @property
    def spec(self) -> JudgeSpec:
        return JudgeSpec(self.name, self.scores, self.description)

    def _selected(self, metric_names: Sequence[str]) -> dict[str, MetricDefinition]:
        return {
            name: self.metrics[name]
            for name in metric_names
            if name in self.metrics and self.metrics[name].measurement in self.scores
        }

    def version(self, metric_names: Sequence[str]) -> str:
        return scores_version(self.matcher, self._selected(metric_names).values())

    def judge(
        self, datapoint: Datapoint[Any, Any], execution: Execution, metric_names: Sequence[str]
    ) -> Judgment:
        evidence = self.match(datapoint, execution)
        scores = score_all(self._selected(metric_names), self.functions, evidence)
        return Judgment(self.name, self.version(metric_names), scores, asdict(evidence))


class ScoreRegistry:
    def __init__(
        self,
        type_name: str,
        matcher: str,
        match: Match,
        description: str,
        observation_metrics: Mapping[str, MetricDefinition],
    ) -> None:
        self.type_name = type_name
        self.metrics: dict[str, MetricDefinition] = dict(observation_metrics)
        self.deterministic = DeterministicJudge(matcher, match, self.metrics, description)
        self._custom: dict[str, CustomJudge] = {}
        self._judge_owners: dict[str, str] = {}
        self._function_owners: dict[str, str] = {}
        self._metric_owners: dict[str, str] = dict.fromkeys(observation_metrics, "core")

    @property
    def judges(self) -> dict[str, JudgeSpec]:
        specs = {self.deterministic.name: self.deterministic.spec}
        for name, judge in self._custom.items():
            model = getattr(judge, "model", None)
            specs[name] = JudgeSpec(
                judge.version,
                tuple(judge.scores),
                str(getattr(judge, "description", "")),
                None if model is None else str(model),
                model is not None,
                isinstance(judge, ReadingJudge),
            )
        return specs

    def _claim_metrics(self, metrics: Mapping[str, MetricDefinition], owner: str) -> None:
        for metric_name, definition in metrics.items():
            existing = self._metric_owners.get(metric_name)
            if existing is not None and existing != owner:
                raise EvalError(f"metric {metric_name!r} is already defined by {existing!r}")
            self.metrics[metric_name] = definition
            self._metric_owners[metric_name] = owner

    def register_scores(self, table: ScoreTable[Any], owner: str) -> None:
        check_table(table, f"{owner} scores")
        for function_name in table.functions:
            existing = self._function_owners.get(function_name)
            if existing is not None and existing != owner:
                raise EvalError(
                    f"score function {function_name!r} is already defined by {existing!r}"
                )
        self._claim_metrics(table.metrics, owner)
        self._function_owners.update({name: owner for name in table.functions})
        self.deterministic.add(table)

    def register_judge(self, name: str, judge: object, owner: str) -> None:
        if name == self.deterministic.name:
            raise EvalError(
                f"judge {name!r} is {self.type_name}'s own; register a score table instead"
            )
        if not isinstance(judge, CustomJudge):
            raise EvalError(
                f"judge {name!r} must expose version, scores, metrics, and judge(datapoint, execution)"
            )
        existing = self._judge_owners.get(name)
        if existing is not None and existing != owner:
            raise EvalError(
                f"judge {name!r} is already registered for {self.type_name} by {existing!r}"
            )
        check_score_table(judge.metrics, judge.scores, f"judge {name!r}")
        self._custom[name] = judge
        self._judge_owners[name] = owner
        self._claim_metrics(judge.metrics, owner)

    def metric_owner(self, name: str) -> str:
        return self._metric_owners[name]

    def judge_version(self, judge_name: str, metrics: Sequence[str]) -> str:
        if judge_name == self.deterministic.name:
            return self.deterministic.version(metrics)
        spec = self.judges.get(judge_name)
        if spec is None:
            raise EvalError(f"unknown {self.type_name} judge: {judge_name!r}")
        return spec.version

    def judge(
        self,
        datapoint: Datapoint[Any, Any],
        execution: Execution,
        judge_name: str,
        metrics: Sequence[str],
    ) -> Judgment:
        if judge_name == self.deterministic.name:
            return self.deterministic.judge(datapoint, execution, metrics)
        custom = self._custom.get(judge_name)
        if custom is None:
            raise EvalError(f"unknown {self.type_name} judge: {judge_name!r}")
        return custom.judge(datapoint, execution)

    async def read(
        self, datapoint: Datapoint[Any, Any], execution: Execution, judge_name: str
    ) -> list[dict[str, Any]]:
        custom = self._custom.get(judge_name)
        if not isinstance(custom, ReadingJudge):
            return []
        return await custom.read(datapoint, execution)


class RegistryBacked:
    """What an evaluation type delegates to its ScoreRegistry and its deterministic judge."""

    default_judge = DeterministicJudge.name
    _registry: ScoreRegistry
    _trace_records: frozenset[str]

    @property
    def metrics(self) -> dict[str, MetricDefinition]:
        return self._registry.metrics

    @property
    def judges(self) -> dict[str, JudgeSpec]:
        return self._registry.judges

    def register_scores(self, table: ScoreTable[Any], owner: str) -> None:
        self._registry.register_scores(table, owner)

    def register_judge(self, name: str, judge: object, owner: str) -> None:
        self._registry.register_judge(name, judge, owner)

    def metric_owner(self, name: str) -> str:
        return self._registry.metric_owner(name)

    def judge_version(self, judge_name: str, metrics: Sequence[str]) -> str:
        return self._registry.judge_version(judge_name, metrics)

    def trace_records(self, judge_name: str) -> frozenset[str] | None:
        return self._trace_records if judge_name == self.default_judge else None

    def judge(
        self,
        datapoint: Datapoint[Any, Any],
        execution: Execution,
        judge_name: str,
        metrics: Sequence[str],
    ) -> Judgment:
        return self._registry.judge(datapoint, execution, judge_name, metrics)
