"""Select up to six original passages from a scoped, reranked candidate pool."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from packages.agent_runtime.context_budget import ApproxTokenEstimator
from packages.knowledge.contracts import MAX_SELECTION_CANDIDATES, RetrievedEvidence
from packages.knowledge.evidence_records import locator_span

MAX_JOINT_ITEMS = 6
_WORDS = re.compile(r"[^\W_]+")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+")
_STOP = frozenset(
    "a an the and or but of at by for in on to with from under into over after before "
    "is are was were be been being do does did have has had as than that this these those "
    "he her his she it its their they them who whom whose what which where when how why "
    "would can could should will may also then there only most other such both all much "
    "about among through between during following including according however".split()
)


@dataclass
class _Passage:
    evidence: RetrievedEvidence
    terms: set[str]
    names: set[str]
    lead: set[str]
    shingles: set[tuple[str, ...]]
    relevance: float
    body_tokens: int
    prompt_tokens: int
    question_mask: int = 0


def _terms(text: str) -> tuple[set[str], set[str], set[str], set[tuple[str, ...]]]:
    # Use the original case for names; case folding is only for matching. CJK
    # bigrams allow lexical overlap without assuming whitespace segmentation.
    text = _CJK.sub(r" \g<0> ", unicodedata.normalize("NFKC", text))
    words = []
    for word in _WORDS.findall(text):
        if _CJK.fullmatch(word) and len(word) > 1:
            words.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            words.append(word)
    tokens = [word.casefold() for word in words]
    terms, names, lead = set(), set(), set()
    for index, (word, term) in enumerate(zip(words, tokens, strict=True)):
        if len(term) < 2 or term in _STOP or term.isdecimal():
            continue
        is_name = word[0].isupper() or _CJK.fullmatch(term) or any(c.isdigit() for c in term)
        terms.add(term)
        if is_name:
            names.add(term)
        if index < 16:
            lead.add(term)
        if index and len(tokens[index - 1]) > 1 and tokens[index - 1] not in _STOP:
            phrase = tokens[index - 1] + " " + term
            terms.add(phrase)
            if is_name and words[index - 1][0].isupper():
                names.add(phrase)
            if index < 16:
                lead.add(phrase)
    width = min(3, len(tokens))
    shingles = {tuple(tokens[i : i + width]) for i in range(len(tokens) - width + 1)}
    return terms, names, lead, shingles


def _pair_scores(
    a: _Passage, b: _Passage, query_terms: set[str], frequencies: Counter[str], source_count: int
) -> tuple[float, float, float]:
    """Return bridge, contiguous-source and redundancy weights for two real chunks."""
    overlap = len(a.shingles & b.shingles) / min(len(a.shingles), len(b.shingles))
    same_revision = a.evidence.document_revision_id == b.evidence.document_revision_id
    continuity = 0.0
    if same_revision:
        left, right = locator_span(a.evidence.locator), locator_span(b.evidence.locator)
        # Page, layout and paragraph-relative coordinates can restart in one revision.
        scope = ("type", "page", "paragraph_ordinal", "block_index", "part_index", "row")
        if (
            left
            and right
            and all(a.evidence.locator.get(k) == b.evidence.locator.get(k) for k in scope)
        ):
            shared = min(left[1], right[1]) - max(left[0], right[0])
            if shared >= 0:
                fraction = shared / min(left[1] - left[0], right[1] - right[0])
                overlap = max(overlap, fraction)
                # Adjacent pieces must contribute original text. An unrelated
                # block in the same document does not earn this bonus.
                continuity = 0.65 * (1 - fraction) * max(a.relevance, b.relevance)
    continuity *= 1 - overlap
    redundancy = 1.55 * max(0.0, (overlap - 0.25) / 0.75) + 0.10 * same_revision
    if same_revision or not (a.question_mask or b.question_mask):
        return 0.0, continuity, redundancy

    # A bridge must be mentioned by both sources, identified as a possible
    # name in at least one, outside the question, and distinctive in this pool.
    mentions = ((a.names & b.terms) | (b.names & a.terms)) - query_terms
    bridge = 0.0
    for term in mentions:
        df = frequencies[term]
        if df > max(2, source_count // 3):
            continue
        specificity = 2 / df  # a cross-source mention always has df >= 2
        name_weight = 1.0 if " " in term else 0.70
        centrality = 1.0 if term in a.lead or term in b.lead else 0.62
        bridge = max(bridge, specificity * name_weight * centrality)
    new_facets = bool(a.question_mask & ~b.question_mask) + bool(b.question_mask & ~a.question_mask)
    bridge *= (0.46 + 0.27 * new_facets) * max(a.relevance, b.relevance) * (1 - overlap)
    return bridge, continuity, redundancy


def select_joint_evidence(
    query: str,
    candidates: Sequence[RetrievedEvidence],
    *,
    max_items: int = MAX_JOINT_ITEMS,
    max_tokens: int = 16_000,
    prompt_budget: int | None = None,
    prompt_costs: Mapping[str, int] | None = None,
) -> tuple[RetrievedEvidence, ...]:
    """Choose a subset jointly and present its unmodified members in BGE order.

    ``max_tokens`` charges whole passage bodies using the QA estimator. When
    supplied, the second budget charges each passage's model-facing serialization.
    Only the first 20 authorized, post-floor candidates participate.
    """
    if not 1 <= max_items <= MAX_JOINT_ITEMS:
        raise ValueError("joint selection requires one to six slots")
    if (prompt_budget is None) != (prompt_costs is None):
        raise ValueError("a prompt budget and its serialization costs must be provided together")
    pool = candidates[:MAX_SELECTION_CANDIDATES]
    scores = [p.rerank_score if p.rerank_score is not None else p.retrieval_score for p in pool]
    if not scores:
        return ()
    low, high = min(scores), max(scores)
    query_terms = _terms(query)[0]
    passages = []
    by_source: dict[str, set[str]] = {}
    seen_ids: set[str] = set()
    estimator = ApproxTokenEstimator()
    for rank, (evidence, score) in enumerate(zip(pool, scores, strict=True)):
        cost = estimator.estimate(evidence.text)
        wire = 0 if prompt_costs is None else prompt_costs[evidence.chunk_id]
        if (
            evidence.chunk_id in seen_ids
            or not evidence.text.strip()
            or cost > max_tokens
            or (prompt_budget is not None and wire > prompt_budget)
        ):
            continue
        seen_ids.add(evidence.chunk_id)
        terms, names, lead, shingles = _terms(evidence.text)
        rank_prior = 1 / (1 + 0.18 * rank)
        normalized_score = (score - low) / (high - low) if high > low else rank_prior
        relevance = 0.90 * rank_prior + 0.55 * normalized_score
        passages.append(_Passage(evidence, terms, names, lead, shingles, relevance, cost, wire))
        by_source.setdefault(evidence.document_revision_id, set()).update(terms)
    frequencies = Counter(term for source in by_source.values() for term in source)
    facets = sorted(term for term in query_terms if " " not in term and term in frequencies)
    for passage in passages:
        passage.question_mask = sum(
            1 << index for index, term in enumerate(facets) if term in passage.terms
        )
    pairs = [
        [_pair_scores(a, b, query_terms, frequencies, len(by_source)) for a in passages[:j]]
        for j, b in enumerate(passages)
    ]

    # There are at most 60,459 nonempty subsets for 20 choose up to six.
    # Charge each new facet once and only the strongest cross-source bridge:
    # a clique repeating a name cannot manufacture extra hops by voting for itself.
    best_score, best = 0.0, ()
    facet_weight = 0.85 / max(1, len(facets))

    def search(chosen, first, used, wire_used, covered, best_bridge, score):
        nonlocal best, best_score
        for j in range(first, len(passages)):
            item = passages[j]
            new_used, new_wire = used + item.body_tokens, wire_used + item.prompt_tokens
            if new_used > max_tokens or (prompt_budget is not None and new_wire > prompt_budget):
                continue
            bridge, continuation, redundancy = best_bridge, 0.0, 0.0
            for i in chosen:
                link, adjacent, duplicate = pairs[j][i]
                bridge = max(bridge, link)
                continuation = max(continuation, adjacent)
                redundancy += duplicate
            new_covered = covered | item.question_mask
            value = (
                score
                + item.relevance
                + (new_covered ^ covered).bit_count() * facet_weight
                + bridge
                - best_bridge
                + continuation
                - redundancy
            )
            subset = (*chosen, j)
            if value > best_score:
                best_score, best = value, subset
            if len(subset) < max_items:
                search(subset, j + 1, new_used, new_wire, new_covered, bridge, value)

    search((), 0, 0, 0, 0, 0.0, 0.0)
    return tuple(passages[i].evidence for i in best)
