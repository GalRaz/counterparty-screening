"""Layer D: open registry lookups for organisations. Officers found are proposed, never auto-screened."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from screening import config
from screening.record import add_coverage_gap
from screening.subjects import Subject


@dataclass
class LayerDResult:
    check: dict
    layer_d: dict | None = None
    proposed_subjects: list[dict] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)

    def apply(self, record: dict) -> None:
        record["checks_run"].append(self.check)
        if self.layer_d is not None:
            kept = (record.get("layer_d") or {}).get("manual_findings") or []
            record["layer_d"] = {**self.layer_d,
                                 "manual_findings": list(kept) + list(self.layer_d.get("manual_findings") or [])}
        for p in self.proposed_subjects:
            if p not in record["proposed_subjects"]:
                record["proposed_subjects"].append(p)
        for g in self.coverage_gaps:
            add_coverage_gap(record, g)


def manual_finding(*, registry: str, url: str, retrieved: str, fields: dict, note: str | None = None) -> dict:
    return {"registry": registry, "url": url, "retrieved": retrieved, "fields": fields, "note": note}


def _gleif_normalise(item: dict) -> dict:
    a = item["attributes"]
    e = a.get("entity", {})
    addr = e.get("legalAddress", {})
    return {
        "lei": a.get("lei"),
        "legal_name": (e.get("legalName") or {}).get("name"),
        "status": e.get("status"),
        "jurisdiction": e.get("jurisdiction"),
        "registered_as": e.get("registeredAs"),
        "address": {"lines": list(addr.get("addressLines", [])), "city": addr.get("city"), "country": addr.get("country")},
        "registration_status": (a.get("registration") or {}).get("status"),
    }


def gleif_lookup(subject: Subject, client: httpx.Client) -> list[dict]:
    if lei := subject.identifier("lei"):
        params = {"filter[lei]": lei}
    else:
        params = {"filter[entity.legalName]": subject.name, "page[size]": "10"}
    r = client.get("/lei-records", params=params, timeout=config.DEFAULT_TIMEOUT)
    r.raise_for_status()
    return [_gleif_normalise(i) for i in r.json().get("data", [])]


def _ch_auth(api_key: str) -> httpx.BasicAuth:
    return httpx.BasicAuth(api_key, "")


def companies_house_lookup(subject: Subject, client: httpx.Client, api_key: str) -> dict | None:
    auth = _ch_auth(api_key)
    number = subject.identifier("uk_company_number")
    if not number:
        r = client.get("/search/companies", params={"q": subject.name}, auth=auth, timeout=config.DEFAULT_TIMEOUT)
        r.raise_for_status()
        want = subject.name.casefold()
        hits = [i for i in r.json().get("items", []) if i.get("title", "").casefold() == want]
        if not hits:
            return None
        number = hits[0]["company_number"]
    p = client.get(f"/company/{number}", auth=auth, timeout=config.DEFAULT_TIMEOUT)
    p.raise_for_status()
    prof = p.json()
    o = client.get(f"/company/{number}/officers", auth=auth, timeout=config.DEFAULT_TIMEOUT)
    o.raise_for_status()
    officers = [{
        "name": i.get("name"), "role": i.get("officer_role"), "appointed_on": i.get("appointed_on"),
        "resigned_on": i.get("resigned_on"), "nationality": i.get("nationality"),
        "country_of_residence": i.get("country_of_residence"),
    } for i in o.json().get("items", [])]
    acc = prof.get("accounts") or {}
    return {
        "company_number": prof.get("company_number"), "company_name": prof.get("company_name"),
        "status": prof.get("company_status"), "incorporated": prof.get("date_of_creation"),
        "type": prof.get("type"), "sic_codes": list(prof.get("sic_codes", [])),
        "accounts": {"next_due": acc.get("next_due"), "overdue": acc.get("overdue")},
        "registered_office": prof.get("registered_office_address"),
        "officers": officers,
    }


def _officer_display_name(raw: str) -> str:
    """Companies House gives 'SURNAME, Given Names'. Return 'Given Names Surname'."""
    if "," in raw:
        sur, given = [p.strip() for p in raw.split(",", 1)]
        return f"{given} {sur.title()}"
    return raw


def run_layer_d(subject: Subject, *, gleif_client: httpx.Client | None, ch_client: httpx.Client | None,
                ch_api_key: str | None, now: str) -> LayerDResult:
    check = {"layer": "D", "provider": "registries", "sources": [], "status": "ok", "error": None, "timestamp": now}
    res = LayerDResult(check=check)
    if subject.type != "organization":
        check["status"] = "not_run"
        return res

    layer_d: dict = {"gleif": [], "companies_house": None, "manual_findings": []}
    attempted = 0
    succeeded = 0
    errors: list[str] = []

    if gleif_client is not None:
        attempted += 1
        check["sources"].append("gleif")
        try:
            layer_d["gleif"] = gleif_lookup(subject, gleif_client)
            succeeded += 1
        except (httpx.HTTPError, KeyError, ValueError) as e:
            errors.append(f"gleif: {type(e).__name__}")
            res.coverage_gaps.append(f"Layer D GLEIF lookup failed: {type(e).__name__}")

    is_gb = (subject.jurisdiction or "").upper() in ("GB", "UK")
    if is_gb and ch_client is not None and ch_api_key:
        attempted += 1
        check["sources"].append("companies_house")
        try:
            ch = companies_house_lookup(subject, ch_client, ch_api_key)
            layer_d["companies_house"] = ch
            succeeded += 1
            if ch:
                for off in ch["officers"]:
                    if off.get("resigned_on"):
                        continue
                    res.proposed_subjects.append({
                        "name": _officer_display_name(off["name"]), "type": "person",
                        "reason": f"active {off.get('role') or 'officer'} of {subject.name}",
                        "source": f"Companies House officers list, company {ch['company_number']}",
                    })
        except (httpx.HTTPError, KeyError, ValueError) as e:
            errors.append(f"companies_house: {type(e).__name__}")
            res.coverage_gaps.append(f"Layer D Companies House lookup failed: {type(e).__name__}")
    elif is_gb and not ch_api_key:
        # Only a gap where Companies House was in scope: a non-GB org was never going to be there.
        res.coverage_gaps.append("Layer D Companies House not queried: no API key configured")

    res.layer_d = layer_d
    if attempted and succeeded == 0:
        check["status"] = "failed"
        check["error"] = "; ".join(errors)
    return res
