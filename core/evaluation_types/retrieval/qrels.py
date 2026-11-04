from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.contracts import Datapoint

Category = Literal["gold", "near_match", "hard_negative"]


class Qrel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    url: str = Field(min_length=1)
    grade: int = Field(ge=0, le=3)
    category: Category
    aliases: list[str] = Field(default_factory=list)
    doc_id: str | None = Field(default=None, min_length=1)
    copy_of: str | None = Field(default=None, min_length=1)
    provenance: dict[str, Any] | None = None

    @property
    def urls(self) -> tuple[str, ...]:
        """The document's URL and every alias: one qrel, several addresses."""
        return (self.url, *self.aliases)

    @property
    def identities(self) -> tuple[str, ...]:
        """Every way a hit can name this document: its addresses and, when known, the index's id."""
        return self.urls if self.doc_id is None else (*self.urls, f"id:{self.doc_id}")


class RetrievalDatapoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    qrels: list[Qrel]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_documents(self) -> "RetrievalDatapoint":
        urls = [identity for qrel in self.qrels for identity in qrel.identities]
        if len(urls) != len(set(urls)):
            raise ValueError("duplicate qrel document identity")
        return self

    @model_validator(mode="after")
    def copies_name_a_gold(self) -> "RetrievalDatapoint":
        golds = {qrel.url for qrel in self.qrels if qrel.category == "gold"}
        for qrel in self.qrels:
            if "copy_of" in (qrel.provenance or {}):
                raise ValueError(f"copy_of is a qrel field, not a provenance key: {qrel.url}")
            if qrel.copy_of is None:
                continue
            if qrel.category != "near_match":
                raise ValueError(f"only a near_match qrel can be a copy: {qrel.url}")
            if qrel.copy_of not in golds:
                raise ValueError(f"copy_of is not the url of a gold qrel on this row: {qrel.url}")
        return self


RetrievalReference = tuple[Qrel, ...]
CATEGORIES: tuple[str, ...] = get_args(Category)


def relevant_ids(reference: RetrievalReference | None) -> list[str]:
    return [q.doc_id for q in reference or () if q.doc_id is not None and q.grade > 0]


def validate_reference(row: dict[str, Any]) -> Datapoint[str, RetrievalReference]:
    parsed = RetrievalDatapoint.model_validate(row)
    return Datapoint(parsed.id, parsed.query, tuple(parsed.qrels), parsed.metadata)


def _short_url(url: str, limit: int = 72) -> str:
    bare = url.split("://", 1)[-1].removeprefix("www.").rstrip("/")
    return bare if len(bare) <= limit else bare[: limit - 1] + "…"


def describe_reference(reference: RetrievalReference | None) -> str:
    qrels = reference or ()
    gold = [_short_url(q.url) for q in qrels if q.category == "gold"]
    near = sum(1 for q in qrels if q.category == "near_match")
    traps = sum(1 for q in qrels if q.category == "hard_negative")
    parts = [gold[0] + (f" +{len(gold) - 1} gold" if len(gold) > 1 else "")] if gold else []
    if near:
        parts.append(f"{near} near match" + ("es" if near > 1 else ""))
    if traps:
        parts.append(f"{traps} trap" + ("s" if traps > 1 else ""))
    return " · ".join(parts) or "no reference"


_EXAMPLES: list[tuple[str, str, list[tuple[str, int, str]]]] = [
    (
        "q1",
        "renew a passport",
        [
            ("https://travel.state.gov/passports/renew", 3, "gold"),
            ("https://travel.state.gov/passports", 1, "near_match"),
        ],
    ),
    (
        "q2",
        "file taxes online for free",
        [
            ("https://www.irs.gov/filing/free-file", 3, "gold"),
            ("https://www.irs.gov/refunds", 0, "hard_negative"),
        ],
    ),
    ("q3", "replace a social security card", [("https://www.ssa.gov/number-card", 3, "gold")]),
]


def example_rows() -> list[dict[str, Any]]:
    return [
        {
            "id": row_id,
            "query": query,
            "qrels": [
                {"url": url, "grade": grade, "category": category} for url, grade, category in qrels
            ],
        }
        for row_id, query, qrels in _EXAMPLES
    ]
