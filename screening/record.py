"""The §5 output record. Structural validation only; guardrail language checks live in lint.py."""
from __future__ import annotations

from screening.subjects import Subject

IDENTITY = ("confirmed_subject", "possible_subject", "different_person")
LEGAL_STATUS = (
    "allegation", "investigation_reported", "charged", "convicted", "acquitted_or_dismissed",
    "regulatory_action", "settlement_no_admission", "civil_claim", "insolvency", "commentary_only",
)
RETRIEVAL_STATUS = ("full", "snippet_only", "unavailable")
SOURCE_TYPE = ("primary", "wire", "aggregator", "low_accountability")
CHECK_STATUS = ("ok", "failed", "not_run")
LAYERS = ("A", "B", "C", "D")
ASSESSMENT = ("false_positive", "true_match", "unresolved")


def new_record(subject: Subject, engagement: str, commissioning_party: str, now: str) -> dict:
    return {
        "engagement": engagement,
        "commissioning_party": commissioning_party,
        "created_at": now,
        "subject": {
            "name": subject.name,
            "type": subject.type,
            "slug": subject.slug,
            "aliases": list(subject.aliases),
            "jurisdiction": subject.jurisdiction,
            "role": subject.role,
            "identifiers_supplied": [f"{i.kind}={i.value}" for i in subject.identifiers],
            "identifier_sources": [f"{i.kind}: {i.source}" for i in subject.identifiers],
        },
        "checks_run": [],
        "watchlist_candidates": [],
        "media_items": [],
        "layer_c": None,
        "layer_d": None,
        "proposed_subjects": [],
        "coverage_gaps": [],
        "human_review_required": True,
    }


def add_coverage_gap(record: dict, gap: str) -> None:
    if gap not in record["coverage_gaps"]:
        record["coverage_gaps"].append(gap)


def replace_layer(record: dict, layer: str) -> None:
    """Undo what a previous run of `layer` wrote, so re-running it replaces instead of duplicating.

    Coverage gaps naming the layer ("Layer C ...") describe that call's outcome and go with it.
    Gaps that are facts about the subject ("no DOB supplied", "transliterated name — matcher
    precision reduced") survive: a re-run does not make them untrue.
    """
    record["checks_run"] = [c for c in record["checks_run"] if c.get("layer") != layer]
    record["coverage_gaps"] = [g for g in record["coverage_gaps"] if not g.startswith(f"Layer {layer}")]
    if layer == "A":
        record["watchlist_candidates"] = []
    elif layer == "C":
        record["layer_c"] = None
    elif layer == "D":
        # Manual findings are the agent's own registry work, typed in by hand from a public search;
        # a re-run of the API lookups did not disprove them and must not silently discard them.
        kept = (record.get("layer_d") or {}).get("manual_findings") or []
        record["layer_d"] = {"gleif": [], "companies_house": None, "manual_findings": kept} if kept else None
        record["proposed_subjects"] = []


def new_media_item(*, title: str, publisher: str, published: str | None, url: str, retrieved: str,
                   retrieval_status: str, identity: str, corroborator: str | None,
                   legal_status: str, source_type: str, query: str | None = None,
                   language: str | None = None, summary: str | None = None,
                   proposed_disposition: str | None = None) -> dict:
    return {
        "title": title, "publisher": publisher, "published": published, "url": url,
        "retrieved": retrieved, "retrieval_status": retrieval_status, "identity": identity,
        "corroborator": corroborator, "legal_status": legal_status, "source_type": source_type,
        "query": query, "language": language, "summary": summary,
        "relevance": "unassessed", "proposed_disposition": proposed_disposition,
    }


def _validate_disposition(c: dict, path: str) -> list[str]:
    """§5: `assessment` stays `unreviewed` unless a human sets `dispositioned_by`."""
    by = c.get("dispositioned_by")
    if isinstance(by, str) and by.strip():
        if c.get("assessment") not in ASSESSMENT:
            return [f"{path}.assessment {c.get('assessment')!r} not in {ASSESSMENT} (dispositioned by {by!r})"]
        return []
    if c.get("assessment") != "unreviewed":
        return [f"{path}.assessment must stay 'unreviewed' (§5)"]
    return []


def validate(record: dict) -> list[str]:
    errs: list[str] = []
    if record.get("human_review_required") is not True:
        errs.append("human_review_required must be true")
    for i, c in enumerate(record.get("checks_run", [])):
        if c.get("status") not in CHECK_STATUS:
            errs.append(f"checks_run[{i}].status {c.get('status')!r} not in {CHECK_STATUS}")
        if c.get("layer") not in LAYERS:
            errs.append(f"checks_run[{i}].layer {c.get('layer')!r} not in {LAYERS}")
    for i, w in enumerate(record.get("watchlist_candidates", [])):
        errs += _validate_disposition(w, f"watchlist_candidates[{i}]")
    for i, m in enumerate((record.get("layer_c") or {}).get("matches") or []):
        errs += _validate_disposition(m, f"layer_c.matches[{i}]")
    for i, m in enumerate(record.get("media_items", [])):
        p = f"media_items[{i}]"
        if m.get("identity") not in IDENTITY:
            errs.append(f"{p}.identity {m.get('identity')!r} not in {IDENTITY}")
        if m.get("legal_status") not in LEGAL_STATUS:
            errs.append(f"{p}.legal_status {m.get('legal_status')!r} not in {LEGAL_STATUS}")
        if m.get("retrieval_status") not in RETRIEVAL_STATUS:
            errs.append(f"{p}.retrieval_status {m.get('retrieval_status')!r} not in {RETRIEVAL_STATUS}")
        if m.get("source_type") not in SOURCE_TYPE:
            errs.append(f"{p}.source_type {m.get('source_type')!r} not in {SOURCE_TYPE}")
        if m.get("relevance") != "unassessed":
            errs.append(f"{p}.relevance must stay 'unassessed' (§5)")
        if m.get("identity") == "confirmed_subject" and not m.get("corroborator"):
            errs.append(f"{p}: confirmed_subject requires a named corroborator (§3.2)")
    return errs
