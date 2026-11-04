import math
from collections.abc import Sequence

from core.contracts import ScoreFunction, ScoreTable
from core.evaluation_types.retrieval.evidence import RetrievalEvidence
from core.metrics import MetricDefinition


def dcg(gains: Sequence[float]) -> float:
    return sum(gain / math.log2(rank + 2) for rank, gain in enumerate(gains))


def ndcg(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    ideal = dcg([2**grade - 1 for grade in evidence.ideal_grades[:cutoff]])
    return dcg([2**grade - 1 for grade in evidence.grades[:cutoff]]) / ideal if ideal else 0.0


def recall(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    relevant = sum(grade > 0 for grade in evidence.ideal_grades)
    return sum(grade > 0 for grade in evidence.grades[:cutoff]) / relevant if relevant else 0.0


def hit(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    return 1.0 if any(grade > 0 for grade in evidence.grades[:cutoff]) else 0.0


def reciprocal_rank(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    ranked = enumerate(evidence.grades[:cutoff], 1)
    return next((1 / rank for rank, grade in ranked if grade > 0), 0.0)


def hard_negative_recall(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    negatives = sum(category == "hard_negative" for category in evidence.categories[:cutoff])
    total = evidence.totals.get("hard_negative", 0)
    return negatives / total if total else 0.0


LINEAR_GAIN = {"gold": 3, "near_match": 1}
RELEVANT = frozenset({"gold", "near_match"})
GOLD = frozenset({"gold"})
BAD = frozenset({"hard_negative"})


def first_rank(
    evidence: RetrievalEvidence, cutoff: int | None, wanted: frozenset[str]
) -> int | None:
    ranked = enumerate(evidence.categories[:cutoff], 1)
    return next((rank for rank, category in ranked if category in wanted), None)


def over_linear_ideal(
    gains: Sequence[int], gold_total: int, near_total: int, cutoff: int | None
) -> float:
    limit = gold_total + near_total if cutoff is None else cutoff
    golds = min(gold_total, limit)
    near = max(0, min(near_total, limit - golds))
    ideal = [3] * golds + [1] * near
    return dcg(gains) / dcg(ideal) if ideal else 0.0


def linear_ndcg(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    gains = [LINEAR_GAIN.get(category or "", 0) for category in evidence.categories[:cutoff]]
    gold_total = evidence.totals.get("gold", 0)
    return over_linear_ideal(gains, gold_total, evidence.totals.get("near_match", 0), cutoff)


def linear_ndcg_once(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    """linear_ndcg with a gold and its copies counted once (ADR 0057)."""
    ranked = list(zip(evidence.categories[:cutoff], evidence.golds[:cutoff], strict=True))
    present = {gold for category, gold in ranked if category == "gold"}
    credited: set[str] = set()
    gains: list[int] = []
    for category, gold in ranked:
        if category != "near_match" or gold is None:
            gains.append(LINEAR_GAIN.get(category or "", 0))
            continue
        gains.append(0 if gold in present or gold in credited else LINEAR_GAIN["near_match"])
        credited.add(gold)
    near_total = evidence.totals.get("near_match", 0) - evidence.copies
    return over_linear_ideal(gains, evidence.totals.get("gold", 0), near_total, cutoff)


def relevant_mrr(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    rank = first_rank(evidence, cutoff, RELEVANT)
    return 1.0 / rank if rank else 0.0


def canonical_mrr(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    rank = first_rank(evidence, cutoff, GOLD)
    return 1.0 / rank if rank else 0.0


def gold_within(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    return 1.0 if first_rank(evidence, cutoff, GOLD) else 0.0


def gold_recall(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    found = sum(category == "gold" for category in evidence.categories[:cutoff])
    total = evidence.totals.get("gold", 0)
    return found / total if total else 0.0


def bad(evidence: RetrievalEvidence, cutoff: int | None) -> float:
    return 1.0 if first_rank(evidence, cutoff, BAD) else 0.0


SCORE_FUNCTIONS: dict[str, ScoreFunction[RetrievalEvidence]] = {
    "ndcg": ndcg,
    "recall": recall,
    "hit": hit,
    "reciprocal_rank": reciprocal_rank,
    "hard_negative_recall": hard_negative_recall,
    "linear_ndcg": linear_ndcg,
    "linear_ndcg_once": linear_ndcg_once,
    "relevant_mrr": relevant_mrr,
    "canonical_mrr": canonical_mrr,
    "gold_within": gold_within,
    "gold_recall": gold_recall,
    "bad": bad,
}
# Version kept from the gov-index pack so recorded runs keep comparing:
# https://github.com/firecrawl/eval-packs/blob/5f044dd/packs/gov-index/scores/gov/metrics.py
GRADED_VERSION = "gov-pr41"


QUALITY_METRICS = {
    "ndcg@10": MetricDefinition("ndcg@10", "ndcg@10", cutoff=10, version="2"),
    "recall@10": MetricDefinition("recall@10", "recall@10", cutoff=10, version="2"),
    "recall@100": MetricDefinition("recall@100", "recall@100", cutoff=100, version="2"),
    "hit@1": MetricDefinition("hit@1", "hit@1", cutoff=1, version="2"),
    "hit@10": MetricDefinition("hit@10", "hit@10", cutoff=10, version="2"),
    "mrr": MetricDefinition("mrr", "reciprocal_rank", version="2"),
    "hard_negative_recall": MetricDefinition(
        "hard_negative_recall", "hard_negative_recall", direction="minimize", version="2"
    ),
}

GRADED_METRICS = {
    "linear_ndcg@10": MetricDefinition(
        "linear_ndcg@10", "linear_ndcg@10", cutoff=10, version=GRADED_VERSION
    ),
    "linear_ndcg_once@10": MetricDefinition(
        "linear_ndcg_once@10", "linear_ndcg_once@10", cutoff=10, version="1"
    ),
    "relevant_mrr@10": MetricDefinition(
        "relevant_mrr@10", "relevant_mrr@10", cutoff=10, version=GRADED_VERSION, gate={"min": 0.5}
    ),
    "canonical_mrr@10": MetricDefinition(
        "canonical_mrr@10", "canonical_mrr@10", cutoff=10, version=GRADED_VERSION
    ),
    "canonical@1": MetricDefinition(
        "canonical@1", "gold_within@1", cutoff=1, version=GRADED_VERSION, gate={"min": 0.25}
    ),
    "success@3": MetricDefinition("success@3", "gold_within@3", cutoff=3, version=GRADED_VERSION),
    "gold_recall@10": MetricDefinition(
        "gold_recall@10", "gold_recall@10", cutoff=10, version=GRADED_VERSION
    ),
    "bad@1": MetricDefinition(
        "bad@1",
        "bad@1",
        aggregation="sum",
        unit="queries",
        direction="minimize",
        cutoff=1,
        version=GRADED_VERSION,
        gate={"max": 0},
    ),
}

TABLE: ScoreTable[RetrievalEvidence] = ScoreTable(
    SCORE_FUNCTIONS, {**QUALITY_METRICS, **GRADED_METRICS}
)
