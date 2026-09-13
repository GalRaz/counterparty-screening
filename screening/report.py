"""Markdown summary. Only §3.6/§8 phrasing for negatives; candidates stay labelled as candidates."""
from __future__ import annotations

from screening.case import Case
from screening.lint import SECTION_8_MARKER

NEGATIVE_MEDIA = ("No adverse media items meeting the identity-resolution criteria were returned by the "
                  "queries listed in `queries_run`, searched in {langs} on {date}.")


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
        return f"{lc.get('number_of_matches', '?')} match(es); media {lc.get('adverse_media')}; scan {lc.get('scan_id')}"
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


def _layer_c_ok(r: dict) -> bool:
    c = _check(r, "C")
    return r.get("layer_c") is not None and c is not None and c["status"] == "ok"


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
      "All candidates carry `assessment: unreviewed`; the commissioning party dispositions them.\n")
    if not all_c:
        w(_section_8(records, now) + "\n")
    else:
        w("Commercial screening (NameScan Sapphire) was run for every subject; scan IDs are listed per subject below.\n")

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
                w(f"- `{c['id']}` {c['caption']} — score {c['score']}, topics {', '.join(c['topics']) or 'none'}, "
                  f"datasets {', '.join(c['datasets'])} — assessment: {c['assessment']}{pd}")
        else:
            a = _check(r, "A")
            w("- No candidates at or above threshold 0.7 were returned." if a and a["status"] == "ok" else "- Layer A did not complete.")
        w("")
        lc = r.get("layer_c")
        w("**Commercial screening (Layer C, NameScan Sapphire):**")
        if lc:
            w(f"- Scan `{lc['scan_id']}` on {str(lc.get('scan_date') or '')[:10]}: {lc.get('number_of_matches')} match(es) at "
              f"match rate ≥ {lc.get('match_rate_floor', 75)}; adverse media {lc['adverse_media']}; "
              f"credits {lc['credits_consumed']}{' (reused prior scan)' if lc.get('reused_prior_scan') else ''}"
              f"{' (TEST KEY — no real coverage)' if lc.get('test_mode') else ''}.")
            for m in lc.get("matches", []):
                ent = m.get("person") or m.get("entity") or {}
                w(f"  - candidate: {ent.get('name') or ent.get('primaryName')} — matchRate {m.get('matchRate')}, category {m.get('category')} — assessment: unreviewed")
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
                w(f"- GLEIF: LEI `{g['lei']}` {g['legal_name']}, status {g['status']}, jurisdiction {g['jurisdiction']}, "
                  f"registered as {g['registered_as']}, address {', '.join(g['address']['lines'])}, {g['address']['city']}, {g['address']['country']}")
            ch = ld.get("companies_house")
            if ch:
                w(f"- Companies House `{ch['company_number']}`: {ch['company_name']}, {ch['status']}, incorporated {ch['incorporated']}, "
                  f"SIC {', '.join(ch['sic_codes'])}, accounts next due {ch['accounts']['next_due']}"
                  f"{' (OVERDUE)' if ch['accounts']['overdue'] else ''}. Officers: "
                  + "; ".join(f"{o['name']} ({o['role']}, {o['appointed_on']}{' – resigned ' + o['resigned_on'] if o.get('resigned_on') else ''})" for o in ch["officers"]))
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
        w("None proposed.")
    w("")

    w("## Coverage gaps\n")
    gaps = sorted({g for r in records for g in r["coverage_gaps"]})
    for g in gaps or ["None recorded."]:
        w(f"- {g}")
    w("")

    w("## Distribution note\n")
    w("Subject names and identifiers were sent to third-party processors (OpenSanctions"
      + (", NameScan" if any(r.get("layer_c") for r in records) else "") + "). Vendor terms may restrict onward disclosure "
      "of their content and may require that data subjects be notified. No adverse inference should be drawn against any "
      "person or entity solely from appearing as a candidate in this document.")
    w("")
    return "\n".join(L)
