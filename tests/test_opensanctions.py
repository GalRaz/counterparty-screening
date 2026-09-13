import json

import httpx

from screening import opensanctions as OS
from screening.config import OPENSANCTIONS_BASE
from screening.record import new_record
from screening.subjects import Identifier, Subject
from tests.conftest import NOW, json_response, load_fixture, mock_client


def person():
    return Subject(type="person", name="Aleksandr Zacharov", aliases=["Александр Захаров"],
                   identifiers=[Identifier("dob", "1965", "passport copy"),
                                Identifier("country", "RU", "user statement")])


def test_build_query_person_splits_name_and_uses_sourced_attributes():
    q = OS.build_query(person())
    assert q["schema"] == "Person"
    assert q["properties"]["name"] == ["Aleksandr Zacharov", "Александр Захаров"]
    assert q["properties"]["firstName"] == ["Aleksandr"]
    assert q["properties"]["lastName"] == ["Zacharov"]
    assert q["properties"]["birthDate"] == ["1965"]
    assert q["properties"]["country"] == ["RU"]


def test_build_query_org_uses_company_schema_and_registration():
    o = Subject(type="organization", name="Acme Pte Ltd",
                identifiers=[Identifier("registration_number", "202301234K", "ACRA extract")])
    q = OS.build_query(o)
    assert q["schema"] == "Company"
    assert q["properties"]["registrationNumber"] == ["202301234K"]
    assert "firstName" not in q["properties"]


def test_build_query_respects_schema_override():
    o = Subject(type="organization", name="Some Foundation", os_schema="Organization")
    assert OS.build_query(o)["schema"] == "Organization"


def test_match_ok_enriches_and_flags_transliteration():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path == "/match/default":
            assert req.headers["Authorization"] == "ApiKey KEY"
            assert dict(req.url.params) == {"algorithm": "best", "threshold": "0.7", "limit": "10"}
            body = json.loads(req.content)
            assert "q1" in body["queries"]
            return json_response(200, load_fixture("opensanctions_match.json"))
        if req.url.path.startswith("/entities/"):
            return json_response(200, load_fixture("opensanctions_entity.json"))
        raise AssertionError(req.url)

    res = OS.match(person(), mock_client(handler, OPENSANCTIONS_BASE), "KEY", NOW)
    assert res.check["status"] == "ok"
    assert res.check["layer"] == "A"
    assert [c["id"] for c in res.candidates] == ["Q12345", "Q99999"]
    assert res.candidates[0]["topics"] == ["sanction"]
    assert res.candidates[0]["entity"]["properties"]["familyPerson"]
    assert res.candidates[0]["assessment"] == "unreviewed"
    assert "transliterated name — matcher precision reduced" in res.coverage_gaps
    rec = new_record(person(), "E", "P", NOW)
    res.apply(rec)
    assert len(rec["watchlist_candidates"]) == 2 and rec["checks_run"][0]["provider"] == "opensanctions"


def test_match_http_error_is_failed_plus_gap():
    def handler(req):
        return json_response(500, {"detail": "boom"})

    res = OS.match(person(), mock_client(handler, OPENSANCTIONS_BASE), "KEY", NOW)
    assert res.check["status"] == "failed"
    assert res.candidates == []
    assert any("Layer A" in g and "failed" in g for g in res.coverage_gaps)


def test_match_transport_error_is_failed():
    def handler(req):
        raise httpx.ConnectError("no route")

    res = OS.match(person(), mock_client(handler, OPENSANCTIONS_BASE), "KEY", NOW)
    assert res.check["status"] == "failed" and "no route" in res.check["error"]


def test_enrichment_failure_keeps_candidate_and_adds_gap():
    def handler(req):
        if req.url.path == "/match/default":
            return json_response(200, load_fixture("opensanctions_match.json"))
        return json_response(503, {})

    res = OS.match(person(), mock_client(handler, OPENSANCTIONS_BASE), "KEY", NOW)
    assert res.check["status"] == "ok"
    assert res.candidates[0]["entity"] is None
    assert any("enrichment" in g for g in res.coverage_gaps)


def test_no_dob_is_a_coverage_gap():
    s = Subject(type="person", name="Jane Doe")
    res = OS.match(s, mock_client(lambda r: json_response(200, {"responses": {"q1": {"results": []}}}), OPENSANCTIONS_BASE), "KEY", NOW)
    assert "no DOB supplied" in res.coverage_gaps
