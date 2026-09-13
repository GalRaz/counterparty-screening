"""Markdown summary. Only §3.6/§8 phrasing for negatives; candidates stay labelled as candidates."""
from __future__ import annotations

from screening.case import Case
from screening.lint import SECTION_8_MARKER, _layer_c_ok

NEGATIVE_MEDIA = ("No adverse media items meeting the identity-resolution criteria were returned by the "
                  "queries listed in `queries_run`, searched in {langs} on {date}.")


def _or(value, fallback: str = "unknown") -> str:
    """Return str(value) if not None or empty string, else fallback."""
    return str(value) if value not in (None, "") else fallback


CURRENCY = {True: "current", False: "not current", None: "currency unknown"}


def _official_lists(match: dict) -> str:
    """`keyword (current) / keyword (not current)` — each list keeps its own currency (§7.6)."""
    lists = match.get("official_lists") or []
    return " / ".join(f"{_or(l.get('keyword'))} ({CURRENCY[l.get('is_current')]})" for l in lists) or "none"


def _disposition_suffix(c: dict) -> str:
    """§5: once a human dispositions a candidate, say who and when — never a verdict on the subject."""
    by = c.get("dispositioned_by")
    if not by:
        return ""
    date = str(c.get("dispositioned_at") or "")[:10]
    note = f" ({c['disposition_note']})" if c.get("disposition_note") else ""
    return f" — dispositioned {c.get('assessment')} by {by} on {date}{note}"


def _count_dispositioned(records: list[dict]) -> int:
    n = sum(1 for r in records for c in r["watchlist_candidates"] if c.get("dispositioned_by"))
    n += sum(1 for r in records for m in (r.get("layer_c") or {}).get("matches") or [] if m.get("dispositioned_by"))
    return n


def _check(record: dict, layer: str) -> dict | None:
    for c in record["checks_run"]:
        if c["layer"] == layer:
            return c
    return None


def _status_cell(record: dict, layer: str) -> str:
    c = _check(record, layer)
    if c is None:
        return "not run"
    if c["status"] == "failed":
        return "FAILED"
    if c["status"] == "not_run":
        return "n/a"
    if layer == "A":
        return f"{len(record['watchlist_candidates'])} candidate(s)"
    if layer == "B":
        n = sum(1 for m in record["media_items"] if m["identity"] == "confirmed_subject")
        p = sum(1 for m in record["media_items"] if m["identity"] == "possible_subject")
        return f"{c.get('mode')} mode; {n} confirmed, {p} unresolved item(s)"
    if layer == "C":
        lc = record["layer_c"] or {}
        return (f"{_or(lc.get('number_of_matches'))} match(es); media {_or(lc.get('adverse_media'))}; "
                f"scan {_or(lc.get('scan_id'))}")
    if layer == "D":
        ld = record["layer_d"] or {}
        parts = [f"GLEIF {len(ld.get('gleif', []))}", "CH yes" if ld.get("companies_house") else "CH no",
                 f"manual {len(ld.get('manual_findings', []))}"]
        return ", ".join(parts)
    return c["status"]


def _section_8(records: list[dict], now: str) -> str:
    langs = sorted({l for r in records for c in r["checks_run"] if c["layer"] == "B" for l in c.get("languages", [])}) or ["en"]
    return (f"Screening performed against the OpenSanctions consolidated dataset (sanctions, PEP and watchlist sources) "
            f"and by structured web search for adverse media in {', '.join(langs)} on {now[:10]}. {SECTION_8_MARKER}: "
            "it does not include proprietary analyst-curated risk profiles or a licensed negative-news index, and no "
            "third-party screening provider has reviewed these results.")


def render(case: Case, records: list[dict], now: str) -> str:
    L: list[str] = []
    w = L.append
    w("# Counterparty Screening — Summary of Results\n")
    w(f"**Engagement:** {case.engagement}  ")
    w(f"**Commissioning party:** {case.commissioning_party}  ")
    w(f"**Generated:** {now[:10]}  ")
    w("**Status:** CONFIDENTIAL — candidate matches for human review. Nothing here is a disposition.\n")

    w("## Bottom line\n")
    all_c = all(_layer_c_ok(r) for r in records)
    n_cand = sum(len(r["watchlist_candidates"]) for r in records)
    n_conf = sum(1 for r in records for m in r["media_items"] if m["identity"] == "confirmed_subject")
    n_poss = sum(1 for r in records for m in r["media_items"] if m["identity"] == "possible_subject")
    w(f"{len(records)} subject(s) screened. Watchlist candidates returned: {n_cand}. Media items linked to a subject "
      f"by a corroborating attribute: {n_conf}. Media items unresolved (name match only): {n_poss}. "
      "Candidates not yet dispositioned carry `assessment: unreviewed`; only the commissioning party dispositions them. "
      f"Candidates dispositioned by the commissioning party: {_count_dispositioned(records)}.\n")
    if not all_c:
        w(_section_8(records, now) + "\n")
    else:
        w("Commercial screening (NameScan Sapphire) was run for every subject; scan IDs are listed per subject below.\n")
    n_test = sum(1 for r in records if (r.get("layer_c") or {}).get("test_mode") is True)
    if n_test:
        w(f"Layer C was run with the NameScan TEST key for {n_test} subject(s): no real coverage, no credits spent.\n")

    w("## What was screened\n")
    w("| Subject | Type | Layer A (OpenSanctions) | Layer C (NameScan) | Layer B (web search) | Layer D (registries) |")
    w("|---|---|---|---|---|---|")
    for r in records:
        s = r["subject"]
        w(f"| {s['name']} | {s['type']} | {_status_cell(r, 'A')} | {_status_cell(r, 'C')} | {_status_cell(r, 'B')} | {_status_cell(r, 'D')} |")
    w("")

    w("## Limitations\n")
    for r in records:
        s = r["subject"]
        if not s["identifiers_supplied"]:
            w(f"- **{s['name']}:** name-only screening; no identifiers supplied. Assurance is limited for common names.")
        poss = [m for m in r["media_items"] if m["identity"] == "possible_subject"]
        if poss:
            w(f"- **{s['name']}:** {len(poss)} unresolved media item(s), name match only, not linked to the subject:")
            for m in poss:
                w(f"  - {m['title']} — {m['publisher']}, {m.get('published') or 'undated'} ({m['legal_status']}, {m['retrieval_status']}) {m['url']}")
        for c in r["checks_run"]:
            if c["status"] == "failed":
                w(f"- **{s['name']}:** Layer {c['layer']} ({c['provider']}) failed: {c.get('error')}. Treated as a coverage gap, not a result.")
    w("")

    w("## Findings\n")
    for r in records:
        s = r["subject"]
        w(f"### {s['name']}\n")
        w("**Watchlist candidates (Layer A, OpenSanctions):**")
        if r["watchlist_candidates"]:
            for c in r["watchlist_candidates"]:
                pd = f" — proposed: {c['proposed_disposition']}" if c.get("proposed_disposition") else ""
                w(f"- `{c.get('id')}` {_or(c.get('caption') or c.get('id'))} — score {_or(c.get('score'))}, "
                  f"topics {', '.join(c.get('topics') or []) or 'none'}, "
                  f"datasets {', '.join(c.get('datasets') or [])} — assessment: {c['assessment']}"
                  f"{_disposition_suffix(c)}{pd}")
        else:
            a = _check(r, "A")
            w("- No candidates at or above threshold 0.7 were returned." if a and a["status"] == "ok" else "- Layer A did not complete.")
        w("")
        lc = r.get("layer_c")
        w("**Commercial screening (Layer C, NameScan Sapphire):**")
        if lc:
            w(f"- Scan `{lc['scan_id']}` on {str(lc.get('scan_date') or '')[:10]}: {_or(lc.get('number_of_matches'))} match(es) at "
              f"match rate ≥ {lc.get('match_rate_floor', 75)}; adverse media {lc['adverse_media']}; "
              f"credits {lc['credits_consumed']}{' (reused prior scan)' if lc.get('reused_prior_scan') else ''}"
              f"{' (TEST KEY — no real coverage)' if lc.get('test_mode') else ''}.")
            for m in lc.get("matches") or []:
                w(f"  - candidate: {_or(m.get('name'))} — match rate {_or(m.get('match_rate'))}, "
                  f"category {_or(m.get('category'))}, matched on {_or(m.get('matched_fields'))}; "
                  f"lists: {_official_lists(m)} — assessment: {m.get('assessment', 'unreviewed')}{_disposition_suffix(m)}")
            vendor_media = lc.get("advanced_media_items") or []
            if vendor_media:
                w(f"- Vendor adverse-media items (NameScan, not resolved to the subject by this system): {len(vendor_media)}")
                for it in vendor_media:
                    w(f"  - {_or(it.get('title'))} — {_or(it.get('source_name'))}, "
                      f"{str(it.get('published') or '')[:10] or 'undated'} {_or(it.get('link'), '')}")
            for asc in lc.get("alias_scans") or []:
                w(f"- Alias scan '{asc.get('alias')}': scan {_or(asc.get('scan_id'))}, "
                  f"{_or(asc.get('number_of_matches'))} match(es)")
                for m in asc.get("matches") or []:
                    w(f"  - candidate: {_or(m.get('name'))} — match rate {_or(m.get('match_rate'))}, "
                      f"category {_or(m.get('category'))}, matched on {_or(m.get('matched_fields'))}; "
                      f"lists: {_official_lists(m)} — assessment: {m.get('assessment', 'unreviewed')}{_disposition_suffix(m)}")
        else:
            w("- Not run.")
        w("")
        w("**Adverse media (Layer B, agent web search):**")
        b = _check(r, "B")
        conf = [m for m in r["media_items"] if m["identity"] == "confirmed_subject"]
        if conf:
            for m in conf:
                w(f"- {m['title']} — {m['publisher']}, {m.get('published') or 'undated'}. Legal status: **{m['legal_status']}**. "
                  f"Source type: {m['source_type']}. Corroborator: {m['corroborator']}. {m['url']}")
                if m.get("summary"):
                    w(f"  - {m['summary']}")
        elif b:
            w("- " + NEGATIVE_MEDIA.format(langs=", ".join(b.get("languages", [])), date=b["timestamp"][:10]))
        else:
            w("- Not run.")
        w("")
        ld = r.get("layer_d")
        if ld:
            w("**Registry (Layer D):**")
            for g in ld.get("gleif", []):
                w(f"- GLEIF: LEI `{g['lei']}` {g['legal_name']}, status {_or(g.get('status'))}, "
                  f"jurisdiction {_or(g.get('jurisdiction'))}, "
                  f"registered as {_or(g.get('registered_as'))}, address {', '.join(g['address']['lines'])}, "
                  f"{_or(g['address'].get('city'))}, {_or(g['address'].get('country'))}")
            ch = ld.get("companies_house")
            if ch:
                w(f"- Companies House `{ch['company_number']}`: {ch['company_name']}, {_or(ch.get('status'))}, "
                  f"incorporated {_or(ch.get('incorporated'))}, "
                  f"SIC {', '.join(ch['sic_codes'])}, accounts next due {_or(ch['accounts'].get('next_due'))}"
                  f"{' (OVERDUE)' if ch['accounts']['overdue'] else ''}. Officers: "
                  + "; ".join(f"{o['name']} ({_or(o.get('role'), 'officer')}, {_or(o.get('appointed_on'))}"
                             f"{' – resigned ' + o['resigned_on'] if o.get('resigned_on') else ''})" for o in ch["officers"]))
            for f in ld.get("manual_findings", []):
                fields = ", ".join(f"{k}: {v}" for k, v in f["fields"].items())
                w(f"- {f['registry']} (retrieved {f['retrieved'][:10]}): {fields}. {f['url']}")
            w("")

    w("## Proposed additional subjects\n")
    props = [(r["subject"]["name"], p) for r in records for p in r["proposed_subjects"]]
    if props:
        w("These were surfaced by the checks and are **not screened**. Add them with `screen subject add` if in scope (§7.1).\n")
        for parent, p in props:
            w(f"- {p['name']} ({p['type']}) — {p['reason']}. Source: {p['source']}")
    else:
        w("No subjects proposed.")
    w("")

    w("## Coverage gaps\n")
    gaps = sorted({g for r in records for g in r["coverage_gaps"]})
    for g in gaps or ["No gaps recorded."]:
        w(f"- {g}")
    w("")

    w("## Distribution note\n")
    w("Subject names and identifiers were sent to third-party processors (OpenSanctions"
      + (", NameScan" if any(r.get("layer_c") for r in records) else "") + "). Vendor terms may restrict onward disclosure "
      "of their content and may require that data subjects be notified. No adverse inference should be drawn against any "
      "person or entity solely from appearing as a candidate in this document.")
    w("")
    return "\n".join(L)
