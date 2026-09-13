"""Layer C: NameScan Sapphire. Every path here exists to make sure money is spent deliberately (§7)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from screening import config
from screening.record import add_coverage_gap
from screening.store import Store
from screening.subjects import Subject

HEADERS_CT = "application/json-patch+json"


class RunAborted(Exception):
    """Raised before any credit is spent."""


@dataclass
class LayerCResult:
    check: dict
    layer_c: dict | None = None
    matches: list[dict] = field(default_factory=list)
    media: list[dict] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def apply(self, record: dict) -> None:
        record["checks_run"].append(self.check)
        if self.layer_c is not None:
            record["layer_c"] = {**self.layer_c,
                                 "matches": [slim_match(m) for m in self.matches],
                                 "advanced_media_items": [slim_media_item(m) for m in self.media]}
        for g in self.coverage_gaps:
            add_coverage_gap(record, g)


def _official_lists(entity: dict) -> tuple[list[str], bool | None]:
    lists = entity.get("officialLists") or []
    keywords = [l.get("keyword") for l in lists if l.get("keyword")]
    flags = [l.get("isCurrent") for l in lists if l.get("isCurrent") is not None]
    return keywords, (any(flags) if flags else None)


def slim_match(match: dict) -> dict:
    """What the record keeps of a vendor match (§7.7). The full object stays in the vendor-text cache."""
    entity = match.get("person") or match.get("entity") or {}
    keywords, is_current = _official_lists(entity)
    return {
        "name": entity.get("name") or entity.get("primaryName"),
        "match_rate": match.get("matchRate"),
        "category": match.get("category"),
        "matched_fields": match.get("matchedFields"),
        "official_lists": keywords,
        "is_current": is_current,
    }


def slim_media_item(item: dict) -> dict:
    """Citation only — no vendor `summary` or `body` is copied into the record."""
    return {"title": item.get("title"), "source_name": item.get("sourceName"),
            "published": item.get("publishedDate"), "link": item.get("link")}


def _common(include_media: bool) -> dict:
    return {"exact": False, "matchRate": config.NS_MATCH_RATE,
            "maxResultCount": config.NS_MAX_RESULTS, "includeAdvancedMedia": include_media}


def build_person_body(subject: Subject, include_media: bool) -> dict:
    body: dict = {}
    if subject.name_is_non_latin():
        body["originalName"] = subject.name
    else:
        first, middle, last = subject.split_person_name()
        if first:
            body["firstName"] = first
            if middle:
                body["middleName"] = middle
            body["lastName"] = last
        else:
            body["originalName"] = subject.name
    # §7.5: only sourced identifiers. A wrong value silently suppresses true hits.
    for kind, key in (("dob", "dob"), ("country", "country"), ("gender", "gender"),
                      ("national_id", "idNumber"), ("passport", "idNumber")):
        if (v := subject.identifier(kind)) and key not in body:
            body[key] = v
    body.update(_common(include_media))
    return body


def build_org_body(subject: Subject, include_media: bool) -> dict:
    body: dict = {"name": subject.name}
    if country := subject.identifier("country"):
        body["country"] = country
    if reg := subject.identifier("registration_number"):
        body["registrationNumber"] = reg
    body.update(_common(include_media))
    return body


class NameScanClient:
    def __init__(self, client: httpx.Client, api_key: str, *, test_mode: bool = False, sleep=time.sleep):
        self.client = client
        self.api_key = api_key
        self.test_mode = test_mode
        self.sleep = sleep

    def _headers(self) -> dict:
        return {"api-key": self.api_key, "Content-Type": HEADERS_CT, "Accept": "application/json"}

    def credits(self) -> float:
        r = self.client.get("/credits/sapphire", headers=self._headers(), timeout=config.DEFAULT_TIMEOUT)
        r.raise_for_status()
        return float(r.json()["balance"])

    def _path(self, subject: Subject) -> str:
        return "/person-scans/sapphire" if subject.type == "person" else "/organisation-scans/sapphire"

    def _post_with_retries(self, path: str, body: dict, timeout: float) -> tuple[dict | None, int, str | None]:
        attempts = 0
        err: str | None = None
        while attempts <= config.NS_MAX_RETRIES:
            attempts += 1
            try:
                r = self.client.post(path, headers=self._headers(), content=json.dumps(body), timeout=timeout)
                if 400 <= r.status_code < 500:
                    return None, attempts, f"HTTP {r.status_code}"
                r.raise_for_status()
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}: {e}"
                if attempts <= config.NS_MAX_RETRIES:
                    self.sleep(2 ** attempts)
                continue
            # A 2xx with an unparseable body is a vendor irregularity, not a transient
            # failure — retrying would spend more than pre-flight counted (§7).
            try:
                return r.json(), attempts, None
            except ValueError:
                return None, attempts, "unparseable response body"
        return None, attempts, err

    def scan(self, subject: Subject, *, include_media: bool, now: str, store: Store | None) -> LayerCResult:
        check = {"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "ok",
                 "error": None, "timestamp": now, "attempts": 0}
        res = LayerCResult(check=check)
        path = self._path(subject)
        key = subject.normalised_key()
        media_requested = include_media and not self.test_mode
        if include_media and self.test_mode:
            res.warnings.append("test key does not support adverse media; includeAdvancedMedia forced false")

        reused = False
        data = None
        if store is not None and not self.test_mode:
            if prior := store.find_scan(key, config.DEDUP_DAYS, now):
                try:
                    r = self.client.get(f"{path}/{prior}", headers=self._headers(), timeout=config.DEFAULT_TIMEOUT)
                    r.raise_for_status()
                    data, reused = r.json(), True
                    check["attempts"] = 1
                except (httpx.HTTPError, ValueError) as e:
                    # The pre-flight budget counted this as a dedup hit (free). We must not
                    # fall back to a paid scan just because the free re-fetch failed (§7).
                    check["status"] = "failed"
                    check["attempts"] = 1
                    check["error"] = f"prior scan {prior} could not be re-fetched: {type(e).__name__}"
                    res.coverage_gaps.append(
                        f"Layer C (namescan) prior scan {prior} could not be re-fetched; "
                        "no new scan was run to stay within the pre-flight budget"
                    )
                    return res

        if data is None:
            body = (build_person_body if subject.type == "person" else build_org_body)(subject, media_requested)
            timeout = config.NS_TIMEOUT_MEDIA if media_requested else config.DEFAULT_TIMEOUT
            data, attempts, err = self._post_with_retries(path, body, timeout)
            check["attempts"] = attempts
            if data is None:
                check["status"] = "failed"
                check["error"] = err
                res.coverage_gaps.append(f"Layer C (namescan) call failed after {attempts} attempt(s): {err}")
                return res
            if subject.aliases:
                # Sapphire scans one name per call; alias variants would each cost a credit (§7.2).
                res.coverage_gaps.append("Layer C scanned the primary name only; alias variant(s) not sent: "
                                         + ", ".join(subject.aliases))

        scan_id = data.get("scanId")
        if media_requested and not reused:
            if "advancedMedia" in data and data["advancedMedia"] is not None:
                adverse_media, cost = "ok", config.NS_COST_WITH_MEDIA
            else:
                adverse_media, cost = "failed", config.NS_COST_NO_MEDIA
                res.coverage_gaps.append("Layer C adverse media check did not run (advancedMedia absent) — Layer B must run in full mode")
        elif reused:
            if data.get("advancedMedia") is not None:
                adverse_media = "ok"
            elif include_media:
                adverse_media = "failed"
                res.coverage_gaps.append("Layer C adverse media check did not run (advancedMedia absent) — Layer B must run in full mode")
            else:
                adverse_media = "not_requested"
            cost = 0.0
        else:
            adverse_media, cost = "not_requested", (0.0 if self.test_mode else config.NS_COST_NO_MEDIA)
        if self.test_mode:
            cost = 0.0

        raw_matches = data.get("persons") if subject.type == "person" else data.get("corporates")
        res.matches = list(raw_matches or [])
        res.media = list(data.get("advancedMedia") or [])
        res.layer_c = {
            "provider": "namescan", "tier": "sapphire", "scan_id": scan_id,
            "match_rate_floor": config.NS_MATCH_RATE, "number_of_matches": data.get("numberOfMatches"),
            "adverse_media": adverse_media, "credits_consumed": cost, "reused_prior_scan": reused,
            "test_mode": self.test_mode, "trigger": "named_subject",
            "authorised_by": None, "authorised_at": None,
            "tax_haven_country_results": data.get("taxHavenCountryResults", []),
            "sanctioned_country_results": data.get("sanctionedCountryResults", []),
            "scan_date": data.get("date"),
        }
        if store is not None and scan_id and not self.test_mode:
            if not reused:
                store.record_scan(key, scan_id, "namescan", subject.type, now)
            store.cache_vendor_text(scan_id, json.dumps(data, ensure_ascii=False), now)
        return res


def run_layer_c(subjects: list[Subject], nc: NameScanClient, store: Store, *, now: str,
                include_media: bool = True, max_subjects: int) -> list[LayerCResult]:
    if len(subjects) > max_subjects:
        raise RunAborted(f"ceiling: {len(subjects)} subjects exceeds NAMESCAN_MAX_SUBJECTS_RUN={max_subjects}")
    warnings: list[str] = []
    if not nc.test_mode:
        try:
            balance = nc.credits()
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            raise RunAborted(f"could not read credit balance: {type(e).__name__}: {e}") from e
        unit = config.NS_COST_WITH_MEDIA if include_media else config.NS_COST_NO_MEDIA
        to_scan = [s for s in subjects if store.find_scan(s.normalised_key(), config.DEDUP_DAYS, now) is None]
        cost = unit * len(to_scan)
        if balance < cost:
            raise RunAborted(f"insufficient credits: balance {balance} < run cost {cost} for {len(to_scan)} new scan(s)")
        if balance < config.NS_LOW_CREDIT_WARN:
            warnings.append(f"credit balance {balance} is below 20 — top up soon")
    results = []
    for s in subjects:
        r = nc.scan(s, include_media=include_media, now=now, store=store)
        r.warnings.extend(warnings)
        results.append(r)
    return results
