"""Answer verification: keep every report section consistent with its evidence.

The synthesizer is an LLM; this module is the deterministic guard-rail that
catches claims the retrieved evidence cannot support and degrades them
*honestly* rather than making a report look successful:

  * a market-cap ranking is not asserted from values lacking a metric,
    currency/unit or as-of date;
  * an answer scoped to a market universe (e.g. FTSE) is not presented as
    verified from evidence that never mentions that universe;
  * figures in the answer that appear nowhere in the evidence are flagged.

Verification never *raises* confidence and never invents data. It only adds
limitations and lowers confidence / flips ``verified`` to ``false``.
"""

from __future__ import annotations

from agent.evidence import evidence_text
from utils.financial_validation import (
    answer_claims_ranking,
    assess_market_cap_evidence,
    conflicting_market_caps,
    is_market_cap_question,
    unmatched_figures,
)
from utils.query_scope import mentions_universe, requested_universe


def _add_limitation(answer, text: str, added: list[str]) -> None:
    if text not in answer.limitations:
        answer.limitations.append(text)
        added.append(text)


def verify_answer(answer, *, query: str, evidence: list[dict],
                  max_results: int | None = None, plan=None) -> list[str]:
    """Reconcile an answer with its evidence; returns limitations added.

    Mutates ``answer`` in place (limitations, confidence, verified) and returns
    the list of newly-added limitations so callers can log/warn.
    """
    added: list[str] = []
    evidence = evidence or []

    # --- market-capitalisation claims / rankings -------------------------
    if is_market_cap_question(query):
        # Title and body are separated by a newline so a headline never
        # bleeds into the company name attached to a figure.
        assessment = assess_market_cap_evidence(
            evidence,
            text_getter=lambda e: f"{e.get('title', '')}\n{e.get('text', '')}",
            query=query)
        conflicts = conflicting_market_caps(assessment.values)
        if conflicts:
            _add_limitation(
                answer,
                "Sources give conflicting market-cap figures for: "
                + ", ".join(sorted(conflicts))
                + ". The figures were not reconciled.",
                added,
            )
            answer.confidence = min(answer.confidence, 0.5)
        if not assessment.usable:
            _add_limitation(
                answer,
                "Market-cap values in the retrieved evidence lack an explicit "
                "metric, currency/unit, or as-of date, so no market-cap ranking "
                "could be verified.",
                added,
            )
            if assessment.ranking_requested or answer_claims_ranking(answer.answer):
                answer.verified = False
                answer.confidence = 0.0
            else:
                answer.confidence = min(answer.confidence, 0.3)

    # --- requested market universe ---------------------------------------
    universe = requested_universe(query)
    if universe and evidence:
        matching = [e for e in evidence
                    if mentions_universe(evidence_text(e), universe)]
        if not matching:
            _add_limitation(
                answer,
                f"The retrieved evidence does not clearly concern the requested "
                f"{universe.upper()} universe, so the answer may be incomplete "
                f"or off-universe.",
                added,
            )
            answer.confidence = min(answer.confidence, 0.4)

    # --- figures unsupported by any evidence ------------------------------
    evidence_texts = [evidence_text(e) for e in evidence]
    missing = unmatched_figures(answer.answer, evidence_texts)
    if len(missing) >= 2:
        _add_limitation(
            answer,
            "Some financial figures in the answer could not be matched to the "
            "retrieved evidence and should be treated as unverified.",
            added,
        )
        answer.confidence = min(answer.confidence, 0.4)

    if not answer.verified:
        answer.confidence = 0.0
    return added


def filter_findings_for_universe(findings: list, query: str,
                                 *, min_relevance: float = 0.0) -> list:
    """Drop off-universe / low-relevance findings for scoped questions.

    Applied to ranked findings so a FTSE question never lists unrelated
    global/US rankings as if they answered it.
    """
    universe = requested_universe(query)
    out: list = []
    for f in findings:
        if float(getattr(f, "relevance", 0.0) or 0.0) < min_relevance:
            continue
        if universe:
            text = (f"{getattr(f, 'title', '')} "
                    f"{getattr(f, 'key_finding', '')} "
                    f"{getattr(f, 'significance', '')} "
                    f"{getattr(f, 'evidence_excerpt', '')}")
            if not mentions_universe(text, universe):
                continue
        out.append(f)
    return out
