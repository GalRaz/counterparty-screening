"""Full A → C → B → D → report flow through the CLI against mocks. Asserts the summary reads correctly."""
import json

import pytest

from screening import cli
from screening.config import COMPANIES_HOUSE_BASE, GLEIF_BASE, NAMESCAN_BASE, OPENSANCTIONS_BASE
from tests.conftest import NOW, json_response, load_fixture, mock_client


@pytest.fixture
def wired(monkeypatch, cases_dir):
    for k in ("OPENSANCTIONS_API_KEY", "NAMESCAN_API_KEY", "COMPANIES_HOUSE_API_KEY"):
        monkeypatch.setenv(k, "K")
    monkeypatch.setattr(cli, "now", lambda: NOW)

    def os_handler(req):
        if req.url.path == "/match/default":
            body = json.loads(req.content)
            if body["queries"]["q1"]["schema"] == "Company":
                return json_response(200, {"responses": {"q1": {"results": []}}})
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
    monkeypatch.setattr(cli, "make_gleif_client", lambda: mock_client(lambda r: json_response(200, {"data": []}), GLEIF_BASE))
    monkeypatch.setattr(cli, "make_ch_client", lambda: mock_client(ch_handler, COMPANIES_HOUSE_BASE))
    return cases_dir


def test_full_flow(wired, monkeypatch, capsys):
    import io
    E = "E2E-2026-001"
    assert cli.main(["case", "new", E, "--commissioning-party", "Test Authority"]) == 0
    assert cli.main(["subject", "add", E, "--type", "organization", "--name", "Jearrard Energy Resources Ltd", "--jurisdiction", "GB"]) == 0
    assert cli.main(["subject", "add", E, "--type", "person", "--name", "Mark Phillips", "--jurisdiction", "AU"]) == 0
    assert cli.main(["run", "A", E]) == 0
    assert cli.main(["run", "C", E]) == 0
    assert cli.main(["run", "D", E]) == 0

    capsys.readouterr()  # discard accumulated stdout from case/subject/run so mode's JSON parses cleanly
    # Person had a Layer C match with media -> reduced mode
    cli.main(["mode", E, "mark-phillips", "--languages", "en"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "reduced"
    # Org had zero matches -> full mode
    cli.main(["mode", E, "jearrard-energy-resources-ltd", "--languages", "en"])
    assert json.loads(capsys.readouterr().out)["mode"] == "full"

    q = wired / "q.txt"
    q.write_text('"Mark Phillips" fraud\n')
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "title": "Councillor fined over procurement", "publisher": "Example News", "published": "2024-06-01",
        "url": "https://news.example/1", "retrieval_status": "full", "identity": "confirmed_subject",
        "corroborator": "role: councillor; Layer C profile lists same role", "legal_status": "regulatory_action",
        "source_type": "wire", "query": '"Mark Phillips" fine', "language": "en", "summary": "A council fine was reported."})))
    assert cli.main(["media", "add", E, "mark-phillips"]) == 0
    assert cli.main(["layerb", "close", E, "mark-phillips", "--mode", "reduced", "--queries-file", str(q), "--languages", "en"]) == 0
    assert cli.main(["layerb", "close", E, "jearrard-energy-resources-ltd", "--mode", "full", "--queries-file", str(q), "--languages", "en"]) == 0

    assert cli.main(["report", E]) == 0
    text = (wired / E / "summary.md").read_text()
    assert "This is not a commercial screening product" not in text  # Layer C ran for all
    assert "ps-abc123" in text and "os-def456" in text
    assert "Mark Laurence Allington" in text  # proposed, not screened
    assert "Councillor fined over procurement" in text
    assert "regulatory_action" in text
    assert "assessment: unreviewed" in text
    assert cli.main(["lint", E]) == 0
    rec = json.loads((wired / E / "mark-phillips" / "record.json").read_text())
    assert rec["human_review_required"] is True
    assert rec["layer_c"]["authorised_by"] == "Test Authority"
