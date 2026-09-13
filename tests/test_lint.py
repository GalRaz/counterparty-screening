from screening import lint as L
from screening.record import new_media_item, new_record
from screening.subjects import Subject
from tests.conftest import NOW


def rec(**over):
    r = new_record(Subject(type="person", name="Mark Phillips"), "E", "P", NOW)
    r.update(over)
    return r


def item(**over):
    base = dict(title="Council fined", publisher="Example News", published="2024-06-01", url="https://news.example/1",
                retrieved=NOW, retrieval_status="full", identity="possible_subject", corroborator=None,
                legal_status="regulatory_action", source_type="wire")
    base.update(over)
    return new_media_item(**base)


def rules(vs):
    return sorted({v.rule for v in vs})


def test_forbidden_phrases_in_record_and_report():
    r = rec()
    r["coverage_gaps"].append("subject appears clear")
    assert "forbidden_phrase" in rules(L.lint_record(r))
    assert "forbidden_phrase" in rules(L.lint_report("## Bottom line\nNo risk found.\n", [rec()]))
    assert "forbidden_phrase" in rules(L.lint_report("## Bottom line\nNo adverse media found.\n", [rec()]))
    assert "forbidden_phrase" not in rules(L.lint_report("## Bottom line\nThe register clearly lists it.\n", [rec()]))


def test_confirmed_requires_corroborator():
    r = rec(media_items=[item(identity="confirmed_subject", corroborator=None)])
    assert "confirmed_without_corroborator" in rules(L.lint_record(r))
    r = rec(media_items=[item(identity="confirmed_subject", corroborator="employer: Acme")])
    assert "confirmed_without_corroborator" not in rules(L.lint_record(r))


def test_failed_check_needs_gap():
    r = rec(checks_run=[{"layer": "A", "provider": "opensanctions", "status": "failed", "timestamp": NOW}])
    assert "failed_check_without_gap" in rules(L.lint_record(r))
    r["coverage_gaps"].append("Layer A (opensanctions) call failed: ConnectError")
    assert "failed_check_without_gap" not in rules(L.lint_record(r))


def test_media_items_need_layer_b_close():
    r = rec(media_items=[item()])
    assert "media_without_layer_b_check" in rules(L.lint_record(r))
    r["checks_run"].append({"layer": "B", "provider": "web_search", "mode": "full", "status": "ok",
                            "queries_run": ['"Mark Phillips"'], "languages": ["en"], "timestamp": NOW})
    assert "media_without_layer_b_check" not in rules(L.lint_record(r))


def test_possible_subject_not_in_findings():
    r = rec(media_items=[item(title="Council fined", url="https://news.example/1")])
    report = "## Findings\nCouncil fined (Example News) https://news.example/1\n## Coverage gaps\n"
    assert "possible_subject_in_findings" in rules(L.lint_report(report, [r]))
    report = "## Findings\nnothing\n## Coverage gaps\nhttps://news.example/1\n"
    assert "possible_subject_in_findings" not in rules(L.lint_report(report, [r]))


def test_legal_status_words_must_match_record():
    r = rec(media_items=[item(identity="confirmed_subject", corroborator="x", legal_status="allegation")])
    report = "## Findings\nMr Phillips was convicted of fraud.\n"
    assert "legal_status_mismatch" in rules(L.lint_report(report, [r]))
    report = "## Findings\nMr Phillips was accused of fraud (allegation).\n"
    assert "legal_status_mismatch" not in rules(L.lint_report(report, [r]))


def test_section_8_required_when_layer_c_missing():
    assert "missing_section_8" in rules(L.lint_report("## Bottom line\nx\n", [rec()]))
    ok = f"## Bottom line\n{L.SECTION_8_MARKER}.\n"
    assert "missing_section_8" not in rules(L.lint_report(ok, [rec()]))
    with_c = rec(layer_c={"scan_id": "ps-1", "adverse_media": "ok"})
    with_c["checks_run"].append({"layer": "C", "provider": "namescan", "status": "ok", "timestamp": NOW})
    assert "missing_section_8" not in rules(L.lint_report("## Bottom line\nx\n", [with_c]))


def test_report_section_extraction():
    txt = "# T\n## Findings\na\nb\n## Coverage gaps\nc\n"
    assert L.report_section(txt, "Findings") == "a\nb\n"
    assert L.report_section(txt, "Nope") == ""


def test_legal_status_word_not_justified_by_possible_subject():
    r = rec(media_items=[item(identity="possible_subject", legal_status="convicted")])
    report = "## Findings\nMr Phillips was convicted of fraud.\n"
    assert "legal_status_mismatch" in rules(L.lint_report(report, [r]))
    # Verify the violation message contains the word "convicted" not "onvicted"
    vs = L.lint_report(report, [r])
    msgs = [v.message for v in vs if v.rule == "legal_status_mismatch"]
    assert any("convicted" in msg for msg in msgs)


def test_whitespace_corroborator_is_missing():
    r = rec(media_items=[item(identity="confirmed_subject", corroborator="  ")])
    assert "confirmed_without_corroborator" in rules(L.lint_record(r))


def test_test_key_layer_c_does_not_satisfy_section_8():
    r = rec(layer_c={"scan_id": "ps-1", "adverse_media": "not_requested", "test_mode": True})
    r["checks_run"].append({"layer": "C", "provider": "namescan", "status": "ok", "timestamp": NOW})
    assert L._layer_c_ok(r) is False
    assert "missing_section_8" in rules(L.lint_report("## Bottom line\nx\n", [r]))
    assert "test_mode_without_section_8" in rules(L.lint_report("## Bottom line\nx\n", [r]))
    ok = f"## Bottom line\n{L.SECTION_8_MARKER}.\n"
    assert rules(L.lint_report(ok, [r])) == []


def test_duplicate_layer_check_is_a_violation():
    r = rec(checks_run=[{"layer": "A", "provider": "opensanctions", "status": "ok", "timestamp": NOW},
                        {"layer": "A", "provider": "opensanctions", "status": "ok", "timestamp": NOW}])
    assert "duplicate_layer_check" in rules(L.lint_record(r))
    r["checks_run"].pop()
    assert "duplicate_layer_check" not in rules(L.lint_record(r))


def test_forbidden_phrase_ignores_vendor_quoted_candidate_lines():
    report = ("## Bottom line\nTwo subjects screened.\n\n"
              "## Findings\n"
              "- `NK-1` Clear Channel Holdings Ltd — score 0.81, topics none, datasets x — assessment: unreviewed\n"
              "  - candidate: Clear Water Trading — match rate 88, category PEP, lists none — assessment: unreviewed\n"
              f"\n## Limitations\n\n## Coverage gaps\n- none\n")
    assert "forbidden_phrase" not in rules(L.lint_report(report, [rec()]))
    bad = report.replace("Two subjects screened.", "No risk found.")
    assert "forbidden_phrase" in rules(L.lint_report(bad, [rec()]))


def test_layer_b_mode_reason_is_scanned_for_forbidden_phrases():
    r = rec(checks_run=[{"layer": "B", "provider": "web_search", "mode": "reduced",
                         "mode_reason": "subject looked clear on Layer C", "queries_run": [],
                         "languages": ["en"], "status": "ok", "timestamp": NOW}])
    assert "forbidden_phrase" in rules(L.lint_record(r))
