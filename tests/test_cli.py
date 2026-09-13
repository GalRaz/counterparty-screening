import json

import httpx
import pytest

from screening import cli
from screening.config import COMPANIES_HOUSE_BASE, GLEIF_BASE, NAMESCAN_BASE, OPENSANCTIONS_BASE
from tests.conftest import NOW, json_response, load_fixture, mock_client


@pytest.fixture(autouse=True)
def wired(monkeypatch, cases_dir):
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "OSKEY")
    monkeypatch.setenv("NAMESCAN_API_KEY", "NSKEY")
    monkeypatch.setenv("NAMESCAN_API_KEY_TEST", "NSTEST")
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "CHKEY")
    monkeypatch.setattr(cli, "now", lambda: NOW)

    def os_handler(req):
        if req.url.path == "/match/default":
            return json_response(200, load_fixture("opensanctions_match.json"))
        return json_response(200, load_fixture("opensanctions_entity.json"))

    def ns_handler(req):
        if req.url.path.endswith("/credits/sapphire"):
            return json_response(200, load_fixture("namescan_credits.json"))
        if "person-scans" in req.url.path:
            return json_response(200, load_fixture("namescan_person.json"))
        return json_response(200, load_fixture("namescan_org.json"))

    def ch_handler(req):
        if req.url.path == "/search/companies":
            return json_response(200, load_fixture("ch_search.json"))
        if req.url.path == "/company/13647702":
            return json_response(200, load_fixture("ch_profile.json"))
        return json_response(200, load_fixture("ch_officers.json"))

    monkeypatch.setattr(cli, "make_os_client", lambda: mock_client(os_handler, OPENSANCTIONS_BASE))
    monkeypatch.setattr(cli, "make_ns_client", lambda: mock_client(ns_handler, NAMESCAN_BASE))
    monkeypatch.setattr(cli, "make_gleif_client", lambda: mock_client(lambda r: json_response(200, load_fixture("gleif_records.json")), GLEIF_BASE))
    monkeypatch.setattr(cli, "make_ch_client", lambda: mock_client(ch_handler, COMPANIES_HOUSE_BASE))
    return cases_dir


def run(*argv, stdin=None, monkeypatch=None, capsys=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(stdin))
    code = cli.main(list(argv))
    out = capsys.readouterr() if capsys else None
    return code, out


def setup_case(capsys, monkeypatch):
    assert run("case", "new", "E1", "--commissioning-party", "GMC Authority", capsys=capsys)[0] == 0
    assert run("subject", "add", "E1", "--type", "organization", "--name", "Jearrard Energy Resources Ltd",
               "--jurisdiction", "GB", capsys=capsys)[0] == 0
    assert run("subject", "add", "E1", "--type", "person", "--name", "Mark Phillips", "--jurisdiction", "AU",
               "--id", "dob=1970@passport copy", "--alias", "M. Phillips", capsys=capsys)[0] == 0


def test_case_and_subject_commands(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    assert (wired / "E1" / "mark-phillips" / "record.json").exists()
    code, out = run("subject", "add", "E1", "--type", "person", "--name", "X", "--id", "dob=1970", capsys=capsys)
    assert code == 1 and "kind=value@source" in out.err


def test_run_a_c_d_and_record_show(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    assert run("run", "A", "E1", capsys=capsys)[0] == 0
    assert run("run", "C", "E1", capsys=capsys)[0] == 0
    assert run("run", "D", "E1", capsys=capsys)[0] == 0
    code, out = run("record", "show", "E1", "mark-phillips", capsys=capsys)
    rec = json.loads(out.out)
    assert [c["layer"] for c in rec["checks_run"]] == ["A", "C", "D"]
    assert rec["layer_c"]["scan_id"] == "ps-abc123"
    assert rec["layer_c"]["authorised_by"] == "GMC Authority"
    assert len(rec["watchlist_candidates"]) == 2
    code, out = run("record", "show", "E1", "jearrard-energy-resources-ltd", capsys=capsys)
    rec = json.loads(out.out)
    assert rec["layer_d"]["companies_house"]["company_number"] == "13647702"
    assert rec["proposed_subjects"][0]["name"] == "Mark Laurence Allington"


def test_run_c_test_key_flag(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    code, out = run("run", "C", "E1", "--subject", "mark-phillips", "--test", capsys=capsys)
    assert code == 0
    rec = json.loads(run("record", "show", "E1", "mark-phillips", capsys=capsys)[1].out)
    assert rec["layer_c"]["test_mode"] is True and rec["layer_c"]["credits_consumed"] == 0.0


def test_run_c_missing_key_exits_4(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    monkeypatch.delenv("NAMESCAN_API_KEY")
    monkeypatch.setattr(cli.config, "get_secret", lambda name: None)
    code, out = run("run", "C", "E1", capsys=capsys)
    assert code == 4 and "NAMESCAN_API_KEY" in out.err
    rec = json.loads(run("record", "show", "E1", "mark-phillips", capsys=capsys)[1].out)
    assert rec["checks_run"][0]["layer"] == "C" and rec["checks_run"][0]["status"] == "not_run"


def test_run_c_ceiling_exit_3(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    monkeypatch.setenv("NAMESCAN_MAX_SUBJECTS_RUN", "1")
    code, out = run("run", "C", "E1", capsys=capsys)
    assert code == 3 and "ceiling" in out.err


def test_mode_media_layerb_close_and_report(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    run("run", "A", "E1", capsys=capsys)
    code, out = run("mode", "E1", "mark-phillips", "--languages", "en", capsys=capsys)
    plan = json.loads(out.out)
    assert plan["mode"] == "full" and plan["families"][0]["family"] == 1

    item = {"title": "Councillor fined", "publisher": "Example News", "published": "2024-06-01",
            "url": "https://news.example/1", "retrieval_status": "full", "identity": "confirmed_subject",
            "corroborator": "role: councillor", "legal_status": "regulatory_action", "source_type": "wire",
            "query": '"Mark Phillips" fine', "language": "en", "summary": "A fine was imposed."}
    code, out = run("media", "add", "E1", "mark-phillips", stdin=json.dumps(item), monkeypatch=monkeypatch, capsys=capsys)
    assert code == 0

    bad = {**item, "identity": "confirmed_subject", "corroborator": None}
    code, out = run("media", "add", "E1", "mark-phillips", stdin=json.dumps(bad), monkeypatch=monkeypatch, capsys=capsys)
    assert code == 1 and "corroborator" in out.err

    # report before layerb close: lint fails
    code, out = run("report", "E1", capsys=capsys)
    assert code == 2 and "media_without_layer_b_check" in out.err
    assert not (wired / "E1" / "summary.md").exists()

    qf = wired / "queries.txt"
    qf.write_text('"Mark Phillips"\n"Mark Phillips" fraud\n')
    code, out = run("layerb", "close", "E1", "mark-phillips", "--mode", "full", "--queries-file", str(qf),
                    "--languages", "en", capsys=capsys)
    assert code == 0
    # the org subject has no Layer B; close it as well with zero media
    qf2 = wired / "q2.txt"
    qf2.write_text('"Jearrard Energy Resources Ltd"\n')
    assert run("layerb", "close", "E1", "jearrard-energy-resources-ltd", "--mode", "full", "--queries-file", str(qf2),
               "--languages", "en", capsys=capsys)[0] == 0

    code, out = run("report", "E1", capsys=capsys)
    assert code == 0
    text = (wired / "E1" / "summary.md").read_text()
    assert "https://news.example/1" in text and "This is not a commercial screening product" in text
    assert run("lint", "E1", capsys=capsys)[0] == 0


def test_registry_add_manual_finding(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    f = {"registry": "ACRA", "url": "https://www.bizfile.gov.sg/x", "fields": {"status": "Live"}, "note": "agent lookup"}
    code, out = run("registry", "add", "E1", "jearrard-energy-resources-ltd", stdin=json.dumps(f), monkeypatch=monkeypatch, capsys=capsys)
    assert code == 0
    rec = json.loads(run("record", "show", "E1", "jearrard-energy-resources-ltd", capsys=capsys)[1].out)
    assert rec["layer_d"]["manual_findings"][0]["registry"] == "ACRA"
    assert rec["layer_d"]["manual_findings"][0]["retrieved"] == NOW


def test_credits_and_purge(capsys, monkeypatch, wired):
    code, out = run("credits", capsys=capsys)
    assert code == 0 and "78.5" in out.out
    code, out = run("purge", capsys=capsys)
    assert code == 0 and "vendor_text" in out.out


def test_usage_error_exits_1(capsys, monkeypatch, wired):
    code, out = run("bogus", capsys=capsys)
    assert code == 1
    code, out = run("--help", capsys=capsys)
    assert code == 0


def test_media_add_malformed_stdin_exits_1(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    code, out = run("media", "add", "E1", "mark-phillips", stdin="not json", monkeypatch=monkeypatch, capsys=capsys)
    assert code == 1 and "JSON" in out.err
    code, out = run("media", "add", "E1", "mark-phillips", stdin='["x"]', monkeypatch=monkeypatch, capsys=capsys)
    assert code == 1


def test_report_force_writes_with_banner(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    run("run", "A", "E1", capsys=capsys)
    item = {"title": "Councillor fined", "publisher": "Example News", "published": "2024-06-01",
            "url": "https://news.example/1", "retrieval_status": "full", "identity": "confirmed_subject",
            "corroborator": "role: councillor", "legal_status": "regulatory_action", "source_type": "wire",
            "query": '"Mark Phillips" fine', "language": "en", "summary": "A fine was imposed."}
    code, out = run("media", "add", "E1", "mark-phillips", stdin=json.dumps(item), monkeypatch=monkeypatch, capsys=capsys)
    assert code == 0
    code, out = run("report", "E1", "--force", capsys=capsys)
    assert code == 2
    summary = wired / "E1" / "summary.md"
    assert summary.exists()
    assert summary.read_text().startswith("> **LINT FAILED**")


def test_run_a_missing_key_exits_4(capsys, monkeypatch, wired):
    setup_case(capsys, monkeypatch)
    monkeypatch.delenv("OPENSANCTIONS_API_KEY")
    monkeypatch.setattr(cli.config, "get_secret", lambda name: None)
    code, out = run("run", "A", "E1", capsys=capsys)
    assert code == 4
    rec = json.loads(run("record", "show", "E1", "mark-phillips", capsys=capsys)[1].out)
    assert rec["checks_run"][0]["layer"] == "A" and rec["checks_run"][0]["status"] == "not_run"
