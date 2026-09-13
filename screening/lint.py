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


def _forbidden(text: str, where: str) -> list[Violation]:
    out = []
    for pat in FORBIDDEN_PHRASES:
        for m in re.finditer(pat, text, re.I):
            out.append(Violation("forbidden_phrase", f"forbidden phrase {m.group(0)!r} (§6.1)", where))
    return out


def lint_record(record: dict) -> list[Violation]:
    slug = record["subject"]["slug"]
    vs: list[Violation] = []
    prose = json.dumps({k: record[k] for k in ("coverage_gaps", "proposed_subjects")}, ensure_ascii=False)
    for m in record["media_items"]:
        prose += " " + json.dumps({k: m.get(k) for k in ("summary", "proposed_disposition")}, ensure_ascii=False)
    for w in record["watchlist_candidates"]:
        prose += " " + json.dumps(w.get("proposed_disposition"), ensure_ascii=False)
    vs += _forbidden(prose, f"{slug}/record.json")

    for i, m in enumerate(record["media_items"]):
        if m.get("identity") == "confirmed_subject" and not (m.get("corroborator") or "").strip():
            vs.append(Violation("confirmed_without_corroborator",
                                f"media_items[{i}] is confirmed_subject with no corroborator (§3.2)", f"{slug}/record.json"))

    gaps = " ".join(record["coverage_gaps"]).lower()
    for i, c in enumerate(record["checks_run"]):
        if c.get("status") == "failed" and f"layer {c['layer'].lower()}" not in gaps:
            vs.append(Violation("failed_check_without_gap",
                                f"checks_run[{i}] (layer {c['layer']}) failed but no coverage gap names Layer {c['layer']} (§6.7)",
                                f"{slug}/record.json"))

    if record["media_items"] and not any(c.get("layer") == "B" for c in record["checks_run"]):
        vs.append(Violation("media_without_layer_b_check",
                            "media_items present but no Layer B checks_run entry; run `screen layerb close`", f"{slug}/record.json"))
    return vs


def _layer_c_ok(record: dict) -> bool:
    return record.get("layer_c") is not None and any(
        c.get("layer") == "C" and c.get("status") == "ok" for c in record["checks_run"])


def lint_report(report: str, records: list[dict]) -> list[Violation]:
    vs = _forbidden(report, "summary.md")
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
    return vs


def lint_all(records: list[dict], report: str | None) -> list[Violation]:
    vs: list[Violation] = []
    for r in records:
        vs += lint_record(r)
    if report is not None:
        vs += lint_report(report, records)
    return vs
