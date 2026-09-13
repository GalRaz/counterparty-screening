"""Guardrail linter (§6). Blocks the report when the record or prose says more than the evidence allows."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

FORBIDDEN_PHRASES = [
    r"\bclear\b", r"\bcleared\b", r"\bno risk\b", r"\bno adverse media found\b", r"\bhas no adverse media\b",
]
SECTION_8_MARKER = "This is not a commercial screening product"

# prose word -> legal_status that must exist in the record for the word to be permissible in Findings
LEGAL_WORDS = {
    r"\bconvicted\b": "convicted",
    r"\bfound guilty\b": "convicted",
    r"\bcharged\b": "charged",
    r"\bindicted\b": "charged",
    r"\bacquitted\b": "acquitted_or_dismissed",
    r"\bsettled\b": "settlement_no_admission",
    r"\binsolvent\b": "insolvency",
}


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str
    where: str


def report_section(report: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", report, re.S | re.M)
    return m.group(1) if m else ""


def agent_prose(report: str) -> str:
    """The parts of a report the agent wrote, for §6.1 scanning.

    Vendor-supplied text is data, not prose: a candidate captioned "Clear Channel Holdings Ltd"
    must not read as a verdict. So scan `Bottom line` and `Coverage gaps` in full, and in
    `Findings` and `Limitations` only the narrative lines — the candidate, vendor and unresolved-item
    bullets in both sections start with `- ` or `  - ` and are exempt.
    """
    parts = []
    # Include the preamble (everything before the first ## heading)
    m = re.search(r"^(.*?)^## ", report, re.S | re.M)
    if m:
        parts.append(m.group(1))

    parts += [report_section(report, h) for h in ("Bottom line", "Coverage gaps")]
    for heading in ("Findings", "Limitations"):
        parts += [l for l in report_section(report, heading).splitlines() if not l.lstrip().startswith("- ")]
    return "\n".join(parts)


def _forbidden(text: str, where: str) -> list[Violation]:
    out = []
    for pat in FORBIDDEN_PHRASES:
        for m in re.finditer(pat, text, re.I):
            out.append(Violation("forbidden_phrase", f"forbidden phrase {m.group(0)!r} (§6.1)", where))
    return out


def _agent_written(record: dict) -> str:
    """Every field of the record the agent composed itself — the only places a verdict can be smuggled in.

    Vendor- and web-derived strings are deliberately excluded: `watchlist_candidates[].caption`,
    `layer_c.matches[].name` and `layer_c.advanced_media_items[].title` come back from a vendor, and a
    media item's `title` and `publisher` are transcribed from the page it was taken from, so "Clear
    Channel executive fined" is a citation, not a finding. The agent's own words about those items —
    corroborator, summary, proposed_disposition, the query it ran — are scanned in full.
    """
    parts: list = [record.get("commissioning_party"), record.get("engagement"), record.get("coverage_gaps") or []]
    for m in record.get("media_items") or []:
        parts.append({k: m.get(k) for k in ("corroborator", "summary", "proposed_disposition", "query")})
    for w in record.get("watchlist_candidates") or []:
        parts.append(w.get("proposed_disposition"))
        parts.append(w.get("disposition_note"))
    for m in (record.get("layer_c") or {}).get("matches") or []:
        parts.append(m.get("disposition_note"))
    for p in record.get("proposed_subjects") or []:
        parts.append({k: p.get(k) for k in ("reason", "source")})
    for c in record.get("checks_run") or []:
        if c.get("layer") == "B" and c.get("mode_reason"):
            parts.append(c["mode_reason"])
    for f in ((record.get("layer_d") or {}).get("manual_findings") or []):
        parts.append({"registry": f.get("registry"), "note": f.get("note")})
        parts.append(list((f.get("fields") or {}).values()))
    return json.dumps(parts, ensure_ascii=False)


def lint_record(record: dict) -> list[Violation]:
    slug = record["subject"]["slug"]
    vs: list[Violation] = []
    vs += _forbidden(_agent_written(record), f"{slug}/record.json")

    for i, m in enumerate(record["media_items"]):
        if m.get("identity") == "confirmed_subject" and not (m.get("corroborator") or "").strip():
            vs.append(Violation("confirmed_without_corroborator",
                                f"media_items[{i}] is confirmed_subject with no corroborator (§3.2)", f"{slug}/record.json"))

    for i, w in enumerate(record["watchlist_candidates"]):
        if w.get("assessment") != "unreviewed" and not (w.get("dispositioned_by") or "").strip():
            vs.append(Violation("disposition_without_human",
                                f"watchlist_candidates[{i}] assessment {w.get('assessment')!r} set without a human "
                                "dispositioning it (dispositioned_by) (§5)", f"{slug}/record.json"))
    for i, m in enumerate((record.get("layer_c") or {}).get("matches") or []):
        if m.get("assessment") != "unreviewed" and not (m.get("dispositioned_by") or "").strip():
            vs.append(Violation("disposition_without_human",
                                f"layer_c.matches[{i}] assessment {m.get('assessment')!r} set without a human "
                                "dispositioning it (dispositioned_by) (§5)", f"{slug}/record.json"))

    gaps = " ".join(record["coverage_gaps"]).lower()
    for i, c in enumerate(record["checks_run"]):
        if c.get("status") == "failed" and f"layer {c['layer'].lower()}" not in gaps:
            vs.append(Violation("failed_check_without_gap",
                                f"checks_run[{i}] (layer {c['layer']}) failed but no coverage gap names Layer {c['layer']} (§6.7)",
                                f"{slug}/record.json"))

    layers = [c.get("layer") for c in record["checks_run"]]
    for layer in sorted({l for l in layers if l is not None and layers.count(l) > 1}):
        vs.append(Violation("duplicate_layer_check",
                            f"{layers.count(layer)} checks_run entries for layer {layer}; re-running a layer must "
                            "replace the previous entry, not add to it", f"{slug}/record.json"))

    if record["media_items"] and not any(c.get("layer") == "B" for c in record["checks_run"]):
        vs.append(Violation("media_without_layer_b_check",
                            "media_items present but no Layer B checks_run entry; run `screen layerb close`", f"{slug}/record.json"))
    return vs


def _layer_c_ok(record: dict) -> bool:
    """True only when real commercial screening happened. A test-key scan buys no coverage (§8)."""
    lc = record.get("layer_c")
    return lc is not None and lc.get("test_mode") is not True and any(
        c.get("layer") == "C" and c.get("status") == "ok" for c in record["checks_run"])


def _test_mode(record: dict) -> bool:
    return (record.get("layer_c") or {}).get("test_mode") is True


def lint_report(report: str, records: list[dict]) -> list[Violation]:
    vs = _forbidden(agent_prose(report), "summary.md")
    findings = report_section(report, "Findings")

    for r in records:
        for m in r["media_items"]:
            if m.get("identity") == "possible_subject" and (
                    (m.get("url") and m["url"] in findings) or (m.get("title") and m["title"] in findings)):
                vs.append(Violation("possible_subject_in_findings",
                                    f"possible_subject item {m.get('title')!r} cited in Findings (§3.2)", "summary.md#Findings"))

    present = {m.get("legal_status") for r in records for m in r["media_items"] if m.get("identity") == "confirmed_subject"}
    for pat, status in LEGAL_WORDS.items():
        if re.search(pat, findings, re.I) and status not in present:
            word = re.sub(r"\\b", "", pat)
            vs.append(Violation("legal_status_mismatch",
                                f"Findings uses {word!r} but no media item has legal_status {status!r} (§3.4)",
                                "summary.md#Findings"))

    if not all(_layer_c_ok(r) for r in records) and SECTION_8_MARKER not in report:
        vs.append(Violation("missing_section_8", "Layer C did not succeed for every subject; §8 limitation paragraph required", "summary.md"))
    if any(_test_mode(r) for r in records) and SECTION_8_MARKER not in report:
        vs.append(Violation("test_mode_without_section_8",
                            "Layer C ran with the NameScan test key for at least one subject; that is no coverage, "
                            "so the §8 limitation paragraph is required", "summary.md"))
    return vs


def lint_all(records: list[dict], report: str | None) -> list[Violation]:
    vs: list[Violation] = []
    for r in records:
        vs += lint_record(r)
    if report is not None:
        vs += lint_report(report, records)
    return vs
