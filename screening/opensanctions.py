"""Layer A: OpenSanctions /match. Returns candidates, never verdicts."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from screening import config
from screening.record import add_coverage_gap
from screening.subjects import Subject

TRANSLITERATION_GAP = "transliterated name — matcher precision reduced"
NO_DOB_GAP = "no DOB supplied"


@dataclass
class LayerAResult:
    check: dict
    candidates: list[dict] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)

    def apply(self, record: dict) -> None:
        record["checks_run"].append(self.check)
        record["watchlist_candidates"].extend(self.candidates)
        for g in self.coverage_gaps:
            add_coverage_gap(record, g)


def build_query(subject: Subject) -> dict:
    props: dict[str, list[str]] = {"name": subject.all_names()}
    if subject.type == "person":
        schema = subject.os_schema or "Person"
        first, _middle, last = subject.split_person_name()
        if first:
            props["firstName"] = [first]
            props["lastName"] = [last]
        if dob := subject.identifier("dob"):
            props["birthDate"] = [dob]
    else:
        schema = subject.os_schema or "Company"
        if reg := subject.identifier("registration_number"):
            props["registrationNumber"] = [reg]
        if tax := subject.identifier("tax_number"):
            props["taxNumber"] = [tax]
    if country := subject.identifier("country"):
        props["country"] = [country]
    return {"schema": schema, "properties": props}


def _candidate(result: dict) -> dict:
    props = result.get("properties", {})
    return {
        "source": "opensanctions",
        "id": result["id"],
        "caption": result.get("caption"),
        "schema": result.get("schema"),
        "score": result.get("score"),
        "topics": list(props.get("topics", [])),
        "datasets": list(result.get("datasets", [])),
        "entity": None,
        "assessment": "unreviewed",
        "proposed_disposition": None,
    }


def match(subject: Subject, client: httpx.Client, api_key: str, now: str) -> LayerAResult:
    check = {
        "layer": "A", "provider": "opensanctions", "dataset": config.OS_DATASET,
        "algorithm": config.OS_ALGORITHM, "threshold": config.OS_THRESHOLD, "limit": config.OS_LIMIT,
        "status": "ok", "error": None, "timestamp": now, "names_queried": subject.all_names(),
    }
    res = LayerAResult(check=check)
    if subject.type == "person" and not subject.identifier("dob"):
        res.coverage_gaps.append(NO_DOB_GAP)
    if subject.has_non_latin_name():
        res.coverage_gaps.append(TRANSLITERATION_GAP)

    headers = {"Authorization": f"ApiKey {api_key}"}
    params = {"algorithm": config.OS_ALGORITHM, "threshold": config.OS_THRESHOLD, "limit": config.OS_LIMIT}
    try:
        r = client.post(f"/match/{config.OS_DATASET}", params=params, headers=headers,
                        json={"queries": {"q1": build_query(subject)}}, timeout=config.DEFAULT_TIMEOUT)
        r.raise_for_status()
        results = r.json()["responses"]["q1"].get("results", [])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        check["status"] = "failed"
        check["error"] = f"{type(e).__name__}: {e}"
        res.coverage_gaps.append(f"Layer A (opensanctions) call failed: {type(e).__name__}")
        return res

    skipped_missing_id = False
    for result in results:
        if not result.get("id"):
            if not skipped_missing_id:
                res.coverage_gaps.append("Layer A returned a candidate without an id; skipped")
                skipped_missing_id = True
            continue
        cand = _candidate(result)
        try:
            e = client.get(f"/entities/{cand['id']}", headers=headers, timeout=config.DEFAULT_TIMEOUT)
            e.raise_for_status()
            cand["entity"] = e.json()
        except (httpx.HTTPError, ValueError):
            res.coverage_gaps.append(f"Layer A enrichment failed for candidate {cand['id']}")
        res.candidates.append(cand)
    return res
