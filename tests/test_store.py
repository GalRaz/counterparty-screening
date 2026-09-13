import json

from screening.store import Store

T0 = "2026-01-01T00:00:00+00:00"
T89 = "2026-03-31T00:00:00+00:00"   # 89 days later
T91 = "2026-04-02T00:00:00+00:00"   # 91 days later
T400 = "2027-02-05T00:00:00+00:00"


def test_find_scan_within_window_only(tmp_path):
    with Store(tmp_path / "s.db") as s:
        s.record_scan("person:mark phillips", "scan-1", "namescan", "person", T0)
        assert s.find_scan("person:mark phillips", 90, T89) == "scan-1"
        assert s.find_scan("person:mark phillips", 90, T91) is None
        assert s.find_scan("person:someone else", 90, T89) is None


def test_find_scan_returns_newest(tmp_path):
    with Store(tmp_path / "s.db") as s:
        s.record_scan("k", "old", "namescan", "person", T0)
        s.record_scan("k", "new", "namescan", "person", T89)
        assert s.find_scan("k", 90, T91) == "new"


def test_vendor_text_cache_and_purge_keeps_scan(tmp_path):
    with Store(tmp_path / "s.db") as s:
        s.record_scan("k", "scan-1", "namescan", "person", T0)
        s.cache_vendor_text("scan-1", "{...}", T0)
        assert s.vendor_text("scan-1") == "{...}"
        counts = s.purge(T91, root=tmp_path / "cases")
        assert counts["vendor_text"] == 1
        assert s.vendor_text("scan-1") is None
        assert s.find_scan("k", 10_000, T91) == "scan-1"


def engagement(tmp_path, slugs, with_summary=True):
    root = tmp_path / "cases"
    eng = root / "E"
    for slug in slugs:
        (eng / slug).mkdir(parents=True)
        (eng / slug / "record.json").write_text("{}")
    (eng / "meta.json").write_text(json.dumps({"engagement": "E", "commissioning_party": "P", "subjects": list(slugs)}))
    if with_summary:
        (eng / "summary.md").write_text("# report naming x\n")
    return root, eng


def test_purge_deletes_old_record_files(tmp_path):
    root, eng = engagement(tmp_path, ["x"])
    rec = eng / "x" / "record.json"
    with Store(tmp_path / "s.db") as s:
        s.index_record("E", "x", str(rec), T0)
        assert s.purge(T91, root=root)["records"] == 0
        assert rec.exists()
        counts = s.purge(T400, root=root)
        assert counts["records"] == 1
        assert not rec.parent.exists()


def test_purge_removes_the_slug_from_engagement_meta(tmp_path, capsys):
    root, eng = engagement(tmp_path, ["x", "y"])
    with Store(tmp_path / "s.db") as s:
        s.index_record("E", "x", str(eng / "x" / "record.json"), T0)
        counts = s.purge(T400, root=root)
    assert counts["records"] == 1 and counts["engagements_removed"] == 0
    assert json.loads((eng / "meta.json").read_text())["subjects"] == ["y"]
    assert eng.is_dir() and (eng / "y").is_dir()


def test_purge_deletes_the_summary_and_says_so(tmp_path, capsys):
    root, eng = engagement(tmp_path, ["x", "y"])
    with Store(tmp_path / "s.db") as s:
        s.index_record("E", "x", str(eng / "x" / "record.json"), T0)
        s.purge(T400, root=root)
    assert not (eng / "summary.md").exists()
    assert "regenerate" in capsys.readouterr().out


def test_purge_removes_the_engagement_when_its_last_subject_goes(tmp_path):
    root, eng = engagement(tmp_path, ["x"])
    with Store(tmp_path / "s.db") as s:
        s.index_record("E", "x", str(eng / "x" / "record.json"), T0)
        counts = s.purge(T400, root=root)
    assert counts["engagements_removed"] == 1
    assert not eng.exists()


def test_purge_skips_paths_outside_the_cases_root(tmp_path):
    root, eng = engagement(tmp_path, ["x"])
    outside = tmp_path / "elsewhere" / "z"
    outside.mkdir(parents=True)
    (outside / "record.json").write_text("{}")
    with Store(tmp_path / "s.db") as s:
        s.index_record("E", "z", str(outside / "record.json"), T0)
        counts = s.purge(T400, root=root)
    assert counts == {"vendor_text": 0, "records": 0, "engagements_removed": 0, "skipped": 1}
    assert outside.exists()


def test_purge_counts_undeletable_directories_as_skipped(tmp_path, monkeypatch):
    root, eng = engagement(tmp_path, ["x"])
    monkeypatch.setattr("screening.store.shutil.rmtree", lambda p: (_ for _ in ()).throw(OSError("busy")))
    with Store(tmp_path / "s.db") as s:
        s.index_record("E", "x", str(eng / "x" / "record.json"), T0)
        counts = s.purge(T400, root=root)
    assert counts["skipped"] == 1 and counts["records"] == 0
    assert (eng / "x").exists()
