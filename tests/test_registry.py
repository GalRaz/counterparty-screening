import base64

import httpx

from screening import registry as RG
from screening.config import COMPANIES_HOUSE_BASE, GLEIF_BASE
from screening.record import new_record
from screening.subjects import Identifier, Subject
from tests.conftest import NOW, json_response, load_fixture, mock_client


def ccc():
    return Subject(type="organization", name="Carbon Capital Corporation Pty Ltd", jurisdiction="AU",
                   identifiers=[Identifier("lei", "984500765B652F3C6A05", "GLEIF search by user")])


def jer():
    return Subject(type="organization", name="Jearrard Energy Resources Ltd", jurisdiction="GB")


def gleif_handler(req: httpx.Request):
    assert req.url.path == "/api/v1/lei-records"
    return json_response(200, load_fixture("gleif_records.json"))


def ch_handler(req: httpx.Request):
    auth = req.headers["Authorization"]
    assert auth == "Basic " + base64.b64encode(b"CHKEY:").decode()
    if req.url.path == "/search/companies":
        return json_response(200, load_fixture("ch_search.json"))
    if req.url.path == "/company/13647702":
        return json_response(200, load_fixture("ch_profile.json"))
    if req.url.path == "/company/13647702/officers":
        return json_response(200, load_fixture("ch_officers.json"))
    raise AssertionError(req.url)


def test_gleif_by_lei_filter_and_normalisation():
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        return gleif_handler(req)

    recs = RG.gleif_lookup(ccc(), mock_client(handler, GLEIF_BASE))
    assert seen["params"] == {"filter[lei]": "984500765B652F3C6A05"}
    assert recs == [{
        "lei": "984500765B652F3C6A05", "legal_name": "CARBON CAPITAL CORPORATION PTY LTD",
        "status": "ACTIVE", "jurisdiction": "AU", "registered_as": "667 478 471",
        "address": {"lines": ["LEVEL 12, 60 CARRINGTON STREET"], "city": "SYDNEY", "country": "AU"},
        "registration_status": "ISSUED"}]


def test_gleif_by_name_when_no_lei():
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        return gleif_handler(req)

    RG.gleif_lookup(jer(), mock_client(handler, GLEIF_BASE))
    assert seen["params"] == {"filter[entity.legalName]": "Jearrard Energy Resources Ltd", "page[size]": "10"}


def test_companies_house_search_requires_exact_title():
    rec = RG.companies_house_lookup(jer(), mock_client(ch_handler, COMPANIES_HOUSE_BASE), "CHKEY")
    assert rec["company_number"] == "13647702"
    assert rec["status"] == "active" and rec["incorporated"] == "2021-09-28"
    assert rec["accounts"] == {"next_due": "2026-06-30", "overdue": True}
    assert rec["sic_codes"] == ["71121"]
    assert [o["name"] for o in rec["officers"]] == ["ALLINGTON, Mark Laurence", "OBERHOLZER, Jan"]
    assert rec["officers"][1]["resigned_on"] == "2025-01-20"


def test_companies_house_no_exact_match_returns_none():
    s = Subject(type="organization", name="Jearrard Energy", jurisdiction="GB")
    assert RG.companies_house_lookup(s, mock_client(ch_handler, COMPANIES_HOUSE_BASE), "CHKEY") is None


def test_companies_house_uses_number_identifier_directly():
    s = Subject(type="organization", name="whatever", identifiers=[Identifier("uk_company_number", "13647702", "user")])
    paths = []

    def handler(req):
        paths.append(req.url.path)
        return ch_handler(req)

    RG.companies_house_lookup(s, mock_client(handler, COMPANIES_HOUSE_BASE), "CHKEY")
    assert "/search/companies" not in paths


def test_run_layer_d_proposes_active_officers_only():
    res = RG.run_layer_d(jer(), gleif_client=mock_client(lambda r: json_response(200, {"data": []}), GLEIF_BASE),
                         ch_client=mock_client(ch_handler, COMPANIES_HOUSE_BASE), ch_api_key="CHKEY", now=NOW)
    assert res.check["status"] == "ok"
    assert res.proposed_subjects == [{"name": "Mark Laurence Allington", "type": "person",
                                      "reason": "active director of Jearrard Energy Resources Ltd",
                                      "source": "Companies House officers list, company 13647702"}]
    rec = new_record(jer(), "E", "P", NOW)
    res.apply(rec)
    assert rec["layer_d"]["companies_house"]["company_number"] == "13647702"
    assert rec["proposed_subjects"][0]["name"] == "Mark Laurence Allington"


def test_run_layer_d_person_is_not_run():
    res = RG.run_layer_d(Subject(type="person", name="X"), gleif_client=None, ch_client=None, ch_api_key=None, now=NOW)
    assert res.check["status"] == "not_run" and res.layer_d is None


def test_run_layer_d_all_sources_failed():
    boom = mock_client(lambda r: json_response(500, {}), GLEIF_BASE)
    res = RG.run_layer_d(ccc(), gleif_client=boom, ch_client=None, ch_api_key=None, now=NOW)
    assert res.check["status"] == "failed"
    assert any("gleif" in g.lower() for g in res.coverage_gaps)
    assert any("companies house" in g.lower() and "no api key" in g.lower() for g in res.coverage_gaps)


def test_non_gb_org_skips_companies_house_without_gap():
    res = RG.run_layer_d(ccc(), gleif_client=mock_client(gleif_handler, GLEIF_BASE), ch_client=None, ch_api_key="CHKEY", now=NOW)
    assert res.check["status"] == "ok"
    assert res.layer_d["companies_house"] is None
    assert not any("companies house" in g.lower() for g in res.coverage_gaps)


def test_manual_finding_shape():
    f = RG.manual_finding(registry="ACRA", url="https://www.bizfile.gov.sg/x", retrieved=NOW,
                          fields={"status": "Live", "incorporated": "2023-04-01"}, note="looked up by agent")
    assert f == {"registry": "ACRA", "url": "https://www.bizfile.gov.sg/x", "retrieved": NOW,
                 "fields": {"status": "Live", "incorporated": "2023-04-01"}, "note": "looked up by agent"}
