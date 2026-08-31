"""Client-side scoring profile rerank.

Cosmos NoSQL FULLTEXTSCORE/RRF can only appear in ORDER BY RANK, so field weights and
freshness are applied here after candidates are returned. Semantics mirror Azure AI
Search scoring profiles (weights + freshness function + interpolation + aggregation).
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Iterable, Mapping

_INTERPOLATIONS = ("constant", "linear", "quadratic", "logarithmic")
_AGGREGATIONS = ("sum", "average", "minimum", "maximum")
_SUPPORTED_FUNCTION_TYPES = ("freshness",)

# Guardrail: bound score envelope to keep aggregation finite.
_MAX_FUNCTIONS_PER_PROFILE = 8
_ALLOWED_FUNCTION_FIELDS = {
    "freshness": frozenset({"sourceModifiedAt", "source_modified_at"}),
}
_WEIGHT_FIELD_MAP = {
    "content": "content",
    "source_name": "sourceName",
    "sourceName": "sourceName",
    "section_path": "sectionPath",
    "sectionPath": "sectionPath",
    "key_phrases": "keyPhrases",
    "keyPhrases": "keyPhrases",
}

# XSD dayTimeDuration subset accepted by AI Search boostingDuration.
_ISO8601_DURATION = re.compile(
    r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)


class ScoringProfileError(ValueError):
    """Raised for invalid scoring profile configuration or usage."""


@dataclass(frozen=True)
class FreshnessParameters:
    boosting_duration_seconds: float

    def __post_init__(self) -> None:
        value = self.boosting_duration_seconds
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ScoringProfileError("boostingDuration must be a finite positive number")
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise ScoringProfileError("boostingDuration must be a finite positive number")


@dataclass(frozen=True)
class ScoringFunction:
    type: str
    field_name: str
    boost: float
    interpolation: str
    freshness: FreshnessParameters | None = None

    def __post_init__(self) -> None:
        if self.type not in _SUPPORTED_FUNCTION_TYPES:
            raise ScoringProfileError(
                f"function type '{self.type}' is not supported in this phase"
            )
        if self.interpolation not in _INTERPOLATIONS:
            raise ScoringProfileError(f"interpolation '{self.interpolation}' is invalid")
        if not isinstance(self.field_name, str) or not self.field_name.strip():
            raise ScoringProfileError("function field_name is required")
        if isinstance(self.boost, bool) or not isinstance(self.boost, (int, float)):
            raise ScoringProfileError("function boost must be finite")
        if not math.isfinite(float(self.boost)):
            raise ScoringProfileError("function boost must be finite")
        if self.type == "freshness" and self.freshness is None:
            raise ScoringProfileError("freshness function requires 'freshness' parameters")
        if self.field_name not in _ALLOWED_FUNCTION_FIELDS[self.type]:
            raise ScoringProfileError(
                f"field '{self.field_name}' is not supported for {self.type} scoring"
            )


@dataclass(frozen=True)
class ScoringProfile:
    name: str
    text_weights: Mapping[str, float] = field(default_factory=dict)
    functions: tuple[ScoringFunction, ...] = ()
    function_aggregation: str = "sum"
    synonym_map: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ScoringProfileError("scoring profile name is required")
        if self.function_aggregation not in _AGGREGATIONS:
            raise ScoringProfileError(
                f"functionAggregation must be one of {_AGGREGATIONS}"
            )
        if not isinstance(self.functions, tuple):
            raise ScoringProfileError("functions must be an immutable tuple")
        if len(self.functions) > _MAX_FUNCTIONS_PER_PROFILE:
            raise ScoringProfileError(
                f"a profile may declare at most {_MAX_FUNCTIONS_PER_PROFILE} functions"
            )
        for function in self.functions:
            if not isinstance(function, ScoringFunction):
                raise ScoringProfileError("functions must contain ScoringFunction instances")
        if self.synonym_map is not None:
            if not isinstance(self.synonym_map, str) or not self.synonym_map.strip():
                raise ScoringProfileError("synonymMap must be a nonempty string when set")
        if not isinstance(self.text_weights, Mapping):
            raise ScoringProfileError("textWeights must be a mapping")
        canonical_seen: set[str] = set()
        for weight_field, weight in self.text_weights.items():
            if not isinstance(weight_field, str) or not weight_field.strip():
                raise ScoringProfileError("text weight field name is required")
            if weight_field not in _WEIGHT_FIELD_MAP:
                raise ScoringProfileError(f"text weight field '{weight_field}' is not supported")
            canonical = _WEIGHT_FIELD_MAP[weight_field]
            if canonical in canonical_seen:
                raise ScoringProfileError(
                    f"duplicate text weight for canonical field '{canonical}'"
                )
            canonical_seen.add(canonical)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ScoringProfileError("text weight must be a non-negative number")
            if not math.isfinite(float(weight)) or weight < 0:
                raise ScoringProfileError("text weight must be a non-negative number")
        # Overflow-safe conservative envelope on the maximum finite score.
        envelope = 1.0 + sum(float(w) for w in self.text_weights.values())
        envelope += sum(abs(float(f.boost)) for f in self.functions)
        if not math.isfinite(envelope) or envelope > sys.float_info.max:
            raise ScoringProfileError(
                "scoring profile envelope is not finite; reduce weights or boosts"
            )
        # Freeze the text_weights mapping so downstream code cannot mutate it.
        object.__setattr__(
            self, "text_weights", MappingProxyType(dict(self.text_weights))
        )


def parse_boosting_duration(value: str) -> float:
    """Parse an ISO-8601 XSD dayTimeDuration subset into seconds."""
    match = _ISO8601_DURATION.match(value)
    if not match or value == "P":
        raise ScoringProfileError(f"boostingDuration '{value}' is not a valid ISO 8601 duration")
    days, hours, minutes, seconds = match.groups()
    total = 0.0
    if days:
        total += int(days) * 86_400
    if hours:
        total += int(hours) * 3_600
    if minutes:
        total += int(minutes) * 60
    if seconds:
        total += float(seconds)
    if total <= 0:
        raise ScoringProfileError(f"boostingDuration '{value}' must be positive")
    return total


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ScoringProfileReranker:
    """Applies a scoring profile to an ordered list of candidate chunks.

    Consumes a sequence of candidate dicts already projected by SecureCosmosRetriever
    (BM25/RRF-ranked and ACL-trimmed). Returns them re-sorted with a numeric score
    embedded per candidate under the key '_scoring_profile_score'.
    """

    def __init__(
        self,
        profile: ScoringProfile,
        *,
        query_terms: Iterable[str] = (),
        now: datetime | None = None,
    ) -> None:
        if now is not None and (not isinstance(now, datetime) or now.tzinfo is None):
            raise ScoringProfileError("reranker 'now' must be timezone-aware")
        self._profile = profile
        self._query_terms = _normalize_query_terms(query_terms)
        self._now = now or datetime.now(timezone.utc)

    @property
    def profile(self) -> ScoringProfile:
        return self._profile

    def rerank(self, candidates: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
        scored: list[tuple[float, int, dict[str, object]]] = []
        for original_rank, candidate in enumerate(candidates):
            enriched = dict(candidate)
            score = self._score(enriched, original_rank)
            enriched["_scoring_profile_score"] = score
            scored.append((score, original_rank, enriched))
        scored.sort(key=lambda triple: (-triple[0], triple[1]))
        return [item for _score, _rank, item in scored]

    def _score(self, candidate: Mapping[str, object], original_rank: int) -> float:
        # Base score is 1/(rank+1) so the retriever ordering wins ties with weight=0.
        base_score = 1.0 / (original_rank + 1)
        weight_bonus = self._text_weight_score(candidate)
        function_bonus = self._function_score(candidate)
        total = base_score + weight_bonus + function_bonus
        if not math.isfinite(total):
            # Envelope guardrails in ScoringProfile.__post_init__ make this unreachable.
            raise ScoringProfileError("scoring produced a non-finite final score")
        return total

    def _text_weight_score(self, candidate: Mapping[str, object]) -> float:
        total = 0.0
        for weight_field, weight in self._profile.text_weights.items():
            if not weight:
                continue
            candidate_key = _WEIGHT_FIELD_MAP[weight_field]
            value = candidate.get(candidate_key)
            if _matches_query_term(value, self._query_terms):
                total += float(weight)
        return total

    def _function_score(self, candidate: Mapping[str, object]) -> float:
        contributions: list[float] = []
        for function in self._profile.functions:
            contribution = self._evaluate_function(function, candidate)
            if contribution is None:
                continue
            # Malformed candidate signals must contribute 0.0; the evaluators enforce
            # that so any non-finite contribution here is an invariant defect.
            if not math.isfinite(contribution):
                raise ScoringProfileError(
                    "scoring function produced a non-finite contribution"
                )
            contributions.append(contribution)
        if not contributions:
            return 0.0
        aggregation = self._profile.function_aggregation
        if aggregation == "sum":
            aggregate = math.fsum(contributions)
        elif aggregation == "average":
            aggregate = math.fsum(contributions) / float(len(self._profile.functions))
        elif aggregation == "minimum":
            aggregate = min(contributions)
        elif aggregation == "maximum":
            aggregate = max(contributions)
        else:
            # Constructor validates aggregation, so this branch is unreachable.
            raise ScoringProfileError(
                f"functionAggregation '{aggregation}' is not implemented"
            )
        if not math.isfinite(aggregate):
            raise ScoringProfileError("aggregated function score is not finite")
        return aggregate

    def _evaluate_function(
        self, function: ScoringFunction, candidate: Mapping[str, object]
    ) -> float | None:
        if function.type == "freshness":
            return self._evaluate_freshness(function, candidate)
        return None

    def _evaluate_freshness(
        self, function: ScoringFunction, candidate: Mapping[str, object]
    ) -> float | None:
        timestamp = _pick_freshness_timestamp(candidate, function.field_name)
        if timestamp is None:
            return 0.0
        try:
            candidate_time = _parse_utc(timestamp)
        except (TypeError, ValueError):
            return 0.0
        # Naive candidate timestamps are ambiguous; treat as no signal rather than
        # raising a TypeError when subtracting from the timezone-aware clock.
        if candidate_time.tzinfo is None:
            return 0.0
        if function.freshness is None:
            return 0.0
        elapsed = (self._now - candidate_time).total_seconds()
        if elapsed < 0:
            return 0.0
        window = function.freshness.boosting_duration_seconds
        return function.boost * _interpolate(elapsed, window, function.interpolation)

def _interpolate(elapsed_seconds: float, window_seconds: float, curve: str) -> float:
    if elapsed_seconds >= window_seconds:
        return 0.0
    if window_seconds <= 0:
        return 0.0
    fraction = elapsed_seconds / window_seconds
    if curve == "constant":
        return 1.0
    if curve == "linear":
        return 1.0 - fraction
    if curve == "quadratic":
        return 1.0 - fraction**2
    if curve == "logarithmic":
        # Convex curve that decays fast near 0 and slowly near the window end.
        return 1.0 - math.log1p(fraction * (math.e - 1))
    raise ScoringProfileError(f"interpolation '{curve}' is invalid")


def _pick_freshness_timestamp(
    candidate: Mapping[str, object], preferred_field: str
) -> str | None:
    value = candidate.get(_camelize(preferred_field))
    return value if isinstance(value, str) and value else None


def _normalize_query_terms(values: Iterable[str]) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        candidates = [value.strip(), *re.findall(r"[\w]+(?:['-][\w]+)*", value)]
        for candidate in candidates:
            normalized = candidate.casefold()
            if len(normalized) >= 2 and normalized not in seen:
                seen.add(normalized)
                terms.append(normalized)
    return tuple(terms)


def _matches_query_term(value: object, query_terms: tuple[str, ...]) -> bool:
    if not query_terms:
        return False
    if isinstance(value, str):
        casefolded = value.casefold()
        return any(
            re.search(rf"(?<!\w){re.escape(term)}(?!\w)", casefolded) is not None
            for term in query_terms
        )
    if isinstance(value, (list, tuple)):
        return any(_matches_query_term(item, query_terms) for item in value)
    return False


def _camelize(name: str) -> str:
    """Accept either snake_case or camelCase field names in profile configs."""
    if "_" not in name:
        return name
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)
