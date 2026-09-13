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
        counts = s.purge(T91)
        assert counts["vendor_text"] == 1
        assert s.vendor_text("scan-1") is None
        assert s.find_scan("k", 10_000, T91) == "scan-1"


def test_purge_deletes_old_record_files(tmp_path):
    rec = tmp_path / "cases" / "E" / "x" / "record.json"
    rec.parent.mkdir(parents=True)
    rec.write_text("{}")
    with Store(tmp_path / "s.db") as s:
        s.index_record("E", "x", str(rec), T0)
        assert s.purge(T91)["records"] == 0
        assert rec.exists()
        assert s.purge(T400)["records"] == 1
        assert not rec.parent.exists()
