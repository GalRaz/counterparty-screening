"""Layer B mode selection (§3.0) and query plan (§3.1). The agent runs the searches; this decides what."""
from __future__ import annotations

from dataclasses import dataclass

from screening.subjects import Subject

RISK_TERMS = [
    "fraud", "bribery", "corruption", "money laundering", "sanctions", "embezzlement", "tax evasion",
    "insider trading", "investigation", "indicted", "charged", "convicted", "lawsuit",
    "regulatory action", "fine", "penalty", "insolvency", "liquidation", "disqualified",
]

FAMILY_NAMES = {
    1: "bare identity", 2: "qualified identity", 3: "risk terms", 4: "local-language", 5: "official sources",
}


@dataclass
class Mode:
    mode: str
    reason: str
    families: list[int]
    risk_window_months: int | None


def select(layer_c: dict | None) -> Mode:
    if layer_c is None:
        return Mode("full", "Layer C not run for this subject; Layer B is the only media source", [1, 2, 3, 4, 5], None)
    matches = layer_c.get("number_of_matches") or 0
    media = layer_c.get("adverse_media")
    if matches == 0:
        return Mode("full", "Layer C returned no match, so its media check had nothing to attach to", [1, 2, 3, 4, 5], None)
    if media == "ok":
        return Mode("reduced", "Layer C returned a match with advancedMedia present; Layer B adds local languages and recency only", [3, 4, 5], 24)
    return Mode("full", "Layer C returned a match but its media check did not run (advancedMedia absent or failed)", [1, 2, 3, 4, 5], None)


def _quoted(names: list[str]) -> list[str]:
    return [f'"{n}"' for n in names]


def query_plan(subject: Subject, mode: Mode, languages: list[str]) -> dict:
    names = subject.all_names()
    j = subject.jurisdiction or "the subject's jurisdiction"
    window = f" Restrict to the last {mode.risk_window_months} months." if mode.risk_window_months else ""
    fams = {
        1: {"queries": _quoted(names), "instruction": "Run each quoted name alone."},
        2: {"queries": None, "instruction": "Combine each name with: employer, role, city, company registration number. Only use attributes with a recorded source."},
        3: {"queries": [f"{q} {t}" for q in _quoted(names) for t in RISK_TERMS],
            "instruction": f"Run each name with each risk term.{window}"},
        4: {"queries": None, "instruction": f"Repeat families 1 and 3 in the official language(s) of {j} and in the original script where applicable. Highest-value step for regional subjects."},
        5: {"queries": None, "instruction": f"Check the national company registry, securities regulator, court listings and financial regulator enforcement pages for {j}."},
    }
    return {
        "mode": mode.mode, "reason": mode.reason, "languages": languages,
        "families": [{"family": f, "name": FAMILY_NAMES[f], **fams[f]} for f in mode.families],
    }


def layer_b_check(mode: Mode, queries_run: list[str], languages: list[str], now: str) -> dict:
    return {"layer": "B", "provider": "web_search", "mode": mode.mode, "mode_reason": mode.reason,
            "queries_run": list(queries_run), "languages": list(languages), "status": "ok", "timestamp": now}
