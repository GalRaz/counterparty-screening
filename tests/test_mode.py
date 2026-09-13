from screening import mode as M
from screening.subjects import Subject
from tests.conftest import NOW


def lc(**kw):
    base = {"number_of_matches": 0, "adverse_media": "not_requested"}
    return {**base, **kw}


def test_no_layer_c_is_full():
    m = M.select(None)
    assert m.mode == "full" and m.families == [1, 2, 3, 4, 5] and "not run" in m.reason


def test_zero_matches_is_full():
    assert M.select(lc(number_of_matches=0, adverse_media="ok")).mode == "full"


def test_match_with_media_is_reduced():
    m = M.select(lc(number_of_matches=2, adverse_media="ok"))
    assert m.mode == "reduced" and m.families == [3, 4, 5] and m.risk_window_months == 24


def test_match_with_media_failed_is_full():
    m = M.select(lc(number_of_matches=2, adverse_media="failed"))
    assert m.mode == "full" and "did not run" in m.reason


def test_match_with_media_not_requested_is_full():
    assert M.select(lc(number_of_matches=2, adverse_media="not_requested")).mode == "full"


def test_query_plan_full_builds_concrete_queries():
    s = Subject(type="person", name="Alex Example", aliases=["A. Example"], jurisdiction="AU")
    plan = M.query_plan(s, M.select(None), ["en"])
    fam = {f["family"]: f for f in plan["families"]}
    assert '"Alex Example"' in fam[1]["queries"] and '"A. Example"' in fam[1]["queries"]
    assert '"Alex Example" fraud' in fam[3]["queries"]
    assert len(fam[3]["queries"]) == len(M.RISK_TERMS) * 2
    assert fam[2]["queries"] is None and "employer" in fam[2]["instruction"]
    assert fam[4]["queries"] is None and "AU" in fam[4]["instruction"]
    assert fam[5]["queries"] is None


def test_query_plan_reduced_limits_families_and_window():
    s = Subject(type="person", name="Alex Example")
    plan = M.query_plan(s, M.select(lc(number_of_matches=1, adverse_media="ok")), ["en", "dz"])
    assert [f["family"] for f in plan["families"]] == [3, 4, 5]
    assert "24 months" in [f for f in plan["families"] if f["family"] == 3][0]["instruction"]
    assert plan["languages"] == ["en", "dz"]


def test_layer_b_check_shape():
    c = M.layer_b_check(M.select(None), ['"x"'], ["en"], NOW)
    assert c == {"layer": "B", "provider": "web_search", "mode": "full", "mode_reason": c["mode_reason"],
                 "queries_run": ['"x"'], "languages": ["en"], "status": "ok", "timestamp": NOW}


def test_layer_b_check_can_be_closed_as_failed():
    c = M.layer_b_check(M.select(None), [], ["en"], NOW, status="failed")
    assert c["status"] == "failed"
    assert M.layer_b_check(M.select(None), [], ["en"], NOW)["status"] == "ok"
