"""`screen` command line. Deterministic side of the system; the skill drives it."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from screening import config, mode as M, namescan as NS, opensanctions as OS, registry as RG
from screening import lint as LINT, record as R, report as RP
from screening.case import Case
from screening.store import Store
from screening.subjects import Subject, parse_identifier

EXIT_OK, EXIT_ERR, EXIT_LINT, EXIT_ABORT, EXIT_NOKEY = 0, 1, 2, 3, 4


class UsageError(Exception):
    """Data/usage problem detected while handling a command (exit 1)."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_os_client() -> httpx.Client:
    return httpx.Client(base_url=config.OPENSANCTIONS_BASE)


def make_ns_client() -> httpx.Client:
    return httpx.Client(base_url=config.NAMESCAN_BASE)


def make_gleif_client() -> httpx.Client:
    return httpx.Client(base_url=config.GLEIF_BASE)


def make_ch_client() -> httpx.Client:
    return httpx.Client(base_url=config.COMPANIES_HOUSE_BASE)


def store_path() -> Path:
    return config.cases_dir() / "store.db"


def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _case(engagement: str) -> Case:
    return Case.load(config.cases_dir(), engagement)


def _slugs(case: Case, only: str | None) -> list[str]:
    if only:
        if only not in case.slugs():
            raise UsageError(f"no subject {only!r} in {case.engagement}")
        return [only]
    return case.slugs()


def _read_stdin_json():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        raise UsageError(f"invalid JSON on stdin: {e}") from e
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        return data
    raise UsageError("stdin JSON must be an object or a list of objects")


# ---- commands -------------------------------------------------------------

def cmd_case_new(a) -> int:
    Case.create(config.cases_dir(), a.engagement, a.commissioning_party, now())
    print(f"created case {a.engagement} under {config.cases_dir()}")
    return EXIT_OK


def cmd_subject_add(a) -> int:
    case = _case(a.engagement)
    try:
        ids = [parse_identifier(t) for t in (a.id or [])]
        s = Subject(type=a.type, name=a.name, aliases=a.alias or [], identifiers=ids,
                    jurisdiction=a.jurisdiction, role=a.role, os_schema=a.os_schema)
    except ValueError as e:
        err(str(e))
        return EXIT_ERR
    slug = case.add_subject(s, now())
    with Store(store_path()) as st:
        st.index_record(case.engagement, slug, str(case.root / slug / "record.json"), now())
    print(slug)
    return EXIT_OK


def cmd_run(a) -> int:
    if a.layer != "C" and (a.test or a.no_media):
        err(f"--test and --no-media apply to Layer C only; Layer {a.layer} takes neither")
        return EXIT_ERR
    case = _case(a.engagement)
    slugs = _slugs(case, a.subject)
    return {"A": _run_a, "C": _run_c, "D": _run_d}[a.layer](case, slugs, a)


def _run_a(case: Case, slugs: list[str], a) -> int:
    key = config.get_secret("OPENSANCTIONS_API_KEY")
    if not key:
        err("OPENSANCTIONS_API_KEY not found in env or Keychain")
        for slug in slugs:
            rec = case.record(slug)
            R.replace_layer(rec, "A")
            rec["checks_run"].append({"layer": "A", "provider": "opensanctions", "status": "not_run", "error": "no API key", "timestamp": now()})
            R.add_coverage_gap(rec, "Layer A (opensanctions) not run: no API key")
            case.save_record(slug, rec)
        return EXIT_NOKEY
    client = make_os_client()
    for slug in slugs:
        res = OS.match(case.subject(slug), client, key, now())
        rec = case.record(slug)
        R.replace_layer(rec, "A")
        res.apply(rec)
        case.save_record(slug, rec)
        print(f"{slug}: Layer A {res.check['status']}, {len(res.candidates)} candidate(s)")
    return EXIT_OK


def _run_c(case: Case, slugs: list[str], a) -> int:
    key_name = "NAMESCAN_API_KEY_TEST" if a.test else "NAMESCAN_API_KEY"
    key = config.get_secret(key_name)
    if not key:
        err(f"{key_name} not found in env or Keychain")
        for slug in slugs:
            rec = case.record(slug)
            R.replace_layer(rec, "C")
            rec["checks_run"].append({"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "not_run",
                                      "error": "no API key", "timestamp": now(), "attempts": 0})
            R.add_coverage_gap(rec, "Layer C (namescan) not run: no API key")
            case.save_record(slug, rec)
        return EXIT_NOKEY
    nc = NS.NameScanClient(make_ns_client(), key, test_mode=a.test)
    subjects = [case.subject(s) for s in slugs]
    with Store(store_path()) as st:
        try:
            results = NS.run_layer_c(subjects, nc, st, now=now(), include_media=not a.no_media,
                                     max_subjects=config.max_subjects_run())
        except NS.RunAborted as e:
            err(f"Layer C aborted before any spend: {e}")
            return EXIT_ABORT
    for slug, res in zip(slugs, results):
        rec = case.record(slug)
        R.replace_layer(rec, "C")
        if res.layer_c is not None:
            res.layer_c["authorised_by"] = case.commissioning_party
            res.layer_c["authorised_at"] = now()
        res.apply(rec)
        case.save_record(slug, rec)
        for w in res.warnings:
            err(f"{slug}: warning: {w}")
        summary = (f"scan {res.layer_c['scan_id']}, {res.layer_c['number_of_matches']} match(es), media {res.layer_c['adverse_media']}"
                   if res.layer_c else f"failed: {res.check['error']}")
        print(f"{slug}: Layer C {res.check['status']} — {summary}")
    return EXIT_OK


def _run_d(case: Case, slugs: list[str], a) -> int:
    ch_key = config.get_secret("COMPANIES_HOUSE_API_KEY")
    gleif = make_gleif_client()
    ch = make_ch_client() if ch_key else None
    for slug in slugs:
        res = RG.run_layer_d(case.subject(slug), gleif_client=gleif, ch_client=ch, ch_api_key=ch_key, now=now())
        rec = case.record(slug)
        R.replace_layer(rec, "D")
        res.apply(rec)
        case.save_record(slug, rec)
        print(f"{slug}: Layer D {res.check['status']}; proposed subjects: {len(res.proposed_subjects)}")
        for p in res.proposed_subjects:
            print(f"  proposed: {p['name']} — {p['reason']} ({p['source']})")
    return EXIT_OK


def cmd_mode(a) -> int:
    case = _case(a.engagement)
    rec = case.record(a.slug)
    m = M.select(rec.get("layer_c"))
    langs = [l.strip() for l in a.languages.split(",")] if a.languages else ["en"]
    print(json.dumps(M.query_plan(case.subject(a.slug), m, langs), indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_media_add(a) -> int:
    case = _case(a.engagement)
    rec = case.record(a.slug)
    items = _read_stdin_json()
    new = []
    for raw in items:
        raw.setdefault("retrieved", now())
        item = R.new_media_item(**{k: raw.get(k) for k in (
            "title", "publisher", "published", "url", "retrieved", "retrieval_status", "identity",
            "corroborator", "legal_status", "source_type", "query", "language", "summary", "proposed_disposition")})
        new.append(item)
    trial = {**rec, "media_items": rec["media_items"] + new}
    errs = R.validate(trial)
    if errs:
        for e in errs:
            err(e)
        return EXIT_ERR
    rec["media_items"] = trial["media_items"]
    case.save_record(a.slug, rec)
    print(f"added {len(new)} media item(s) to {a.slug}")
    return EXIT_OK


def cmd_media_rm(a) -> int:
    case = _case(a.engagement)
    rec = case.record(a.slug)
    items = rec["media_items"]
    if not 0 <= a.index < len(items):
        err(f"media_items index {a.index} out of range: {a.slug} has {len(items)} item(s)")
        return EXIT_ERR
    gone = items.pop(a.index)
    case.save_record(a.slug, rec)
    print(f"{a.slug}: removed media item {a.index}: {gone['title']} — {gone['publisher']} {gone['url']}")
    return EXIT_OK


def cmd_gap_add(a) -> int:
    case = _case(a.engagement)
    rec = case.record(a.slug)
    R.add_coverage_gap(rec, a.text)
    case.save_record(a.slug, rec)
    print(f"{a.slug}: coverage gap recorded: {a.text}")
    return EXIT_OK


def cmd_layerb_close(a) -> int:
    case = _case(a.engagement)
    rec = case.record(a.slug)
    queries = [q for q in Path(a.queries_file).read_text().splitlines() if q.strip()]
    langs = [l.strip() for l in a.languages.split(",")]
    if a.status == "failed" and not (a.reason or "").strip():
        err("--status failed requires --reason: a failed layer must name its coverage gap (§6.7)")
        return EXIT_ERR
    m = M.select(rec.get("layer_c"))
    if m.mode == "full" and a.mode == "reduced":
        # Reduced mode is only ever earned by a Layer C match with media (§3.0). Declaring it
        # otherwise would narrow the search on paper without narrowing what was missed.
        err(f"refusing to record reduced mode: computed mode is full ({m.reason}). "
            "Run the full query plan, or re-run Layer C first.")
        return EXIT_ERR
    if m.mode != a.mode:
        err(f"warning: declared mode {a.mode} differs from computed mode {m.mode} ({m.reason}); recording declared mode")
        m = M.Mode(a.mode, f"declared by agent; computed was {m.mode}: {m.reason}", m.families, m.risk_window_months)
    R.replace_layer(rec, "B")
    check = M.layer_b_check(m, queries, langs, now(), status=a.status)
    if a.status == "failed":
        check["error"] = a.reason
        R.add_coverage_gap(rec, f"Layer B (web_search) incomplete: {a.reason}")
    rec["checks_run"].append(check)
    case.save_record(a.slug, rec)
    print(f"{a.slug}: Layer B closed ({a.status}), {len(queries)} query string(s), languages {','.join(langs)}")
    return EXIT_OK


def cmd_registry_add(a) -> int:
    case = _case(a.engagement)
    rec = case.record(a.slug)
    if rec["layer_d"] is None:
        rec["layer_d"] = {"gleif": [], "companies_house": None, "manual_findings": []}
    for raw in _read_stdin_json():
        try:
            f = RG.manual_finding(registry=raw["registry"], url=raw["url"], retrieved=raw.get("retrieved") or now(),
                                  fields=raw.get("fields", {}), note=raw.get("note"))
        except KeyError as e:
            err(f"manual finding missing field {e}")
            return EXIT_ERR
        rec["layer_d"]["manual_findings"].append(f)
    case.save_record(a.slug, rec)
    print(f"{a.slug}: manual registry finding(s) added")
    return EXIT_OK


def cmd_record_show(a) -> int:
    print(json.dumps(_case(a.engagement).record(a.slug), indent=2, ensure_ascii=False))
    return EXIT_OK


def _lint(case: Case, report: str | None) -> list[LINT.Violation]:
    records = [case.record(s) for s in case.slugs()]
    return LINT.lint_all(records, report)


def cmd_lint(a) -> int:
    case = _case(a.engagement)
    report = case.summary_path.read_text() if case.summary_path.exists() else None
    vs = _lint(case, report)
    for v in vs:
        err(f"{v.where}: [{v.rule}] {v.message}")
    print(f"{len(vs)} violation(s)")
    return EXIT_LINT if vs else EXIT_OK


def cmd_report(a) -> int:
    case = _case(a.engagement)
    records = [case.record(s) for s in case.slugs()]
    text = RP.render(case, records, now())
    vs = LINT.lint_all(records, text)
    for v in vs:
        err(f"{v.where}: [{v.rule}] {v.message}")
    if vs and not a.force:
        err(f"{len(vs)} lint violation(s); summary.md not written. Fix the record or pass --force.")
        return EXIT_LINT
    if vs:
        text = "> **LINT FAILED** — written with --force; see violations above.\n\n" + text
    case.summary_path.write_text(text)
    print(f"wrote {case.summary_path}")
    return EXIT_LINT if vs else EXIT_OK


def cmd_credits(a) -> int:
    key = config.get_secret("NAMESCAN_API_KEY_TEST" if a.test else "NAMESCAN_API_KEY")
    if not key:
        err("NameScan key not found")
        return EXIT_NOKEY
    try:
        bal = NS.NameScanClient(make_ns_client(), key, test_mode=a.test).credits()
    except (KeyError, ValueError, TypeError) as e:
        err(f"could not read NameScan credit balance: {type(e).__name__}: {e}")
        return EXIT_ERR
    print(f"NameScan Sapphire balance: {bal} credits")
    return EXIT_OK


def cmd_purge(a) -> int:
    with Store(store_path()) as st:
        counts = st.purge(now(), config.VENDOR_TEXT_DAYS, config.RECORD_RETENTION_DAYS)
    print(json.dumps(counts))
    return EXIT_OK


# ---- parser ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="screen", description="Counterparty screening. Emits candidates for human review, never verdicts.")
    sp = p.add_subparsers(dest="cmd", required=True)

    c = sp.add_parser("case").add_subparsers(dest="sub", required=True)
    n = c.add_parser("new"); n.add_argument("engagement"); n.add_argument("--commissioning-party", required=True); n.set_defaults(fn=cmd_case_new)

    s = sp.add_parser("subject").add_subparsers(dest="sub", required=True)
    sa = s.add_parser("add"); sa.add_argument("engagement"); sa.add_argument("--type", required=True, choices=["person", "organization"])
    sa.add_argument("--name", required=True); sa.add_argument("--alias", action="append"); sa.add_argument("--id", action="append", help="kind=value@source")
    sa.add_argument("--jurisdiction"); sa.add_argument("--role"); sa.add_argument("--os-schema"); sa.set_defaults(fn=cmd_subject_add)

    r = sp.add_parser("run"); r.add_argument("layer", choices=["A", "C", "D"]); r.add_argument("engagement")
    r.add_argument("--subject"); r.add_argument("--test", action="store_true", help="NameScan test key (no credits, no media)")
    r.add_argument("--no-media", action="store_true"); r.set_defaults(fn=cmd_run)

    m = sp.add_parser("mode"); m.add_argument("engagement"); m.add_argument("slug"); m.add_argument("--languages", default="en"); m.set_defaults(fn=cmd_mode)

    md = sp.add_parser("media").add_subparsers(dest="sub", required=True)
    ma = md.add_parser("add"); ma.add_argument("engagement"); ma.add_argument("slug"); ma.set_defaults(fn=cmd_media_add)
    mr = md.add_parser("rm"); mr.add_argument("engagement"); mr.add_argument("slug")
    mr.add_argument("index", type=int, help="0-based index into media_items"); mr.set_defaults(fn=cmd_media_rm)

    gp = sp.add_parser("gap").add_subparsers(dest="sub", required=True)
    ga = gp.add_parser("add"); ga.add_argument("engagement"); ga.add_argument("slug")
    ga.add_argument("text", help="what could not be checked, in the agent's own words"); ga.set_defaults(fn=cmd_gap_add)

    lb = sp.add_parser("layerb").add_subparsers(dest="sub", required=True)
    lc = lb.add_parser("close"); lc.add_argument("engagement"); lc.add_argument("slug")
    lc.add_argument("--mode", required=True, choices=["full", "reduced"]); lc.add_argument("--queries-file", required=True)
    lc.add_argument("--languages", required=True)
    lc.add_argument("--status", default="ok", choices=["ok", "failed"])
    lc.add_argument("--reason", help="required with --status failed; recorded as a coverage gap")
    lc.set_defaults(fn=cmd_layerb_close)

    rg = sp.add_parser("registry").add_subparsers(dest="sub", required=True)
    ra = rg.add_parser("add"); ra.add_argument("engagement"); ra.add_argument("slug"); ra.set_defaults(fn=cmd_registry_add)

    rc = sp.add_parser("record").add_subparsers(dest="sub", required=True)
    rs = rc.add_parser("show"); rs.add_argument("engagement"); rs.add_argument("slug"); rs.set_defaults(fn=cmd_record_show)

    rp = sp.add_parser("report"); rp.add_argument("engagement"); rp.add_argument("--force", action="store_true"); rp.set_defaults(fn=cmd_report)
    li = sp.add_parser("lint"); li.add_argument("engagement"); li.set_defaults(fn=cmd_lint)
    cr = sp.add_parser("credits"); cr.add_argument("--test", action="store_true"); cr.set_defaults(fn=cmd_credits)
    pu = sp.add_parser("purge"); pu.set_defaults(fn=cmd_purge)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code == 0 else EXIT_ERR
    try:
        return a.fn(a)
    except UsageError as e:
        err(str(e))
        return EXIT_ERR
    except (FileNotFoundError, FileExistsError) as e:
        err(str(e))
        return EXIT_ERR
    except httpx.HTTPError as e:
        err(f"network error: {type(e).__name__}: {e}")
        return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
