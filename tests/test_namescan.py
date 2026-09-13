import json

import httpx
import pytest

from screening import namescan as NS
from screening.config import NAMESCAN_BASE
from screening.record import new_record
from screening.store import Store
from screening.subjects import Identifier, Subject
from tests.conftest import NOW, json_response, load_fixture, mock_client

LATER = "2026-10-01T10:00:00+00:00"


def person():
    return Subject(type="person", name="Mark Phillips", jurisdiction="AU",
                   identifiers=[Identifier("dob", "1970", "passport copy")])


def org():
    return Subject(type="organization", name="Green Bond Corporation", jurisdiction="LU",
                   identifiers=[Identifier("registration_number", "B123456", "RCS extract")])


def make_handler(person_body=None, org_body=None, credits=78.5, fail_times=0, status_on_fail=503):
    state = {"posts": 0, "fails_left": fail_times, "requests": []}

    def handler(req: httpx.Request):
        state["requests"].append(req)
        assert req.headers["api-key"] == "KEY"
        if req.url.path.endswith("/credits/sapphire"):
            return json_response(200, {**load_fixture("namescan_credits.json"), "balance": credits})
        if req.method == "POST":
            assert req.headers["content-type"] == "application/json-patch+json"
            if state["fails_left"] > 0:
                state["fails_left"] -= 1
                return json_response(status_on_fail, {"message": "degraded"})
            state["posts"] += 1
            if "person-scans" in req.url.path:
                return json_response(200, person_body or load_fixture("namescan_person.json"))
            return json_response(200, org_body or load_fixture("namescan_org.json"))
        if req.method == "GET" and "/sapphire/" in req.url.path:
            body = load_fixture("namescan_person.json") if "person" in req.url.path else load_fixture("namescan_org.json")
            return json_response(200, body)
        raise AssertionError(f"unexpected {req.method} {req.url}")

    return handler, state


def client_for(handler, **kw):
    return NS.NameScanClient(mock_client(handler, NAMESCAN_BASE), "KEY", sleep=lambda s: None, **kw)


def test_person_body_only_uses_sourced_fields():
    s = Subject(type="person", name="Mark Laurence Allington", jurisdiction="GB")
    body = NS.build_person_body(s, include_media=True)
    assert body == {"firstName": "Mark", "middleName": "Laurence", "lastName": "Allington",
                    "exact": False, "matchRate": 75, "maxResultCount": 100, "includeAdvancedMedia": True}
    assert "country" not in body  # jurisdiction is NOT a sourced identifier (§7.5)


def test_person_body_with_sourced_attributes_and_original_name():
    s = Subject(type="person", name="Александр Захаров",
                identifiers=[Identifier("dob", "1965", "passport copy"),
                             Identifier("country", "RU", "passport copy"),
                             Identifier("gender", "male", "passport copy")])
    body = NS.build_person_body(s, include_media=False)
    assert body["originalName"] == "Александр Захаров"
    assert "firstName" not in body
    assert body["dob"] == "1965" and body["country"] == "RU" and body["gender"] == "male"


def test_org_body():
    assert NS.build_org_body(org(), include_media=True) == {
        "name": "Green Bond Corporation", "registrationNumber": "B123456",
        "exact": False, "matchRate": 75, "maxResultCount": 100, "includeAdvancedMedia": True}


def test_scan_person_ok_persists_scan_and_media_ok(tmp_path):
    handler, state = make_handler()
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
        assert res.check["status"] == "ok"
        assert res.layer_c["scan_id"] == "ps-abc123"
        assert res.layer_c["adverse_media"] == "ok"
        assert res.layer_c["credits_consumed"] == 1.25
        assert res.layer_c["reused_prior_scan"] is False
        assert len(res.matches) == 1 and res.matches[0]["category"] == "PEP"
        assert res.media[0]["title"] == "Council fined over procurement"
        assert store.find_scan(person().normalised_key(), 90, NOW) == "ps-abc123"
        assert store.vendor_text("ps-abc123") is not None
        # request timeout must be >= 60s when media requested
        post = [r for r in state["requests"] if r.method == "POST"][0]
        assert post.extensions["timeout"]["read"] >= 60


def test_media_absent_means_failed_not_empty(tmp_path):
    body = {**load_fixture("namescan_person.json")}
    del body["advancedMedia"]
    handler, _ = make_handler(person_body=body)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.layer_c["adverse_media"] == "failed"
    assert res.layer_c["credits_consumed"] == 1.0
    assert any("adverse media" in g.lower() for g in res.coverage_gaps)


def test_media_present_but_empty_is_ok(tmp_path):
    body = {**load_fixture("namescan_person.json"), "advancedMedia": []}
    handler, _ = make_handler(person_body=body)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.layer_c["adverse_media"] == "ok" and res.media == []


def test_media_not_requested(tmp_path):
    handler, _ = make_handler(org_body=load_fixture("namescan_org.json"))
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(org(), include_media=False, now=NOW, store=store)
    assert res.layer_c["adverse_media"] == "not_requested"
    assert res.layer_c["credits_consumed"] == 1.0
    assert res.layer_c["tax_haven_country_results"] == [{"country": "Luxembourg"}]


def test_dedup_reuses_scan_within_90_days(tmp_path):
    handler, state = make_handler()
    with Store(tmp_path / "s.db") as store:
        nc = client_for(handler)
        nc.scan(person(), include_media=True, now=NOW, store=store)
        res = nc.scan(person(), include_media=True, now=LATER, store=store)
    assert state["posts"] == 1
    assert res.layer_c["reused_prior_scan"] is True
    assert res.layer_c["credits_consumed"] == 0.0
    assert res.layer_c["scan_id"] == "ps-abc123"


def test_retries_twice_then_succeeds(tmp_path):
    handler, _ = make_handler(fail_times=2)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.check["status"] == "ok" and res.check["attempts"] == 3


def test_gives_up_after_two_retries(tmp_path):
    handler, _ = make_handler(fail_times=3)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
        assert res.check["status"] == "failed" and res.check["attempts"] == 3
        assert res.layer_c is None
        assert any("Layer C" in g for g in res.coverage_gaps)
        assert store.find_scan(person().normalised_key(), 90, NOW) is None


def test_4xx_is_not_retried(tmp_path):
    handler, _ = make_handler(fail_times=1, status_on_fail=400)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.check["status"] == "failed" and res.check["attempts"] == 1


def test_dedup_refetch_failure_does_not_spend(tmp_path):
    handler, state = make_handler()
    with Store(tmp_path / "s.db") as store:
        store.record_scan(person().normalised_key(), "ps-abc123", "namescan", "person", NOW)

        def refetch_fails(req: httpx.Request):
            if req.method == "GET" and "/sapphire/" in req.url.path:
                return json_response(500, {"message": "degraded"})
            return handler(req)

        res = client_for(refetch_fails).scan(person(), include_media=True, now=LATER, store=store)
    assert state["posts"] == 0
    assert res.check["status"] == "failed"
    assert res.layer_c is None
    assert any("could not be re-fetched" in g for g in res.coverage_gaps)


def test_reused_scan_without_media_flags_failed_when_media_requested(tmp_path):
    no_media = {k: v for k, v in load_fixture("namescan_person.json").items() if k != "advancedMedia"}

    def handler(req: httpx.Request):
        if req.method == "GET" and "/sapphire/" in req.url.path:
            return json_response(200, no_media)
        raise AssertionError(f"unexpected {req.method} {req.url}")

    with Store(tmp_path / "s.db") as store:
        store.record_scan(person().normalised_key(), "ps-abc123", "namescan", "person", NOW)
        res = client_for(handler).scan(person(), include_media=True, now=LATER, store=store)
    assert res.layer_c["reused_prior_scan"] is True
    assert res.layer_c["adverse_media"] == "failed"
    assert res.layer_c["credits_consumed"] == 0.0
    assert any("adverse media" in g.lower() for g in res.coverage_gaps)


def test_reused_scan_without_media_is_not_requested_when_media_off(tmp_path):
    no_media = {k: v for k, v in load_fixture("namescan_person.json").items() if k != "advancedMedia"}

    def handler(req: httpx.Request):
        if req.method == "GET" and "/sapphire/" in req.url.path:
            return json_response(200, no_media)
        raise AssertionError(f"unexpected {req.method} {req.url}")

    with Store(tmp_path / "s.db") as store:
        store.record_scan(person().normalised_key(), "ps-abc123", "namescan", "person", NOW)
        res = client_for(handler).scan(person(), include_media=False, now=LATER, store=store)
    assert res.layer_c["reused_prior_scan"] is True
    assert res.layer_c["adverse_media"] == "not_requested"
    assert not any("adverse media" in g.lower() for g in res.coverage_gaps)


def test_unparseable_success_body_is_not_retried(tmp_path):
    state = {"posts": 0}

    def handler(req: httpx.Request):
        if req.method == "POST":
            state["posts"] += 1
            return httpx.Response(200, content=b"not json")
        raise AssertionError(f"unexpected {req.method} {req.url}")

    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.check["status"] == "failed"
    assert res.check["attempts"] == 1
    assert state["posts"] == 1
    assert res.layer_c is None


def test_apply_writes_layer_c_check_and_gaps(tmp_path):
    body = {**load_fixture("namescan_person.json")}
    del body["advancedMedia"]
    handler, _ = make_handler(person_body=body)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    rec = new_record(person(), "E", "P", NOW)
    res.apply(rec)
    assert rec["checks_run"][0]["layer"] == "C"
    assert rec["layer_c"]["scan_id"] == "ps-abc123"
    assert len(rec["layer_c"]["matches"]) == 1
    assert "advanced_media_items" in rec["layer_c"]
    assert any("adverse media" in g.lower() for g in rec["coverage_gaps"])


def test_test_mode_forces_media_off_and_skips_store(tmp_path):
    handler, state = make_handler(person_body={**load_fixture("namescan_person.json"), "advancedMedia": None})
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler, test_mode=True).scan(person(), include_media=True, now=NOW, store=store)
        assert json.loads([r for r in state["requests"] if r.method == "POST"][0].content)["includeAdvancedMedia"] is False
        assert res.layer_c["test_mode"] is True
        assert res.layer_c["adverse_media"] == "not_requested"
        assert res.layer_c["credits_consumed"] == 0.0
        assert store.find_scan(person().normalised_key(), 90, NOW) is None


def test_run_layer_c_preflight_aborts_when_credits_short(tmp_path):
    handler, state = make_handler(credits=2.0)
    with Store(tmp_path / "s.db") as store:
        with pytest.raises(NS.RunAborted, match="credits"):
            NS.run_layer_c([person(), org()], client_for(handler), store, now=NOW, max_subjects=20)
    assert state["posts"] == 0


def test_run_layer_c_ceiling_aborts(tmp_path):
    handler, state = make_handler()
    with Store(tmp_path / "s.db") as store:
        with pytest.raises(NS.RunAborted, match="ceiling"):
            NS.run_layer_c([person(), org()], client_for(handler), store, now=NOW, max_subjects=1)
    assert state["posts"] == 0


def test_run_layer_c_skips_credit_cost_for_dedup_hits(tmp_path):
    handler, state = make_handler(credits=1.3)
    with Store(tmp_path / "s.db") as store:
        store.record_scan(person().normalised_key(), "ps-abc123", "namescan", "person", NOW)
        results = NS.run_layer_c([person(), org()], client_for(handler), store, now=NOW, max_subjects=20)
    assert [r.layer_c["reused_prior_scan"] for r in results] == [True, False]
    assert state["posts"] == 1


def test_run_layer_c_low_credit_warning(tmp_path):
    handler, _ = make_handler(credits=15)
    with Store(tmp_path / "s.db") as store:
        results = NS.run_layer_c([org()], client_for(handler), store, now=NOW, max_subjects=20)
    assert any("below 20" in w for w in results[0].warnings)


def test_run_layer_c_credits_endpoint_failure_aborts(tmp_path):
    def handler(req):
        return json_response(500, {})
    with Store(tmp_path / "s.db") as store:
        with pytest.raises(NS.RunAborted, match="balance"):
            NS.run_layer_c([org()], client_for(handler), store, now=NOW, max_subjects=20)


def test_person_body_splits_latin_primary_name_even_with_non_latin_alias():
    s = Subject(type="person", name="Alexander Zakharov", aliases=["Александр Захаров"])
    body = NS.build_person_body(s, include_media=False)
    assert body["firstName"] == "Alexander" and body["lastName"] == "Zakharov"
    assert "originalName" not in body


def test_aliases_not_sent_are_recorded_as_a_coverage_gap(tmp_path):
    handler, _ = make_handler()
    s = Subject(type="person", name="Mark Phillips", aliases=["M. Phillips", "Marcus Phillips"],
                identifiers=[Identifier("dob", "1970", "passport copy")])
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(s, include_media=True, now=NOW, store=store)
    assert ("Layer C scanned the primary name only; alias variant(s) not sent: M. Phillips, Marcus Phillips"
            in res.coverage_gaps)


def test_no_alias_gap_without_aliases(tmp_path):
    handler, _ = make_handler()
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert not any("alias variant" in g for g in res.coverage_gaps)


def test_apply_writes_slim_match_and_media_shapes(tmp_path):
    handler, _ = make_handler()
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    rec = new_record(person(), "E", "P", NOW)
    res.apply(rec)
    assert rec["layer_c"]["scan_date"] == "2026-09-13T10:00:00"
    assert rec["layer_c"]["matches"] == [{
        "name": "Mark Phillips", "match_rate": 88, "category": "PEP", "matched_fields": "Name",
        "official_lists": [{"keyword": "Australian PEP", "is_current": True}],
        "assessment": "unreviewed", "dispositioned_by": None, "dispositioned_at": None, "disposition_note": None}]
    assert rec["layer_c"]["advanced_media_items"] == [{
        "title": "Council fined over procurement", "source_name": "Example News",
        "published": "2024-06-01T00:00:00", "link": "https://news.example/1"}]
    # the full vendor payload stays in the cache, not in the record
    with Store(tmp_path / "s.db") as store:
        assert "Full text." in store.vendor_text("ps-abc123")


def test_apply_slims_organisation_matches(tmp_path):
    body = {**load_fixture("namescan_org.json"), "numberOfMatches": 1, "corporates": [
        {"matchRate": 91, "matchedFields": "Name", "category": "Sanction",
         "entity": {"primaryName": "Green Bond Corporation",
                    "officialLists": [{"keyword": "EU Sanctions", "isCurrent": False}]}}]}
    handler, _ = make_handler(org_body=body)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(org(), include_media=False, now=NOW, store=store)
    rec = new_record(org(), "E", "P", NOW)
    res.apply(rec)
    assert rec["layer_c"]["matches"] == [{
        "name": "Green Bond Corporation", "match_rate": 91, "category": "Sanction",
        "matched_fields": "Name", "official_lists": [{"keyword": "EU Sanctions", "is_current": False}],
        "assessment": "unreviewed", "dispositioned_by": None, "dispositioned_at": None, "disposition_note": None}]


def test_alias_gap_survives_a_dedup_reuse(tmp_path):
    handler, state = make_handler()
    s = Subject(type="person", name="Mark Phillips", aliases=["M. Phillips"],
                identifiers=[Identifier("dob", "1970", "passport copy")])
    with Store(tmp_path / "s.db") as store:
        store.record_scan(s.normalised_key(), "ps-abc123", "namescan", "person", NOW)
        res = client_for(handler).scan(s, include_media=True, now=LATER, store=store)
    assert state["posts"] == 0 and res.layer_c["reused_prior_scan"] is True
    assert "Layer C scanned the primary name only; alias variant(s) not sent: M. Phillips" in res.coverage_gaps


def test_alias_subject_copies_name_only_and_drops_aliases():
    s = Subject(type="person", name="Mark Phillips", aliases=["M. Phillips", "Marcus Phillips"],
                jurisdiction="AU", identifiers=[Identifier("dob", "1970", "passport copy")])
    a = NS.alias_subject(s, "M. Phillips")
    assert a.name == "M. Phillips"
    assert a.aliases == []
    assert a.jurisdiction == "AU"
    assert a.identifier("dob") == "1970"
    assert a.normalised_key() != s.normalised_key()


def test_run_layer_c_ceiling_counts_aliases(tmp_path):
    handler, state = make_handler()
    s = Subject(type="person", name="Mark Phillips", aliases=["M. Phillips", "Marcus Phillips"],
                identifiers=[Identifier("dob", "1970", "passport copy")])
    with Store(tmp_path / "s.db") as store:
        # 1 subject + 2 aliases = 3 units, ceiling 2 must abort
        with pytest.raises(NS.RunAborted, match="ceiling"):
            NS.run_layer_c([s], client_for(handler), store, now=NOW, max_subjects=2)
    assert state["posts"] == 0
    with Store(tmp_path / "s.db") as store:
        # ceiling 3 is exactly enough
        results = NS.run_layer_c([s], client_for(handler), store, now=NOW, max_subjects=3)
    assert len(results) == 1


def test_run_layer_c_preflight_cost_counts_aliases(tmp_path):
    # unit is 1.25 (media on); 1 subject + 2 aliases = 3 units => cost 3.75; balance 3 must abort
    handler, state = make_handler(credits=3.0)
    s = Subject(type="person", name="Mark Phillips", aliases=["M. Phillips", "Marcus Phillips"],
                identifiers=[Identifier("dob", "1970", "passport copy")])
    with Store(tmp_path / "s.db") as store:
        with pytest.raises(NS.RunAborted, match="credits"):
            NS.run_layer_c([s], client_for(handler), store, now=NOW, max_subjects=20)
    assert state["posts"] == 0


def test_run_layer_c_preflight_cost_skips_already_scanned_aliases(tmp_path):
    # dedup hit for one alias means only 2 new units (subject + 1 alias) => cost 2.5, balance 2.5 is enough
    handler, state = make_handler(credits=2.5)
    s = Subject(type="person", name="Mark Phillips", aliases=["M. Phillips", "Marcus Phillips"],
                identifiers=[Identifier("dob", "1970", "passport copy")])
    with Store(tmp_path / "s.db") as store:
        store.record_scan(NS.alias_subject(s, "M. Phillips").normalised_key(), "ps-alias", "namescan", "person", NOW)
        results = NS.run_layer_c([s], client_for(handler), store, now=NOW, max_subjects=20)
    assert len(results) == 1


def test_official_lists_keep_is_current_per_list(tmp_path):
    body = {**load_fixture("namescan_person.json"), "numberOfMatches": 1, "persons": [
        {"matchRate": 88, "matchedFields": "Name, DOB", "category": "PEP",
         "person": {"name": "Mark Phillips", "officialLists": [
             {"keyword": "Australian PEP", "isCurrent": True},
             {"keyword": "EU Sanctions", "isCurrent": False},
             {"keyword": "Unknown List"}]}}]}
    handler, _ = make_handler(person_body=body)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    rec = new_record(person(), "E", "P", NOW)
    res.apply(rec)
    m = rec["layer_c"]["matches"][0]
    assert "is_current" not in m
    assert m["official_lists"] == [
        {"keyword": "Australian PEP", "is_current": True},
        {"keyword": "EU Sanctions", "is_current": False},
        {"keyword": "Unknown List", "is_current": None}]
