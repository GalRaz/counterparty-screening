# Counterparty Screening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python CLI `screen` plus a Claude Code skill `/screen` that screens named people and organisations against OpenSanctions (Layer A), NameScan Sapphire (Layer C), agent-run adverse-media web search (Layer B) and open company registries (Layer D), emitting a JSON record and Markdown summary for human review.

**Architecture:** The CLI owns everything deterministic: vendor API calls, cost guards, SQLite dedup store, the spec §5 JSON record, a guardrail linter, and the Markdown report. The skill owns judgement: subject normalisation, Layer B search and classification, proposing directors as subjects, narrative. The skill only writes through CLI commands. Order per subject is A → C → B, then D for organisations.

**Tech Stack:** Python 3.14 (floor 3.12), venv, `httpx`, stdlib `sqlite3`, `argparse`, `pytest` with `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-09-13-counterparty-screening-design.md` and `screening-tools-spec.md` (vendor spec v7). Section references like §3.0 point at the vendor spec.

## Global Constraints

- Python `>=3.12`. Only runtime dependency: `httpx>=0.27`. Dev dependency: `pytest>=8`.
- Secrets come from env var, else macOS Keychain via `security find-generic-password -s <NAME> -w`. Never written to any file. Names: `OPENSANCTIONS_API_KEY`, `NAMESCAN_API_KEY`, `NAMESCAN_API_KEY_TEST`, `COMPANIES_HOUSE_API_KEY`, `NAMESCAN_MAX_SUBJECTS_RUN` (default `20`).
- All records live under `cases/` (override with `SCREENING_CASES_DIR`). `cases/` is gitignored already.
- OpenSanctions: `POST /match/default?algorithm=best&threshold=0.7&limit=10`, header `Authorization: ApiKey <key>`.
- NameScan: base `https://api.namescan.io/v3.1`, header `api-key: <key>`, `Content-Type: application/json-patch+json`, `matchRate: 75`, `maxResultCount: 100`, `includeAdvancedMedia: true` by default, 60 s timeout with media, 2 retries max, credits at `GET /credits/sapphire`.
- Dedup window 90 days. Vendor text purge 90 days. Record retention 365 days. scanIds kept forever.
- Never populate NameScan `dob`, `country`, `gender`, `idNumber` from anything but a sourced identifier (§7.5).
- Forbidden output language anywhere in record or report: `clear`, `cleared`, `no risk`, `no adverse media found`, `has no adverse media` (§6.1, §3.6).
- `assessment` is always `unreviewed`; `relevance` always `unassessed`; `human_review_required` always `true` (§5).
- Every network failure produces a `checks_run` entry with `status: failed` and a `coverage_gaps` entry (§6.7).
- Never log request bodies (§6.8).
- Commit after every task with the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Do not push.

## File Structure

```
pyproject.toml                     package metadata, pytest config, `screen` entry point
screening/__init__.py              empty
screening/config.py                constants, secret lookup, paths
screening/subjects.py              Subject / Identifier model, slug, dedup key, script detection
screening/record.py                §5 record: construction, enums, structural validation
screening/case.py                  on-disk case layout: meta.json, subject.json, record.json
screening/store.py                 SQLite: scans, vendor_text, records; purge
screening/opensanctions.py         Layer A client
screening/namescan.py              Layer C client with pre-flight, ceiling, dedup, retries
screening/mode.py                  §3.0 Layer B mode selection and §3.1 query plan
screening/registry.py              Layer D: GLEIF, Companies House, manual findings
screening/lint.py                  guardrail rules over record + report
screening/report.py                Markdown summary renderer
screening/cli.py                   argparse entry point
tests/conftest.py                  shared fixtures (tmp cases dir, sample subjects, mock clients)
tests/test_<module>.py             one test file per module
tests/test_cli.py                  end-to-end against mocks
.claude/skills/screen/SKILL.md     orchestration procedure for the agent
README.md                          setup, account creation, usage
```

---

### Task 1: Project scaffold and config

**Files:**
- Create: `pyproject.toml`, `screening/__init__.py`, `screening/config.py`, `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `config.get_secret(name: str) -> str | None`, `config.cases_dir() -> Path`, `config.max_subjects_run() -> int`, and the constants listed in the code below. Every later module imports from `screening.config`.

- [ ] **Step 1: Create the venv and package metadata**

```bash
cd /Users/galraz/work/background-check
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
```

Create `pyproject.toml`:

```toml
[project]
name = "screening"
version = "0.1.0"
description = "Counterparty screening: sanctions, PEP, adverse media, registries. Candidates for human review, never verdicts."
requires-python = ">=3.12"
dependencies = ["httpx>=0.27"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
screen = "screening.cli:main"

[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["screening*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Create empty `screening/__init__.py`.

```bash
.venv/bin/pip install -e '.[dev]'
```
Expected: `Successfully installed httpx ... pytest ... screening`.

- [ ] **Step 2: Write the failing config tests**

`tests/test_config.py`:

```python
import subprocess
from pathlib import Path

from screening import config


def test_env_var_wins_over_keychain(monkeypatch):
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "from-env")
    assert config.get_secret("OPENSANCTIONS_API_KEY") == "from-env"


def test_keychain_used_when_env_missing(monkeypatch):
    monkeypatch.delenv("NAMESCAN_API_KEY", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="from-keychain\n", stderr="")

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    assert config.get_secret("NAMESCAN_API_KEY") == "from-keychain"
    assert calls[0] == ["security", "find-generic-password", "-s", "NAMESCAN_API_KEY", "-w"]


def test_missing_everywhere_returns_none(monkeypatch):
    monkeypatch.delenv("NAMESCAN_API_KEY", raising=False)

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(44, cmd)

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    assert config.get_secret("NAMESCAN_API_KEY") is None


def test_cases_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_CASES_DIR", str(tmp_path / "c"))
    assert config.cases_dir() == tmp_path / "c"


def test_cases_dir_default_is_repo_cases(monkeypatch):
    monkeypatch.delenv("SCREENING_CASES_DIR", raising=False)
    assert config.cases_dir() == config.REPO_ROOT / "cases"


def test_max_subjects_default_and_override(monkeypatch):
    monkeypatch.delenv("NAMESCAN_MAX_SUBJECTS_RUN", raising=False)
    assert config.max_subjects_run() == 20
    monkeypatch.setenv("NAMESCAN_MAX_SUBJECTS_RUN", "5")
    assert config.max_subjects_run() == 5
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'screening.config'`

- [ ] **Step 4: Write config.py**

```python
"""Constants, secret lookup and paths. Nothing here performs network calls."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Vendor endpoints
OPENSANCTIONS_BASE = "https://api.opensanctions.org"
NAMESCAN_BASE = "https://api.namescan.io/v3.1"
GLEIF_BASE = "https://api.gleif.org/api/v1"
COMPANIES_HOUSE_BASE = "https://api.company-information.service.gov.uk"

# Layer A defaults (vendor spec §2, §11)
OS_DATASET = "default"
OS_ALGORITHM = "best"
OS_THRESHOLD = 0.7
OS_LIMIT = 10

# Layer C defaults (vendor spec §7)
NS_MATCH_RATE = 75
NS_MAX_RESULTS = 100
NS_COST_WITH_MEDIA = 1.25
NS_COST_NO_MEDIA = 1.0
NS_LOW_CREDIT_WARN = 20
NS_MAX_RETRIES = 2
NS_TIMEOUT_MEDIA = 60.0
DEFAULT_TIMEOUT = 30.0

# Retention (vendor spec §11)
DEDUP_DAYS = 90
VENDOR_TEXT_DAYS = 90
RECORD_RETENTION_DAYS = 365

SECRET_NAMES = (
    "OPENSANCTIONS_API_KEY",
    "NAMESCAN_API_KEY",
    "NAMESCAN_API_KEY_TEST",
    "COMPANIES_HOUSE_API_KEY",
)


def get_secret(name: str) -> str | None:
    """Environment variable first, then macOS Keychain generic password with service `name`."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", name, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.strip() or None


def cases_dir() -> Path:
    return Path(os.environ.get("SCREENING_CASES_DIR", REPO_ROOT / "cases"))


def max_subjects_run() -> int:
    return int(os.environ.get("NAMESCAN_MAX_SUBJECTS_RUN", "20"))
```

- [ ] **Step 5: Write shared test fixtures**

`tests/conftest.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-09-13T10:00:00+00:00"


@pytest.fixture
def cases_dir(tmp_path, monkeypatch):
    d = tmp_path / "cases"
    monkeypatch.setenv("SCREENING_CASES_DIR", str(d))
    return d


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def mock_client(handler, base_url: str) -> httpx.Client:
    """httpx client whose transport is `handler(request) -> httpx.Response`."""
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url)


def json_response(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)
```

Create empty `tests/__init__.py` (so `from tests.conftest import ...` resolves) and the empty directory `tests/fixtures/` with a `.gitkeep`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml screening/__init__.py screening/config.py tests/__init__.py tests/conftest.py tests/test_config.py tests/fixtures/.gitkeep
git commit -m "feat: project scaffold and config with Keychain secret lookup

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Subject model

**Files:**
- Create: `screening/subjects.py`, `tests/test_subjects.py`

**Interfaces:**
- Produces:
  - `Identifier(kind: str, value: str, source: str)` frozen dataclass. `source` must be non-empty.
  - `Subject(type: str, name: str, aliases: list[str] = [], identifiers: list[Identifier] = [], jurisdiction: str | None = None, role: str | None = None, os_schema: str | None = None)`.
  - `Subject.slug -> str`, `Subject.normalised_key() -> str`, `Subject.all_names() -> list[str]`, `Subject.identifier(kind) -> str | None`, `Subject.identifier_source(kind) -> str | None`, `Subject.has_non_latin_name() -> bool`, `Subject.split_person_name() -> tuple[str, str, str]` (first, middle, last), `Subject.to_dict() -> dict`, `Subject.from_dict(d) -> Subject`, `parse_identifier(text: str) -> Identifier` for the CLI form `kind=value@source`.
  - Identifier kinds used downstream: `dob`, `country`, `gender`, `registration_number`, `tax_number`, `lei`, `uk_company_number`, `passport`, `national_id`. Not enforced as an enum.

- [ ] **Step 1: Write the failing tests**

`tests/test_subjects.py`:

```python
import pytest

from screening.subjects import Identifier, Subject, parse_identifier


def test_identifier_requires_source():
    with pytest.raises(ValueError, match="source"):
        Identifier("dob", "1965", "")


def test_subject_type_must_be_person_or_organization():
    with pytest.raises(ValueError, match="type"):
        Subject(type="company", name="Acme")


def test_subject_name_required():
    with pytest.raises(ValueError, match="name"):
        Subject(type="person", name="  ")


def test_slug_and_key_normalise_whitespace_and_case():
    s = Subject(type="person", name="  Aleksandr   ZACHAROV ")
    assert s.slug == "aleksandr-zacharov"
    assert s.normalised_key() == "person:aleksandr zacharov"


def test_key_includes_dob_for_person_and_registration_for_org():
    p = Subject(type="person", name="Mark Phillips",
                identifiers=[Identifier("dob", "1970", "passport copy")])
    assert p.normalised_key() == "person:mark phillips:dob=1970"
    o = Subject(type="organization", name="Carbon Capital Corporation Pty Ltd",
                identifiers=[Identifier("registration_number", "32 667 478 471", "ASIC extract")])
    assert o.normalised_key() == "organization:carbon capital corporation pty ltd:reg=32667478471"


def test_all_names_includes_aliases_deduped():
    s = Subject(type="person", name="Alexander Ivanov", aliases=["Александр Иванов", "Alexander Ivanov"])
    assert s.all_names() == ["Alexander Ivanov", "Александр Иванов"]


def test_non_latin_detection():
    assert Subject(type="person", name="Александр Иванов").has_non_latin_name()
    assert Subject(type="person", name="Zoë Müller-Łukasz").has_non_latin_name() is False


def test_split_person_name():
    assert Subject(type="person", name="Mark Laurence Allington").split_person_name() == ("Mark", "Laurence", "Allington")
    assert Subject(type="person", name="Madonna").split_person_name() == ("", "", "Madonna")


def test_identifier_lookup_and_source():
    s = Subject(type="person", name="X", identifiers=[Identifier("country", "AU", "user statement")])
    assert s.identifier("country") == "AU"
    assert s.identifier_source("country") == "user statement"
    assert s.identifier("dob") is None


def test_round_trip_dict():
    s = Subject(type="organization", name="Acme Pte Ltd", aliases=["ACME"], jurisdiction="SG",
                role="counterparty", identifiers=[Identifier("registration_number", "2023", "ACRA")])
    assert Subject.from_dict(s.to_dict()) == s


def test_parse_identifier_cli_form():
    assert parse_identifier("dob=1970-01-02@passport copy") == Identifier("dob", "1970-01-02", "passport copy")
    with pytest.raises(ValueError):
        parse_identifier("dob=1970")  # no source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_subjects.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'screening.subjects'`

- [ ] **Step 3: Write subjects.py**

```python
"""Subject model. Identifiers carry provenance; nothing here is ever inferred."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

TYPES = ("person", "organization")


@dataclass(frozen=True)
class Identifier:
    kind: str
    value: str
    source: str

    def __post_init__(self):
        if not self.kind.strip():
            raise ValueError("identifier kind is required")
        if not self.value.strip():
            raise ValueError("identifier value is required")
        if not self.source.strip():
            raise ValueError("identifier source is required (§4: never invent one)")


def parse_identifier(text: str) -> Identifier:
    """CLI form `kind=value@source`."""
    m = re.fullmatch(r"([^=]+)=(.+?)@(.+)", text)
    if not m:
        raise ValueError(f"identifier must be kind=value@source, got {text!r}")
    return Identifier(m.group(1).strip(), m.group(2).strip(), m.group(3).strip())


def _squash(name: str) -> str:
    return " ".join(name.split())


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return s or "subject"


@dataclass
class Subject:
    type: str
    name: str
    aliases: list[str] = field(default_factory=list)
    identifiers: list[Identifier] = field(default_factory=list)
    jurisdiction: str | None = None
    role: str | None = None
    os_schema: str | None = None  # explicit OpenSanctions schema override

    def __post_init__(self):
        if self.type not in TYPES:
            raise ValueError(f"type must be one of {TYPES}")
        self.name = _squash(self.name)
        if not self.name:
            raise ValueError("name is required")
        self.aliases = [_squash(a) for a in self.aliases if _squash(a)]

    @property
    def slug(self) -> str:
        return slugify(self.name)

    def normalised_key(self) -> str:
        key = f"{self.type}:{self.name.lower()}"
        if self.type == "person" and (dob := self.identifier("dob")):
            key += f":dob={dob}"
        if self.type == "organization" and (reg := self.identifier("registration_number")):
            key += f":reg={re.sub(r'\s+', '', reg)}"
        return key

    def all_names(self) -> list[str]:
        seen: list[str] = []
        for n in [self.name, *self.aliases]:
            if n not in seen:
                seen.append(n)
        return seen

    def identifier(self, kind: str) -> str | None:
        for i in self.identifiers:
            if i.kind == kind:
                return i.value
        return None

    def identifier_source(self, kind: str) -> str | None:
        for i in self.identifiers:
            if i.kind == kind:
                return i.source
        return None

    def has_non_latin_name(self) -> bool:
        for n in self.all_names():
            for ch in n:
                if ch.isalpha() and "LATIN" not in unicodedata.name(ch, "LATIN"):
                    return True
        return False

    def split_person_name(self) -> tuple[str, str, str]:
        parts = self.name.split()
        if len(parts) == 1:
            return "", "", parts[0]
        return parts[0], " ".join(parts[1:-1]), parts[-1]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Subject":
        d = dict(d)
        d["identifiers"] = [Identifier(**i) for i in d.get("identifiers", [])]
        return cls(**d)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_subjects.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add screening/subjects.py tests/test_subjects.py
git commit -m "feat: subject model with sourced identifiers and dedup key

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Record construction and validation

**Files:**
- Create: `screening/record.py`, `tests/test_record.py`

**Interfaces:**
- Consumes: `Subject` from Task 2.
- Produces:
  - `new_record(subject: Subject, engagement: str, commissioning_party: str, now: str) -> dict`
  - `add_coverage_gap(record: dict, gap: str) -> None` (idempotent)
  - `new_media_item(**fields) -> dict` applying defaults `relevance="unassessed"`, `proposed_disposition=None`
  - `validate(record: dict) -> list[str]` structural errors, empty list when valid
  - Enums: `IDENTITY`, `LEGAL_STATUS`, `RETRIEVAL_STATUS`, `SOURCE_TYPE`, `CHECK_STATUS = ("ok", "failed", "not_run")`
  - Record shape (all keys always present):
    ```
    engagement, commissioning_party, created_at,
    subject: {name, type, slug, aliases, jurisdiction, role,
              identifiers_supplied: ["kind=value"], identifier_sources: ["kind: source"]},
    checks_run: [], watchlist_candidates: [], media_items: [],
    layer_c: None | dict, layer_d: None | dict, proposed_subjects: [],
    coverage_gaps: [], human_review_required: True
    ```

- [ ] **Step 1: Write the failing tests**

`tests/test_record.py`:

```python
from screening import record as R
from screening.subjects import Identifier, Subject
from tests.conftest import NOW


def make_subject():
    return Subject(type="person", name="Mark Phillips", jurisdiction="AU",
                   identifiers=[Identifier("dob", "1970", "passport copy")])


def test_new_record_shape():
    rec = R.new_record(make_subject(), "GBC-BTN-2026-002", "GMC Authority", NOW)
    assert rec["engagement"] == "GBC-BTN-2026-002"
    assert rec["commissioning_party"] == "GMC Authority"
    assert rec["subject"]["slug"] == "mark-phillips"
    assert rec["subject"]["identifiers_supplied"] == ["dob=1970"]
    assert rec["subject"]["identifier_sources"] == ["dob: passport copy"]
    assert rec["human_review_required"] is True
    assert rec["layer_c"] is None and rec["layer_d"] is None
    assert R.validate(rec) == []


def test_add_coverage_gap_is_idempotent():
    rec = R.new_record(make_subject(), "E", "P", NOW)
    R.add_coverage_gap(rec, "no DOB supplied")
    R.add_coverage_gap(rec, "no DOB supplied")
    assert rec["coverage_gaps"] == ["no DOB supplied"]


def test_media_item_defaults():
    item = R.new_media_item(title="t", publisher="p", published="2025-01-01", url="https://x",
                            retrieved=NOW, retrieval_status="full", identity="possible_subject",
                            corroborator=None, legal_status="allegation", source_type="wire")
    assert item["relevance"] == "unassessed"
    assert item["proposed_disposition"] is None


def test_validate_rejects_reviewed_flags():
    rec = R.new_record(make_subject(), "E", "P", NOW)
    rec["human_review_required"] = False
    rec["watchlist_candidates"].append({"source": "opensanctions", "id": "Q1", "caption": "c",
                                        "score": 0.9, "topics": [], "datasets": [], "assessment": "cleared"})
    errs = R.validate(rec)
    assert any("human_review_required" in e for e in errs)
    assert any("assessment" in e for e in errs)


def test_validate_media_enums_and_corroborator():
    rec = R.new_record(make_subject(), "E", "P", NOW)
    rec["media_items"].append(R.new_media_item(
        title="t", publisher="p", published="2025", url="u", retrieved=NOW,
        retrieval_status="full", identity="confirmed_subject", corroborator=None,
        legal_status="convicted", source_type="primary"))
    rec["media_items"].append(R.new_media_item(
        title="t", publisher="p", published="2025", url="u", retrieved=NOW,
        retrieval_status="pdf", identity="possible_subject", corroborator=None,
        legal_status="rumour", source_type="primary"))
    errs = R.validate(rec)
    assert any("corroborator" in e for e in errs)
    assert any("retrieval_status" in e for e in errs)
    assert any("legal_status" in e for e in errs)


def test_validate_check_status_enum():
    rec = R.new_record(make_subject(), "E", "P", NOW)
    rec["checks_run"].append({"layer": "A", "provider": "opensanctions", "status": "done", "timestamp": NOW})
    assert any("status" in e for e in R.validate(rec))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_record.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Write record.py**

```python
"""The §5 output record. Structural validation only; guardrail language checks live in lint.py."""
from __future__ import annotations

from screening.subjects import Subject

IDENTITY = ("confirmed_subject", "possible_subject", "different_person")
LEGAL_STATUS = (
    "allegation", "investigation_reported", "charged", "convicted", "acquitted_or_dismissed",
    "regulatory_action", "settlement_no_admission", "civil_claim", "insolvency", "commentary_only",
)
RETRIEVAL_STATUS = ("full", "snippet_only", "unavailable")
SOURCE_TYPE = ("primary", "wire", "aggregator", "low_accountability")
CHECK_STATUS = ("ok", "failed", "not_run")
LAYERS = ("A", "B", "C", "D")


def new_record(subject: Subject, engagement: str, commissioning_party: str, now: str) -> dict:
    return {
        "engagement": engagement,
        "commissioning_party": commissioning_party,
        "created_at": now,
        "subject": {
            "name": subject.name,
            "type": subject.type,
            "slug": subject.slug,
            "aliases": list(subject.aliases),
            "jurisdiction": subject.jurisdiction,
            "role": subject.role,
            "identifiers_supplied": [f"{i.kind}={i.value}" for i in subject.identifiers],
            "identifier_sources": [f"{i.kind}: {i.source}" for i in subject.identifiers],
        },
        "checks_run": [],
        "watchlist_candidates": [],
        "media_items": [],
        "layer_c": None,
        "layer_d": None,
        "proposed_subjects": [],
        "coverage_gaps": [],
        "human_review_required": True,
    }


def add_coverage_gap(record: dict, gap: str) -> None:
    if gap not in record["coverage_gaps"]:
        record["coverage_gaps"].append(gap)


def new_media_item(*, title: str, publisher: str, published: str | None, url: str, retrieved: str,
                   retrieval_status: str, identity: str, corroborator: str | None,
                   legal_status: str, source_type: str, query: str | None = None,
                   language: str | None = None, summary: str | None = None,
                   proposed_disposition: str | None = None) -> dict:
    return {
        "title": title, "publisher": publisher, "published": published, "url": url,
        "retrieved": retrieved, "retrieval_status": retrieval_status, "identity": identity,
        "corroborator": corroborator, "legal_status": legal_status, "source_type": source_type,
        "query": query, "language": language, "summary": summary,
        "relevance": "unassessed", "proposed_disposition": proposed_disposition,
    }


def validate(record: dict) -> list[str]:
    errs: list[str] = []
    if record.get("human_review_required") is not True:
        errs.append("human_review_required must be true")
    for i, c in enumerate(record.get("checks_run", [])):
        if c.get("status") not in CHECK_STATUS:
            errs.append(f"checks_run[{i}].status {c.get('status')!r} not in {CHECK_STATUS}")
        if c.get("layer") not in LAYERS:
            errs.append(f"checks_run[{i}].layer {c.get('layer')!r} not in {LAYERS}")
    for i, w in enumerate(record.get("watchlist_candidates", [])):
        if w.get("assessment") != "unreviewed":
            errs.append(f"watchlist_candidates[{i}].assessment must stay 'unreviewed' (§5)")
    for i, m in enumerate(record.get("media_items", [])):
        p = f"media_items[{i}]"
        if m.get("identity") not in IDENTITY:
            errs.append(f"{p}.identity {m.get('identity')!r} not in {IDENTITY}")
        if m.get("legal_status") not in LEGAL_STATUS:
            errs.append(f"{p}.legal_status {m.get('legal_status')!r} not in {LEGAL_STATUS}")
        if m.get("retrieval_status") not in RETRIEVAL_STATUS:
            errs.append(f"{p}.retrieval_status {m.get('retrieval_status')!r} not in {RETRIEVAL_STATUS}")
        if m.get("source_type") not in SOURCE_TYPE:
            errs.append(f"{p}.source_type {m.get('source_type')!r} not in {SOURCE_TYPE}")
        if m.get("relevance") != "unassessed":
            errs.append(f"{p}.relevance must stay 'unassessed' (§5)")
        if m.get("identity") == "confirmed_subject" and not m.get("corroborator"):
            errs.append(f"{p}: confirmed_subject requires a named corroborator (§3.2)")
    return errs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_record.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add screening/record.py tests/test_record.py
git commit -m "feat: §5 record construction and structural validation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Case directory layout

**Files:**
- Create: `screening/case.py`, `tests/test_case.py`

**Interfaces:**
- Consumes: `Subject`, `record.new_record`.
- Produces:
  - `Case.create(cases_dir: Path, engagement: str, commissioning_party: str, now: str) -> Case` (raises `FileExistsError` if present)
  - `Case.load(cases_dir: Path, engagement: str) -> Case` (raises `FileNotFoundError`)
  - `case.root: Path`, `case.engagement`, `case.commissioning_party`
  - `case.add_subject(subject: Subject, now: str) -> str` returns slug; writes `<root>/<slug>/subject.json` and `record.json`; raises `FileExistsError` on duplicate slug
  - `case.slugs() -> list[str]`, `case.subject(slug) -> Subject`, `case.record(slug) -> dict`, `case.save_record(slug, record: dict) -> None`
  - `case.summary_path -> Path` (`<root>/summary.md`)
  - Layout: `cases/<engagement>/meta.json`, `cases/<engagement>/<slug>/{subject.json,record.json}`, `cases/<engagement>/summary.md`

- [ ] **Step 1: Write the failing tests**

`tests/test_case.py`:

```python
import json

import pytest

from screening.case import Case
from screening.subjects import Subject
from tests.conftest import NOW


def test_create_and_load(cases_dir):
    c = Case.create(cases_dir, "GBC-BTN-2026-002", "GMC Authority", NOW)
    assert (cases_dir / "GBC-BTN-2026-002" / "meta.json").exists()
    again = Case.load(cases_dir, "GBC-BTN-2026-002")
    assert again.commissioning_party == "GMC Authority"
    with pytest.raises(FileExistsError):
        Case.create(cases_dir, "GBC-BTN-2026-002", "x", NOW)
    with pytest.raises(FileNotFoundError):
        Case.load(cases_dir, "NOPE")


def test_add_subject_writes_files_and_record(cases_dir):
    c = Case.create(cases_dir, "E1", "P", NOW)
    slug = c.add_subject(Subject(type="organization", name="Acme Pte Ltd", jurisdiction="SG"), NOW)
    assert slug == "acme-pte-ltd"
    assert c.slugs() == ["acme-pte-ltd"]
    assert c.subject(slug).jurisdiction == "SG"
    rec = c.record(slug)
    assert rec["engagement"] == "E1" and rec["subject"]["type"] == "organization"
    rec["coverage_gaps"].append("x")
    c.save_record(slug, rec)
    assert json.loads((c.root / slug / "record.json").read_text())["coverage_gaps"] == ["x"]


def test_duplicate_subject_rejected(cases_dir):
    c = Case.create(cases_dir, "E1", "P", NOW)
    c.add_subject(Subject(type="person", name="Jane Doe"), NOW)
    with pytest.raises(FileExistsError):
        c.add_subject(Subject(type="person", name="jane doe"), NOW)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_case.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Write case.py**

```python
"""On-disk layout for one engagement: cases/<engagement>/{meta.json, <slug>/subject.json, <slug>/record.json, summary.md}."""
from __future__ import annotations

import json
from pathlib import Path

from screening import record as R
from screening.subjects import Subject


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path):
    return json.loads(path.read_text())


class Case:
    def __init__(self, root: Path, meta: dict):
        self.root = root
        self.meta = meta

    @property
    def engagement(self) -> str:
        return self.meta["engagement"]

    @property
    def commissioning_party(self) -> str:
        return self.meta["commissioning_party"]

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.md"

    @classmethod
    def create(cls, cases_dir: Path, engagement: str, commissioning_party: str, now: str) -> "Case":
        root = cases_dir / engagement
        if root.exists():
            raise FileExistsError(f"case {engagement} already exists at {root}")
        root.mkdir(parents=True)
        meta = {"engagement": engagement, "commissioning_party": commissioning_party,
                "created_at": now, "subjects": []}
        _write_json(root / "meta.json", meta)
        return cls(root, meta)

    @classmethod
    def load(cls, cases_dir: Path, engagement: str) -> "Case":
        root = cases_dir / engagement
        if not (root / "meta.json").exists():
            raise FileNotFoundError(f"no case {engagement} under {cases_dir}")
        return cls(root, _read_json(root / "meta.json"))

    def _save_meta(self) -> None:
        _write_json(self.root / "meta.json", self.meta)

    def add_subject(self, subject: Subject, now: str) -> str:
        slug = subject.slug
        d = self.root / slug
        if d.exists():
            raise FileExistsError(f"subject {slug} already in case {self.engagement}")
        d.mkdir()
        _write_json(d / "subject.json", subject.to_dict())
        _write_json(d / "record.json", R.new_record(subject, self.engagement, self.commissioning_party, now))
        self.meta["subjects"].append(slug)
        self._save_meta()
        return slug

    def slugs(self) -> list[str]:
        return list(self.meta["subjects"])

    def subject(self, slug: str) -> Subject:
        return Subject.from_dict(_read_json(self.root / slug / "subject.json"))

    def record(self, slug: str) -> dict:
        return _read_json(self.root / slug / "record.json")

    def save_record(self, slug: str, record: dict) -> None:
        _write_json(self.root / slug / "record.json", record)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_case.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add screening/case.py tests/test_case.py
git commit -m "feat: case directory layout with per-subject records

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: SQLite store with dedup and purge

**Files:**
- Create: `screening/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces:
  - `Store(path: Path)` opens/creates the DB; `store.close()`; usable as context manager.
  - `store.record_scan(key: str, scan_id: str, provider: str, subject_type: str, now: str) -> None`
  - `store.find_scan(key: str, within_days: int, now: str) -> str | None` newest scan_id within window
  - `store.cache_vendor_text(scan_id: str, body: str, now: str) -> None`
  - `store.vendor_text(scan_id: str) -> str | None`
  - `store.index_record(engagement: str, slug: str, path: str, now: str) -> None`
  - `store.purge(now: str, vendor_text_days: int = 90, record_days: int = 365) -> dict` returns `{"vendor_text": n, "records": n}` and deletes the whole subject directory (parent of each indexed `record.json`) for records older than the window, since `subject.json` holds personal data too. Never deletes from `scans`.
  - Timestamps are ISO-8601 strings with offset; comparisons use `datetime.fromisoformat`.

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Write store.py**

```python
"""SQLite store: scanIds (kept forever), cached vendor text (90 days), record index (12 months)."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  key TEXT NOT NULL, scan_id TEXT NOT NULL, provider TEXT NOT NULL,
  subject_type TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scans_key ON scans(key, created_at);
CREATE TABLE IF NOT EXISTS vendor_text (
  scan_id TEXT PRIMARY KEY, body TEXT NOT NULL, cached_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
  engagement TEXT NOT NULL, slug TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (engagement, slug)
);
"""


def _cutoff(now: str, days: int) -> str:
    return (datetime.fromisoformat(now) - timedelta(days=days)).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self.conn.close()

    def record_scan(self, key: str, scan_id: str, provider: str, subject_type: str, now: str) -> None:
        self.conn.execute("INSERT INTO scans VALUES (?,?,?,?,?)", (key, scan_id, provider, subject_type, now))
        self.conn.commit()

    def find_scan(self, key: str, within_days: int, now: str) -> str | None:
        row = self.conn.execute(
            "SELECT scan_id FROM scans WHERE key=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
            (key, _cutoff(now, within_days)),
        ).fetchone()
        return row[0] if row else None

    def cache_vendor_text(self, scan_id: str, body: str, now: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO vendor_text VALUES (?,?,?)", (scan_id, body, now))
        self.conn.commit()

    def vendor_text(self, scan_id: str) -> str | None:
        row = self.conn.execute("SELECT body FROM vendor_text WHERE scan_id=?", (scan_id,)).fetchone()
        return row[0] if row else None

    def index_record(self, engagement: str, slug: str, path: str, now: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (engagement, slug, path, now))
        self.conn.commit()

    def purge(self, now: str, vendor_text_days: int = 90, record_days: int = 365) -> dict:
        vt = self.conn.execute("DELETE FROM vendor_text WHERE cached_at<?", (_cutoff(now, vendor_text_days),)).rowcount
        old = self.conn.execute("SELECT engagement, slug, path FROM records WHERE created_at<?",
                                (_cutoff(now, record_days),)).fetchall()
        for engagement, slug, path in old:
            d = Path(path).parent
            if d.is_dir():
                shutil.rmtree(d)
            self.conn.execute("DELETE FROM records WHERE engagement=? AND slug=?", (engagement, slug))
        self.conn.commit()
        return {"vendor_text": vt, "records": len(old)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add screening/store.py tests/test_store.py
git commit -m "feat: SQLite store with scanId dedup and retention purge

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Layer A — OpenSanctions client

**Files:**
- Create: `screening/opensanctions.py`, `tests/test_opensanctions.py`, `tests/fixtures/opensanctions_match.json`, `tests/fixtures/opensanctions_entity.json`

**Interfaces:**
- Consumes: `Subject`, `config.OS_*`, `record.add_coverage_gap`.
- Produces:
  - `build_query(subject: Subject) -> dict` the body for `/match`.
  - `match(subject: Subject, client: httpx.Client, api_key: str, now: str) -> LayerAResult`
  - `LayerAResult(check: dict, candidates: list[dict], coverage_gaps: list[str])` with `.apply(record: dict) -> None` that appends check, extends `watchlist_candidates`, adds gaps.
  - Candidate shape: `{"source": "opensanctions", "id", "caption", "score", "schema", "topics": [...], "datasets": [...], "entity": <full /entities response or None>, "assessment": "unreviewed", "proposed_disposition": None}`.
  - Check shape: `{"layer": "A", "provider": "opensanctions", "dataset": "default", "algorithm": "best", "threshold": 0.7, "limit": 10, "status": "ok"|"failed", "error": str|None, "timestamp": now, "names_queried": [...]}`.

- [ ] **Step 1: Create fixtures**

`tests/fixtures/opensanctions_match.json`:

```json
{
  "responses": {
    "q1": {
      "status": 200,
      "results": [
        {
          "id": "Q12345",
          "caption": "Aleksandr Zacharov",
          "schema": "Person",
          "score": 0.91,
          "match": true,
          "datasets": ["us_ofac_sdn", "eu_fsf"],
          "properties": {"topics": ["sanction"], "birthDate": ["1965-03-04"], "nationality": ["ru"]}
        },
        {
          "id": "Q99999",
          "caption": "Alexander Zakharov",
          "schema": "Person",
          "score": 0.72,
          "match": true,
          "datasets": ["ru_acf_bribetakers"],
          "properties": {"topics": ["poi"]}
        }
      ]
    }
  }
}
```

`tests/fixtures/opensanctions_entity.json`:

```json
{
  "id": "Q12345",
  "caption": "Aleksandr Zacharov",
  "schema": "Person",
  "datasets": ["us_ofac_sdn", "eu_fsf"],
  "properties": {
    "name": ["Aleksandr Zacharov"],
    "birthDate": ["1965-03-04"],
    "topics": ["sanction"],
    "familyPerson": [{"id": "rel-1", "caption": "Spouse of Aleksandr Zacharov"}]
  }
}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_opensanctions.py`:

```python
import json

import httpx

from screening import opensanctions as OS
from screening.config import OPENSANCTIONS_BASE
from screening.record import new_record
from screening.subjects import Identifier, Subject
from tests.conftest import NOW, json_response, load_fixture, mock_client


def person():
    return Subject(type="person", name="Aleksandr Zacharov", aliases=["Александр Захаров"],
                   identifiers=[Identifier("dob", "1965", "passport copy"),
                                Identifier("country", "RU", "user statement")])


def test_build_query_person_splits_name_and_uses_sourced_attributes():
    q = OS.build_query(person())
    assert q["schema"] == "Person"
    assert q["properties"]["name"] == ["Aleksandr Zacharov", "Александр Захаров"]
    assert q["properties"]["firstName"] == ["Aleksandr"]
    assert q["properties"]["lastName"] == ["Zacharov"]
    assert q["properties"]["birthDate"] == ["1965"]
    assert q["properties"]["country"] == ["RU"]


def test_build_query_org_uses_company_schema_and_registration():
    o = Subject(type="organization", name="Acme Pte Ltd",
                identifiers=[Identifier("registration_number", "202301234K", "ACRA extract")])
    q = OS.build_query(o)
    assert q["schema"] == "Company"
    assert q["properties"]["registrationNumber"] == ["202301234K"]
    assert "firstName" not in q["properties"]


def test_build_query_respects_schema_override():
    o = Subject(type="organization", name="Some Foundation", os_schema="Organization")
    assert OS.build_query(o)["schema"] == "Organization"


def test_match_ok_enriches_and_flags_transliteration():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path == "/match/default":
            assert req.headers["Authorization"] == "ApiKey KEY"
            assert dict(req.url.params) == {"algorithm": "best", "threshold": "0.7", "limit": "10"}
            body = json.loads(req.content)
            assert "q1" in body["queries"]
            return json_response(200, load_fixture("opensanctions_match.json"))
        if req.url.path.startswith("/entities/"):
            return json_response(200, load_fixture("opensanctions_entity.json"))
        raise AssertionError(req.url)

    res = OS.match(person(), mock_client(handler, OPENSANCTIONS_BASE), "KEY", NOW)
    assert res.check["status"] == "ok"
    assert res.check["layer"] == "A"
    assert [c["id"] for c in res.candidates] == ["Q12345", "Q99999"]
    assert res.candidates[0]["topics"] == ["sanction"]
    assert res.candidates[0]["entity"]["properties"]["familyPerson"]
    assert res.candidates[0]["assessment"] == "unreviewed"
    assert "transliterated name — matcher precision reduced" in res.coverage_gaps
    rec = new_record(person(), "E", "P", NOW)
    res.apply(rec)
    assert len(rec["watchlist_candidates"]) == 2 and rec["checks_run"][0]["provider"] == "opensanctions"


def test_match_http_error_is_failed_plus_gap():
    def handler(req):
        return json_response(500, {"detail": "boom"})

    res = OS.match(person(), mock_client(handler, OPENSANCTIONS_BASE), "KEY", NOW)
    assert res.check["status"] == "failed"
    assert res.candidates == []
    assert any("Layer A" in g and "failed" in g for g in res.coverage_gaps)


def test_match_transport_error_is_failed():
    def handler(req):
        raise httpx.ConnectError("no route")

    res = OS.match(person(), mock_client(handler, OPENSANCTIONS_BASE), "KEY", NOW)
    assert res.check["status"] == "failed" and "no route" in res.check["error"]


def test_enrichment_failure_keeps_candidate_and_adds_gap():
    def handler(req):
        if req.url.path == "/match/default":
            return json_response(200, load_fixture("opensanctions_match.json"))
        return json_response(503, {})

    res = OS.match(person(), mock_client(handler, OPENSANCTIONS_BASE), "KEY", NOW)
    assert res.check["status"] == "ok"
    assert res.candidates[0]["entity"] is None
    assert any("enrichment" in g for g in res.coverage_gaps)


def test_no_dob_is_a_coverage_gap():
    s = Subject(type="person", name="Jane Doe")
    res = OS.match(s, mock_client(lambda r: json_response(200, {"responses": {"q1": {"results": []}}}), OPENSANCTIONS_BASE), "KEY", NOW)
    assert "no DOB supplied" in res.coverage_gaps
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_opensanctions.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 4: Write opensanctions.py**

```python
"""Layer A: OpenSanctions /match. Returns candidates, never verdicts."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from screening import config
from screening.record import add_coverage_gap
from screening.subjects import Subject

TRANSLITERATION_GAP = "transliterated name — matcher precision reduced"
NO_DOB_GAP = "no DOB supplied"


@dataclass
class LayerAResult:
    check: dict
    candidates: list[dict] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)

    def apply(self, record: dict) -> None:
        record["checks_run"].append(self.check)
        record["watchlist_candidates"].extend(self.candidates)
        for g in self.coverage_gaps:
            add_coverage_gap(record, g)


def build_query(subject: Subject) -> dict:
    props: dict[str, list[str]] = {"name": subject.all_names()}
    if subject.type == "person":
        schema = subject.os_schema or "Person"
        first, _middle, last = subject.split_person_name()
        if first:
            props["firstName"] = [first]
            props["lastName"] = [last]
        if dob := subject.identifier("dob"):
            props["birthDate"] = [dob]
    else:
        schema = subject.os_schema or "Company"
        if reg := subject.identifier("registration_number"):
            props["registrationNumber"] = [reg]
        if tax := subject.identifier("tax_number"):
            props["taxNumber"] = [tax]
    if country := subject.identifier("country"):
        props["country"] = [country]
    return {"schema": schema, "properties": props}


def _candidate(result: dict) -> dict:
    props = result.get("properties", {})
    return {
        "source": "opensanctions",
        "id": result["id"],
        "caption": result.get("caption"),
        "schema": result.get("schema"),
        "score": result.get("score"),
        "topics": list(props.get("topics", [])),
        "datasets": list(result.get("datasets", [])),
        "entity": None,
        "assessment": "unreviewed",
        "proposed_disposition": None,
    }


def match(subject: Subject, client: httpx.Client, api_key: str, now: str) -> LayerAResult:
    check = {
        "layer": "A", "provider": "opensanctions", "dataset": config.OS_DATASET,
        "algorithm": config.OS_ALGORITHM, "threshold": config.OS_THRESHOLD, "limit": config.OS_LIMIT,
        "status": "ok", "error": None, "timestamp": now, "names_queried": subject.all_names(),
    }
    res = LayerAResult(check=check)
    if subject.type == "person" and not subject.identifier("dob"):
        res.coverage_gaps.append(NO_DOB_GAP)
    if subject.has_non_latin_name():
        res.coverage_gaps.append(TRANSLITERATION_GAP)

    headers = {"Authorization": f"ApiKey {api_key}"}
    params = {"algorithm": config.OS_ALGORITHM, "threshold": config.OS_THRESHOLD, "limit": config.OS_LIMIT}
    try:
        r = client.post(f"/match/{config.OS_DATASET}", params=params, headers=headers,
                        json={"queries": {"q1": build_query(subject)}}, timeout=config.DEFAULT_TIMEOUT)
        r.raise_for_status()
        results = r.json()["responses"]["q1"].get("results", [])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        check["status"] = "failed"
        check["error"] = f"{type(e).__name__}: {e}"
        res.coverage_gaps.append(f"Layer A (opensanctions) call failed: {type(e).__name__}")
        return res

    for result in results:
        cand = _candidate(result)
        try:
            e = client.get(f"/entities/{cand['id']}", headers=headers, timeout=config.DEFAULT_TIMEOUT)
            e.raise_for_status()
            cand["entity"] = e.json()
        except (httpx.HTTPError, ValueError):
            res.coverage_gaps.append(f"Layer A enrichment failed for candidate {cand['id']}")
        res.candidates.append(cand)
    return res
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_opensanctions.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add screening/opensanctions.py tests/test_opensanctions.py tests/fixtures/opensanctions_*.json
git commit -m "feat: Layer A OpenSanctions match client with enrichment

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Layer C — NameScan Sapphire client with cost controls

**Files:**
- Create: `screening/namescan.py`, `tests/test_namescan.py`, `tests/fixtures/namescan_person.json`, `tests/fixtures/namescan_org.json`, `tests/fixtures/namescan_credits.json`

**Interfaces:**
- Consumes: `Subject`, `Store` (Task 5), `config.NS_*`, `record.add_coverage_gap`.
- Produces:
  - `NameScanClient(client: httpx.Client, api_key: str, *, test_mode: bool = False, sleep=time.sleep)`
  - `nc.credits() -> float` balance (raises `httpx.HTTPError` on failure)
  - `build_person_body(subject, include_media) -> dict`, `build_org_body(subject, include_media) -> dict`
  - `nc.scan(subject, *, include_media: bool, now: str, store: Store | None) -> LayerCResult` handles dedup lookup, retries, persistence of scanId, vendor text cache.
  - `run_layer_c(subjects: list[Subject], nc: NameScanClient, store: Store, *, now: str, include_media: bool = True, max_subjects: int) -> list[LayerCResult]` performs pre-flight and ceiling; raises `RunAborted(reason)` before any spend.
  - `LayerCResult(check: dict, layer_c: dict | None, matches: list[dict], media: list[dict], coverage_gaps: list[str], warnings: list[str])` with `.apply(record)`. `warnings` is for stderr only and is never written to the record.
  - `layer_c` shape (§7.7): `{"provider": "namescan", "tier": "sapphire", "scan_id", "match_rate_floor": 75, "number_of_matches", "adverse_media": "ok"|"failed"|"not_requested", "credits_consumed", "reused_prior_scan", "test_mode", "tax_haven_country_results", "sanctioned_country_results", "trigger": "named_subject", "authorised_by": None, "authorised_at": None}`. `authorised_by` is filled by the CLI from the case's commissioning party.
  - Check shape: `{"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "ok"|"failed", "error", "timestamp", "attempts": int}`.
  - `class RunAborted(Exception)`.

- [ ] **Step 1: Create fixtures**

`tests/fixtures/namescan_credits.json`:

```json
{"balance": 78.5, "keyExpiryDate": "2027-09-01T00:00:00", "credits": [{"credits": 100, "remainingCredits": 78.5, "expiryDate": "2027-09-01T00:00:00"}]}
```

`tests/fixtures/namescan_person.json`:

```json
{
  "date": "2026-09-13T10:00:00",
  "scanId": "ps-abc123",
  "numberOfMatches": 1,
  "persons": [
    {
      "matchRate": 88,
      "matchedFields": "Name",
      "category": "PEP",
      "person": {
        "name": "Mark Phillips",
        "officialLists": [{"keyword": "Australian PEP", "isCurrent": true}],
        "roles": [{"title": "Councillor", "since": "2019"}],
        "nationalities": ["Australia"],
        "locations": [{"country": "Australia"}],
        "profileOfInterests": [],
        "linkedIndividuals": [],
        "linkedCompanies": [],
        "sources": ["https://example.org/register"],
        "disqualifiedDirectors": []
      }
    }
  ],
  "advancedMedia": [
    {"title": "Council fined over procurement", "link": "https://news.example/1",
     "sourceName": "Example News", "publishedDate": "2024-06-01T00:00:00",
     "summary": "A council was fined.", "body": "Full text."}
  ],
  "taxHavenCountryResults": [],
  "sanctionedCountryResults": []
}
```

`tests/fixtures/namescan_org.json`:

```json
{
  "date": "2026-09-13T10:00:00",
  "scanId": "os-def456",
  "numberOfMatches": 0,
  "corporates": [],
  "taxHavenCountryResults": [{"country": "Luxembourg"}],
  "sanctionedCountryResults": []
}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_namescan.py`:

```python
import json

import httpx
import pytest

from screening import namescan as NS
from screening.config import NAMESCAN_BASE
from screening.record import new_record
from screening.store import Store
from screening.subjects import Identifier, Subject
from tests.conftest import NOW, json_response, load_fixture, mock_client

LATER = "2026-10-01T10:00:00+00:00"


def person():
    return Subject(type="person", name="Mark Phillips", jurisdiction="AU",
                   identifiers=[Identifier("dob", "1970", "passport copy")])


def org():
    return Subject(type="organization", name="Green Bond Corporation", jurisdiction="LU",
                   identifiers=[Identifier("registration_number", "B123456", "RCS extract")])


def make_handler(person_body=None, org_body=None, credits=78.5, fail_times=0, status_on_fail=503):
    state = {"posts": 0, "fails_left": fail_times, "requests": []}

    def handler(req: httpx.Request):
        state["requests"].append(req)
        assert req.headers["api-key"] == "KEY"
        if req.url.path.endswith("/credits/sapphire"):
            return json_response(200, {**load_fixture("namescan_credits.json"), "balance": credits})
        if req.method == "POST":
            assert req.headers["content-type"] == "application/json-patch+json"
            if state["fails_left"] > 0:
                state["fails_left"] -= 1
                return json_response(status_on_fail, {"message": "degraded"})
            state["posts"] += 1
            if "person-scans" in req.url.path:
                return json_response(200, person_body or load_fixture("namescan_person.json"))
            return json_response(200, org_body or load_fixture("namescan_org.json"))
        if req.method == "GET" and "/sapphire/" in req.url.path:
            body = load_fixture("namescan_person.json") if "person" in req.url.path else load_fixture("namescan_org.json")
            return json_response(200, body)
        raise AssertionError(f"unexpected {req.method} {req.url}")

    return handler, state


def client_for(handler, **kw):
    return NS.NameScanClient(mock_client(handler, NAMESCAN_BASE), "KEY", sleep=lambda s: None, **kw)


def test_person_body_only_uses_sourced_fields():
    s = Subject(type="person", name="Mark Laurence Allington", jurisdiction="GB")
    body = NS.build_person_body(s, include_media=True)
    assert body == {"firstName": "Mark", "middleName": "Laurence", "lastName": "Allington",
                    "exact": False, "matchRate": 75, "maxResultCount": 100, "includeAdvancedMedia": True}
    assert "country" not in body  # jurisdiction is NOT a sourced identifier (§7.5)


def test_person_body_with_sourced_attributes_and_original_name():
    s = Subject(type="person", name="Александр Захаров",
                identifiers=[Identifier("dob", "1965", "passport copy"),
                             Identifier("country", "RU", "passport copy"),
                             Identifier("gender", "male", "passport copy")])
    body = NS.build_person_body(s, include_media=False)
    assert body["originalName"] == "Александр Захаров"
    assert "firstName" not in body
    assert body["dob"] == "1965" and body["country"] == "RU" and body["gender"] == "male"


def test_org_body():
    assert NS.build_org_body(org(), include_media=True) == {
        "name": "Green Bond Corporation", "registrationNumber": "B123456",
        "exact": False, "matchRate": 75, "maxResultCount": 100, "includeAdvancedMedia": True}


def test_scan_person_ok_persists_scan_and_media_ok(tmp_path):
    handler, state = make_handler()
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
        assert res.check["status"] == "ok"
        assert res.layer_c["scan_id"] == "ps-abc123"
        assert res.layer_c["adverse_media"] == "ok"
        assert res.layer_c["credits_consumed"] == 1.25
        assert res.layer_c["reused_prior_scan"] is False
        assert len(res.matches) == 1 and res.matches[0]["category"] == "PEP"
        assert res.media[0]["title"] == "Council fined over procurement"
        assert store.find_scan(person().normalised_key(), 90, NOW) == "ps-abc123"
        assert store.vendor_text("ps-abc123") is not None
        # request timeout must be >= 60s when media requested
        post = [r for r in state["requests"] if r.method == "POST"][0]
        assert post.extensions["timeout"]["read"] >= 60


def test_media_absent_means_failed_not_empty(tmp_path):
    body = {**load_fixture("namescan_person.json")}
    del body["advancedMedia"]
    handler, _ = make_handler(person_body=body)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.layer_c["adverse_media"] == "failed"
    assert res.layer_c["credits_consumed"] == 1.0
    assert any("adverse media" in g.lower() for g in res.coverage_gaps)


def test_media_present_but_empty_is_ok(tmp_path):
    body = {**load_fixture("namescan_person.json"), "advancedMedia": []}
    handler, _ = make_handler(person_body=body)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.layer_c["adverse_media"] == "ok" and res.media == []


def test_media_not_requested(tmp_path):
    handler, _ = make_handler(org_body=load_fixture("namescan_org.json"))
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(org(), include_media=False, now=NOW, store=store)
    assert res.layer_c["adverse_media"] == "not_requested"
    assert res.layer_c["credits_consumed"] == 1.0
    assert res.layer_c["tax_haven_country_results"] == [{"country": "Luxembourg"}]


def test_dedup_reuses_scan_within_90_days(tmp_path):
    handler, state = make_handler()
    with Store(tmp_path / "s.db") as store:
        nc = client_for(handler)
        nc.scan(person(), include_media=True, now=NOW, store=store)
        res = nc.scan(person(), include_media=True, now=LATER, store=store)
    assert state["posts"] == 1
    assert res.layer_c["reused_prior_scan"] is True
    assert res.layer_c["credits_consumed"] == 0.0
    assert res.layer_c["scan_id"] == "ps-abc123"


def test_retries_twice_then_succeeds(tmp_path):
    handler, state = make_handler(fail_times=2)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.check["status"] == "ok" and res.check["attempts"] == 3


def test_gives_up_after_two_retries(tmp_path):
    handler, state = make_handler(fail_times=3)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.check["status"] == "failed" and res.check["attempts"] == 3
    assert res.layer_c is None
    assert any("Layer C" in g for g in res.coverage_gaps)
    assert store.find_scan(person().normalised_key(), 90, NOW) is None


def test_4xx_is_not_retried(tmp_path):
    handler, state = make_handler(fail_times=1, status_on_fail=400)
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler).scan(person(), include_media=True, now=NOW, store=store)
    assert res.check["status"] == "failed" and res.check["attempts"] == 1


def test_test_mode_forces_media_off_and_skips_store(tmp_path):
    handler, state = make_handler(person_body={**load_fixture("namescan_person.json"), "advancedMedia": None})
    with Store(tmp_path / "s.db") as store:
        res = client_for(handler, test_mode=True).scan(person(), include_media=True, now=NOW, store=store)
        assert json.loads([r for r in state["requests"] if r.method == "POST"][0].content)["includeAdvancedMedia"] is False
        assert res.layer_c["test_mode"] is True
        assert res.layer_c["adverse_media"] == "not_requested"
        assert res.layer_c["credits_consumed"] == 0.0
        assert store.find_scan(person().normalised_key(), 90, NOW) is None


def test_run_layer_c_preflight_aborts_when_credits_short(tmp_path):
    handler, state = make_handler(credits=2.0)
    with Store(tmp_path / "s.db") as store:
        with pytest.raises(NS.RunAborted, match="credits"):
            NS.run_layer_c([person(), org()], client_for(handler), store, now=NOW, max_subjects=20)
    assert state["posts"] == 0


def test_run_layer_c_ceiling_aborts(tmp_path):
    handler, state = make_handler()
    with Store(tmp_path / "s.db") as store:
        with pytest.raises(NS.RunAborted, match="ceiling"):
            NS.run_layer_c([person(), org()], client_for(handler), store, now=NOW, max_subjects=1)
    assert state["posts"] == 0


def test_run_layer_c_skips_credit_cost_for_dedup_hits(tmp_path):
    handler, state = make_handler(credits=1.3)
    with Store(tmp_path / "s.db") as store:
        store.record_scan(person().normalised_key(), "ps-abc123", "namescan", "person", NOW)
        results = NS.run_layer_c([person(), org()], client_for(handler), store, now=NOW, max_subjects=20)
    assert [r.layer_c["reused_prior_scan"] for r in results] == [True, False]
    assert state["posts"] == 1


def test_run_layer_c_low_credit_warning(tmp_path):
    handler, _ = make_handler(credits=15)
    with Store(tmp_path / "s.db") as store:
        results = NS.run_layer_c([org()], client_for(handler), store, now=NOW, max_subjects=20)
    assert any("below 20" in w for w in results[0].warnings)


def test_run_layer_c_credits_endpoint_failure_aborts(tmp_path):
    def handler(req):
        return json_response(500, {})
    with Store(tmp_path / "s.db") as store:
        with pytest.raises(NS.RunAborted, match="balance"):
            NS.run_layer_c([org()], client_for(handler), store, now=NOW, max_subjects=20)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_namescan.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 4: Write namescan.py**

```python
"""Layer C: NameScan Sapphire. Every path here exists to make sure money is spent deliberately (§7)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from screening import config
from screening.record import add_coverage_gap
from screening.store import Store
from screening.subjects import Subject

HEADERS_CT = "application/json-patch+json"


class RunAborted(Exception):
    """Raised before any credit is spent."""


@dataclass
class LayerCResult:
    check: dict
    layer_c: dict | None = None
    matches: list[dict] = field(default_factory=list)
    media: list[dict] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def apply(self, record: dict) -> None:
        record["checks_run"].append(self.check)
        if self.layer_c is not None:
            record["layer_c"] = {**self.layer_c, "matches": self.matches, "advanced_media_items": self.media}
        for g in self.coverage_gaps:
            add_coverage_gap(record, g)


def _common(include_media: bool) -> dict:
    return {"exact": False, "matchRate": config.NS_MATCH_RATE,
            "maxResultCount": config.NS_MAX_RESULTS, "includeAdvancedMedia": include_media}


def build_person_body(subject: Subject, include_media: bool) -> dict:
    body: dict = {}
    if subject.has_non_latin_name():
        body["originalName"] = subject.name
    else:
        first, middle, last = subject.split_person_name()
        if first:
            body["firstName"] = first
            if middle:
                body["middleName"] = middle
            body["lastName"] = last
        else:
            body["originalName"] = subject.name
    # §7.5: only sourced identifiers. A wrong value silently suppresses true hits.
    for kind, key in (("dob", "dob"), ("country", "country"), ("gender", "gender"),
                      ("national_id", "idNumber"), ("passport", "idNumber")):
        if (v := subject.identifier(kind)) and key not in body:
            body[key] = v
    body.update(_common(include_media))
    return body


def build_org_body(subject: Subject, include_media: bool) -> dict:
    body: dict = {"name": subject.name}
    if country := subject.identifier("country"):
        body["country"] = country
    if reg := subject.identifier("registration_number"):
        body["registrationNumber"] = reg
    body.update(_common(include_media))
    return body


class NameScanClient:
    def __init__(self, client: httpx.Client, api_key: str, *, test_mode: bool = False, sleep=time.sleep):
        self.client = client
        self.api_key = api_key
        self.test_mode = test_mode
        self.sleep = sleep

    def _headers(self) -> dict:
        return {"api-key": self.api_key, "Content-Type": HEADERS_CT, "Accept": "application/json"}

    def credits(self) -> float:
        r = self.client.get("/credits/sapphire", headers=self._headers(), timeout=config.DEFAULT_TIMEOUT)
        r.raise_for_status()
        return float(r.json()["balance"])

    def _path(self, subject: Subject) -> str:
        return "/person-scans/sapphire" if subject.type == "person" else "/organisation-scans/sapphire"

    def _post_with_retries(self, path: str, body: dict, timeout: float) -> tuple[dict | None, int, str | None]:
        attempts = 0
        err: str | None = None
        while attempts <= config.NS_MAX_RETRIES:
            attempts += 1
            try:
                r = self.client.post(path, headers=self._headers(), content=json.dumps(body), timeout=timeout)
                if 400 <= r.status_code < 500:
                    return None, attempts, f"HTTP {r.status_code}"
                r.raise_for_status()
                return r.json(), attempts, None
            except (httpx.HTTPError, ValueError) as e:
                err = f"{type(e).__name__}: {e}"
                if attempts <= config.NS_MAX_RETRIES:
                    self.sleep(2 ** attempts)
        return None, attempts, err

    def scan(self, subject: Subject, *, include_media: bool, now: str, store: Store | None) -> LayerCResult:
        check = {"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "ok",
                 "error": None, "timestamp": now, "attempts": 0}
        res = LayerCResult(check=check)
        path = self._path(subject)
        key = subject.normalised_key()
        media_requested = include_media and not self.test_mode
        if include_media and self.test_mode:
            res.warnings.append("test key does not support adverse media; includeAdvancedMedia forced false")

        reused = False
        data = None
        if store is not None and not self.test_mode:
            if prior := store.find_scan(key, config.DEDUP_DAYS, now):
                try:
                    r = self.client.get(f"{path}/{prior}", headers=self._headers(), timeout=config.DEFAULT_TIMEOUT)
                    r.raise_for_status()
                    data, reused = r.json(), True
                    check["attempts"] = 1
                except (httpx.HTTPError, ValueError) as e:
                    res.warnings.append(f"could not re-fetch prior scan {prior}: {type(e).__name__}; running a new scan")

        if data is None:
            body = (build_person_body if subject.type == "person" else build_org_body)(subject, media_requested)
            timeout = config.NS_TIMEOUT_MEDIA if media_requested else config.DEFAULT_TIMEOUT
            data, attempts, err = self._post_with_retries(path, body, timeout)
            check["attempts"] = attempts
            if data is None:
                check["status"] = "failed"
                check["error"] = err
                res.coverage_gaps.append(f"Layer C (namescan) call failed after {attempts} attempt(s): {err}")
                return res

        scan_id = data.get("scanId")
        if media_requested and not reused:
            if "advancedMedia" in data and data["advancedMedia"] is not None:
                adverse_media, cost = "ok", config.NS_COST_WITH_MEDIA
            else:
                adverse_media, cost = "failed", config.NS_COST_NO_MEDIA
                res.coverage_gaps.append("Layer C adverse media check did not run (advancedMedia absent) — Layer B must run in full mode")
        elif reused:
            adverse_media = "ok" if data.get("advancedMedia") is not None else "not_requested"
            cost = 0.0
        else:
            adverse_media, cost = "not_requested", (0.0 if self.test_mode else config.NS_COST_NO_MEDIA)
        if self.test_mode:
            cost = 0.0

        raw_matches = data.get("persons") if subject.type == "person" else data.get("corporates")
        res.matches = list(raw_matches or [])
        res.media = list(data.get("advancedMedia") or [])
        res.layer_c = {
            "provider": "namescan", "tier": "sapphire", "scan_id": scan_id,
            "match_rate_floor": config.NS_MATCH_RATE, "number_of_matches": data.get("numberOfMatches"),
            "adverse_media": adverse_media, "credits_consumed": cost, "reused_prior_scan": reused,
            "test_mode": self.test_mode, "trigger": "named_subject",
            "authorised_by": None, "authorised_at": None,
            "tax_haven_country_results": data.get("taxHavenCountryResults", []),
            "sanctioned_country_results": data.get("sanctionedCountryResults", []),
            "scan_date": data.get("date"),
        }
        if store is not None and scan_id and not self.test_mode:
            if not reused:
                store.record_scan(key, scan_id, "namescan", subject.type, now)
            store.cache_vendor_text(scan_id, json.dumps(data, ensure_ascii=False), now)
        return res


def run_layer_c(subjects: list[Subject], nc: NameScanClient, store: Store, *, now: str,
                include_media: bool = True, max_subjects: int) -> list[LayerCResult]:
    if len(subjects) > max_subjects:
        raise RunAborted(f"ceiling: {len(subjects)} subjects exceeds NAMESCAN_MAX_SUBJECTS_RUN={max_subjects}")
    warnings: list[str] = []
    if not nc.test_mode:
        try:
            balance = nc.credits()
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise RunAborted(f"could not read credit balance: {type(e).__name__}: {e}") from e
        unit = config.NS_COST_WITH_MEDIA if include_media else config.NS_COST_NO_MEDIA
        to_scan = [s for s in subjects if store.find_scan(s.normalised_key(), config.DEDUP_DAYS, now) is None]
        cost = unit * len(to_scan)
        if balance < cost:
            raise RunAborted(f"insufficient credits: balance {balance} < run cost {cost} for {len(to_scan)} new scan(s)")
        if balance < config.NS_LOW_CREDIT_WARN:
            warnings.append(f"credit balance {balance} is below 20 — top up soon")
    results = []
    for s in subjects:
        r = nc.scan(s, include_media=include_media, now=now, store=store)
        r.warnings.extend(warnings)
        results.append(r)
    return results
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_namescan.py -v`
Expected: 17 passed. If `test_scan_person_ok_persists_scan_and_media_ok` fails on the timeout assertion, inspect `post.extensions["timeout"]`; httpx stores it as a dict with `connect/read/write/pool` keys.

- [ ] **Step 6: Commit**

```bash
git add screening/namescan.py tests/test_namescan.py tests/fixtures/namescan_*.json
git commit -m "feat: Layer C NameScan client with pre-flight, dedup, ceiling and bounded retries

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Layer B mode selection and query plan

**Files:**
- Create: `screening/mode.py`, `tests/test_mode.py`

**Interfaces:**
- Consumes: the `layer_c` dict from a record (or `None`), `Subject`.
- Produces:
  - `Mode(mode: "full"|"reduced", reason: str, families: list[int], risk_window_months: int | None)`
  - `select(layer_c: dict | None) -> Mode` implementing the §3.0 table.
  - `RISK_TERMS: list[str]` the §3.1.3 list, verbatim.
  - `query_plan(subject: Subject, mode: Mode, languages: list[str]) -> dict` with keys `mode`, `reason`, `languages`, `families: [{"family": int, "name": str, "queries": [str] | None, "instruction": str}]`. Families 1 and 3 get concrete English query strings; 2, 4, 5 get instructions because they need context the CLI lacks.
  - `layer_b_check(mode: Mode, queries_run: list[str], languages: list[str], now: str) -> dict` the §5 `checks_run` entry: `{"layer": "B", "provider": "web_search", "mode", "mode_reason", "queries_run", "languages", "status": "ok", "timestamp"}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_mode.py`:

```python
from screening import mode as M
from screening.subjects import Subject
from tests.conftest import NOW


def lc(**kw):
    base = {"number_of_matches": 0, "adverse_media": "not_requested"}
    return {**base, **kw}


def test_no_layer_c_is_full():
    m = M.select(None)
    assert m.mode == "full" and m.families == [1, 2, 3, 4, 5] and "not run" in m.reason


def test_zero_matches_is_full():
    assert M.select(lc(number_of_matches=0, adverse_media="ok")).mode == "full"


def test_match_with_media_is_reduced():
    m = M.select(lc(number_of_matches=2, adverse_media="ok"))
    assert m.mode == "reduced" and m.families == [3, 4, 5] and m.risk_window_months == 24


def test_match_with_media_failed_is_full():
    m = M.select(lc(number_of_matches=2, adverse_media="failed"))
    assert m.mode == "full" and "did not run" in m.reason


def test_match_with_media_not_requested_is_full():
    assert M.select(lc(number_of_matches=2, adverse_media="not_requested")).mode == "full"


def test_query_plan_full_builds_concrete_queries():
    s = Subject(type="person", name="Mark Phillips", aliases=["M. Phillips"], jurisdiction="AU")
    plan = M.query_plan(s, M.select(None), ["en"])
    fam = {f["family"]: f for f in plan["families"]}
    assert '"Mark Phillips"' in fam[1]["queries"] and '"M. Phillips"' in fam[1]["queries"]
    assert '"Mark Phillips" fraud' in fam[3]["queries"]
    assert len(fam[3]["queries"]) == len(M.RISK_TERMS) * 2
    assert fam[2]["queries"] is None and "employer" in fam[2]["instruction"]
    assert fam[4]["queries"] is None and "AU" in fam[4]["instruction"]
    assert fam[5]["queries"] is None


def test_query_plan_reduced_limits_families_and_window():
    s = Subject(type="person", name="Mark Phillips")
    plan = M.query_plan(s, M.select(lc(number_of_matches=1, adverse_media="ok")), ["en", "dz"])
    assert [f["family"] for f in plan["families"]] == [3, 4, 5]
    assert "24 months" in [f for f in plan["families"] if f["family"] == 3][0]["instruction"]
    assert plan["languages"] == ["en", "dz"]


def test_layer_b_check_shape():
    c = M.layer_b_check(M.select(None), ['"x"'], ["en"], NOW)
    assert c == {"layer": "B", "provider": "web_search", "mode": "full", "mode_reason": c["mode_reason"],
                 "queries_run": ['"x"'], "languages": ["en"], "status": "ok", "timestamp": NOW}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mode.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Write mode.py**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mode.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add screening/mode.py tests/test_mode.py
git commit -m "feat: Layer B mode selection and query plan per §3.0/§3.1

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Layer D — registry lookups (GLEIF, Companies House, manual)

**Files:**
- Create: `screening/registry.py`, `tests/test_registry.py`, `tests/fixtures/gleif_records.json`, `tests/fixtures/ch_profile.json`, `tests/fixtures/ch_officers.json`, `tests/fixtures/ch_search.json`

**Interfaces:**
- Consumes: `Subject`, `config.GLEIF_BASE`, `config.COMPANIES_HOUSE_BASE`, `record.add_coverage_gap`.
- Produces:
  - `gleif_lookup(subject, client: httpx.Client) -> list[dict]` normalised GLEIF records: `{"lei", "legal_name", "status", "jurisdiction", "registered_as", "address": {"lines": [...], "city", "country"}, "registration_status"}`. Looks up by `lei` identifier if present, else by exact legal name (`filter[entity.legalName]`).
  - `companies_house_lookup(subject, client, api_key) -> dict | None` normalised: `{"company_number", "company_name", "status", "incorporated", "type", "sic_codes", "accounts": {"next_due", "overdue"}, "registered_office", "officers": [{"name", "role", "appointed_on", "resigned_on", "nationality", "country_of_residence"}]}`. Uses `uk_company_number` identifier if present; else `/search/companies?q=<name>` and accepts only a case-insensitive exact title match; otherwise returns `None`.
  - `run_layer_d(subject, *, gleif_client, ch_client, ch_api_key: str | None, now: str) -> LayerDResult`
  - `LayerDResult(check: dict, layer_d: dict, proposed_subjects: list[dict], coverage_gaps: list[str])` with `.apply(record)`.
  - `layer_d` shape: `{"gleif": [...], "companies_house": {...}|None, "manual_findings": []}`. `proposed_subjects` items: `{"name", "type": "person", "reason", "source"}` for active officers (no `resigned_on`).
  - Check shape: `{"layer": "D", "provider": "registries", "sources": ["gleif", "companies_house"], "status", "error", "timestamp"}`. Status is `ok` if at least one source succeeded, `failed` if all attempted sources failed, `not_run` for persons.
  - `manual_finding(**fields) -> dict` for `screen registry add`: `{"registry", "url", "retrieved", "fields": {...}, "note"}`.

- [ ] **Step 1: Create fixtures**

`tests/fixtures/gleif_records.json`:

```json
{
  "meta": {"pagination": {"total": 1}},
  "data": [
    {
      "type": "lei-records",
      "id": "984500765B652F3C6A05",
      "attributes": {
        "lei": "984500765B652F3C6A05",
        "entity": {
          "legalName": {"name": "CARBON CAPITAL CORPORATION PTY LTD", "language": "en"},
          "legalAddress": {"addressLines": ["LEVEL 12, 60 CARRINGTON STREET"], "city": "SYDNEY", "country": "AU", "postalCode": "2000"},
          "registeredAs": "667 478 471",
          "jurisdiction": "AU",
          "status": "ACTIVE"
        },
        "registration": {"status": "ISSUED", "validatedAt": {"id": "RA000014"}}
      }
    }
  ]
}
```

`tests/fixtures/ch_search.json`:

```json
{"items": [
  {"title": "JEARRARD ENERGY RESOURCES LTD", "company_number": "13647702", "company_status": "active"},
  {"title": "JEARRARD ENERGY HOLDINGS LTD", "company_number": "99999999", "company_status": "active"}
]}
```

`tests/fixtures/ch_profile.json`:

```json
{
  "company_name": "JEARRARD ENERGY RESOURCES LTD",
  "company_number": "13647702",
  "company_status": "active",
  "date_of_creation": "2021-09-28",
  "type": "ltd",
  "sic_codes": ["71121"],
  "accounts": {"next_due": "2026-06-30", "overdue": true, "last_accounts": {"type": "micro-entity", "made_up_to": "2024-09-30"}},
  "registered_office_address": {"address_line_1": "1 Marybrook Street", "locality": "Berkeley", "postal_code": "GL13 9AA"}
}
```

`tests/fixtures/ch_officers.json`:

```json
{"items": [
  {"name": "ALLINGTON, Mark Laurence", "officer_role": "director", "appointed_on": "2021-09-28", "nationality": "British", "country_of_residence": "England"},
  {"name": "OBERHOLZER, Jan", "officer_role": "director", "appointed_on": "2024-02-15", "resigned_on": "2025-01-20", "nationality": "South African"}
]}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_registry.py`:

```python
import base64

import httpx

from screening import registry as RG
from screening.config import COMPANIES_HOUSE_BASE, GLEIF_BASE
from screening.record import new_record
from screening.subjects import Identifier, Subject
from tests.conftest import NOW, json_response, load_fixture, mock_client


def ccc():
    return Subject(type="organization", name="Carbon Capital Corporation Pty Ltd", jurisdiction="AU",
                   identifiers=[Identifier("lei", "984500765B652F3C6A05", "GLEIF search by user")])


def jer():
    return Subject(type="organization", name="Jearrard Energy Resources Ltd", jurisdiction="GB")


def gleif_handler(req: httpx.Request):
    assert req.url.path == "/api/v1/lei-records"
    return json_response(200, load_fixture("gleif_records.json"))


def ch_handler(req: httpx.Request):
    auth = req.headers["Authorization"]
    assert auth == "Basic " + base64.b64encode(b"CHKEY:").decode()
    if req.url.path == "/search/companies":
        return json_response(200, load_fixture("ch_search.json"))
    if req.url.path == "/company/13647702":
        return json_response(200, load_fixture("ch_profile.json"))
    if req.url.path == "/company/13647702/officers":
        return json_response(200, load_fixture("ch_officers.json"))
    raise AssertionError(req.url)


def test_gleif_by_lei_filter_and_normalisation():
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        return gleif_handler(req)

    recs = RG.gleif_lookup(ccc(), mock_client(handler, GLEIF_BASE))
    assert seen["params"] == {"filter[lei]": "984500765B652F3C6A05"}
    assert recs == [{
        "lei": "984500765B652F3C6A05", "legal_name": "CARBON CAPITAL CORPORATION PTY LTD",
        "status": "ACTIVE", "jurisdiction": "AU", "registered_as": "667 478 471",
        "address": {"lines": ["LEVEL 12, 60 CARRINGTON STREET"], "city": "SYDNEY", "country": "AU"},
        "registration_status": "ISSUED"}]


def test_gleif_by_name_when_no_lei():
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params)
        return gleif_handler(req)

    RG.gleif_lookup(jer(), mock_client(handler, GLEIF_BASE))
    assert seen["params"] == {"filter[entity.legalName]": "Jearrard Energy Resources Ltd", "page[size]": "10"}


def test_companies_house_search_requires_exact_title():
    rec = RG.companies_house_lookup(jer(), mock_client(ch_handler, COMPANIES_HOUSE_BASE), "CHKEY")
    assert rec["company_number"] == "13647702"
    assert rec["status"] == "active" and rec["incorporated"] == "2021-09-28"
    assert rec["accounts"] == {"next_due": "2026-06-30", "overdue": True}
    assert rec["sic_codes"] == ["71121"]
    assert [o["name"] for o in rec["officers"]] == ["ALLINGTON, Mark Laurence", "OBERHOLZER, Jan"]
    assert rec["officers"][1]["resigned_on"] == "2025-01-20"


def test_companies_house_no_exact_match_returns_none():
    s = Subject(type="organization", name="Jearrard Energy", jurisdiction="GB")
    assert RG.companies_house_lookup(s, mock_client(ch_handler, COMPANIES_HOUSE_BASE), "CHKEY") is None


def test_companies_house_uses_number_identifier_directly():
    s = Subject(type="organization", name="whatever", identifiers=[Identifier("uk_company_number", "13647702", "user")])
    paths = []

    def handler(req):
        paths.append(req.url.path)
        return ch_handler(req)

    RG.companies_house_lookup(s, mock_client(handler, COMPANIES_HOUSE_BASE), "CHKEY")
    assert "/search/companies" not in paths


def test_run_layer_d_proposes_active_officers_only():
    res = RG.run_layer_d(jer(), gleif_client=mock_client(lambda r: json_response(200, {"data": []}), GLEIF_BASE),
                         ch_client=mock_client(ch_handler, COMPANIES_HOUSE_BASE), ch_api_key="CHKEY", now=NOW)
    assert res.check["status"] == "ok"
    assert res.proposed_subjects == [{"name": "Mark Laurence Allington", "type": "person",
                                      "reason": "active director of Jearrard Energy Resources Ltd",
                                      "source": "Companies House officers list, company 13647702"}]
    rec = new_record(jer(), "E", "P", NOW)
    res.apply(rec)
    assert rec["layer_d"]["companies_house"]["company_number"] == "13647702"
    assert rec["proposed_subjects"][0]["name"] == "Mark Laurence Allington"


def test_run_layer_d_person_is_not_run():
    res = RG.run_layer_d(Subject(type="person", name="X"), gleif_client=None, ch_client=None, ch_api_key=None, now=NOW)
    assert res.check["status"] == "not_run" and res.layer_d is None


def test_run_layer_d_all_sources_failed():
    boom = mock_client(lambda r: json_response(500, {}), GLEIF_BASE)
    res = RG.run_layer_d(ccc(), gleif_client=boom, ch_client=None, ch_api_key=None, now=NOW)
    assert res.check["status"] == "failed"
    assert any("gleif" in g.lower() for g in res.coverage_gaps)
    assert any("companies house" in g.lower() and "no api key" in g.lower() for g in res.coverage_gaps)


def test_non_gb_org_skips_companies_house_without_gap():
    res = RG.run_layer_d(ccc(), gleif_client=mock_client(gleif_handler, GLEIF_BASE), ch_client=None, ch_api_key="CHKEY", now=NOW)
    assert res.check["status"] == "ok"
    assert res.layer_d["companies_house"] is None
    assert not any("companies house" in g.lower() for g in res.coverage_gaps)


def test_manual_finding_shape():
    f = RG.manual_finding(registry="ACRA", url="https://www.bizfile.gov.sg/x", retrieved=NOW,
                          fields={"status": "Live", "incorporated": "2023-04-01"}, note="looked up by agent")
    assert f == {"registry": "ACRA", "url": "https://www.bizfile.gov.sg/x", "retrieved": NOW,
                 "fields": {"status": "Live", "incorporated": "2023-04-01"}, "note": "looked up by agent"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_registry.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 4: Write registry.py**

```python
"""Layer D: open registry lookups for organisations. Officers found are proposed, never auto-screened."""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from screening import config
from screening.record import add_coverage_gap
from screening.subjects import Subject


@dataclass
class LayerDResult:
    check: dict
    layer_d: dict | None = None
    proposed_subjects: list[dict] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)

    def apply(self, record: dict) -> None:
        record["checks_run"].append(self.check)
        if self.layer_d is not None:
            record["layer_d"] = self.layer_d
        for p in self.proposed_subjects:
            if p not in record["proposed_subjects"]:
                record["proposed_subjects"].append(p)
        for g in self.coverage_gaps:
            add_coverage_gap(record, g)


def manual_finding(*, registry: str, url: str, retrieved: str, fields: dict, note: str | None = None) -> dict:
    return {"registry": registry, "url": url, "retrieved": retrieved, "fields": fields, "note": note}


def _gleif_normalise(item: dict) -> dict:
    a = item["attributes"]
    e = a.get("entity", {})
    addr = e.get("legalAddress", {})
    return {
        "lei": a.get("lei"),
        "legal_name": (e.get("legalName") or {}).get("name"),
        "status": e.get("status"),
        "jurisdiction": e.get("jurisdiction"),
        "registered_as": e.get("registeredAs"),
        "address": {"lines": list(addr.get("addressLines", [])), "city": addr.get("city"), "country": addr.get("country")},
        "registration_status": (a.get("registration") or {}).get("status"),
    }


def gleif_lookup(subject: Subject, client: httpx.Client) -> list[dict]:
    if lei := subject.identifier("lei"):
        params = {"filter[lei]": lei}
    else:
        params = {"filter[entity.legalName]": subject.name, "page[size]": "10"}
    r = client.get("/lei-records", params=params, timeout=config.DEFAULT_TIMEOUT)
    r.raise_for_status()
    return [_gleif_normalise(i) for i in r.json().get("data", [])]


def _ch_auth(api_key: str) -> httpx.BasicAuth:
    return httpx.BasicAuth(api_key, "")


def companies_house_lookup(subject: Subject, client: httpx.Client, api_key: str) -> dict | None:
    auth = _ch_auth(api_key)
    number = subject.identifier("uk_company_number")
    if not number:
        r = client.get("/search/companies", params={"q": subject.name}, auth=auth, timeout=config.DEFAULT_TIMEOUT)
        r.raise_for_status()
        want = subject.name.casefold()
        hits = [i for i in r.json().get("items", []) if i.get("title", "").casefold() == want]
        if not hits:
            return None
        number = hits[0]["company_number"]
    p = client.get(f"/company/{number}", auth=auth, timeout=config.DEFAULT_TIMEOUT)
    p.raise_for_status()
    prof = p.json()
    o = client.get(f"/company/{number}/officers", auth=auth, timeout=config.DEFAULT_TIMEOUT)
    o.raise_for_status()
    officers = [{
        "name": i.get("name"), "role": i.get("officer_role"), "appointed_on": i.get("appointed_on"),
        "resigned_on": i.get("resigned_on"), "nationality": i.get("nationality"),
        "country_of_residence": i.get("country_of_residence"),
    } for i in o.json().get("items", [])]
    acc = prof.get("accounts") or {}
    return {
        "company_number": prof.get("company_number"), "company_name": prof.get("company_name"),
        "status": prof.get("company_status"), "incorporated": prof.get("date_of_creation"),
        "type": prof.get("type"), "sic_codes": list(prof.get("sic_codes", [])),
        "accounts": {"next_due": acc.get("next_due"), "overdue": acc.get("overdue")},
        "registered_office": prof.get("registered_office_address"),
        "officers": officers,
    }


def _officer_display_name(raw: str) -> str:
    """Companies House gives 'SURNAME, Given Names'. Return 'Given Names Surname'."""
    if "," in raw:
        sur, given = [p.strip() for p in raw.split(",", 1)]
        return f"{given} {sur.title()}"
    return raw


def run_layer_d(subject: Subject, *, gleif_client: httpx.Client | None, ch_client: httpx.Client | None,
                ch_api_key: str | None, now: str) -> LayerDResult:
    check = {"layer": "D", "provider": "registries", "sources": [], "status": "ok", "error": None, "timestamp": now}
    res = LayerDResult(check=check)
    if subject.type != "organization":
        check["status"] = "not_run"
        return res

    layer_d: dict = {"gleif": [], "companies_house": None, "manual_findings": []}
    attempted = 0
    succeeded = 0
    errors: list[str] = []

    if gleif_client is not None:
        attempted += 1
        check["sources"].append("gleif")
        try:
            layer_d["gleif"] = gleif_lookup(subject, gleif_client)
            succeeded += 1
        except (httpx.HTTPError, KeyError, ValueError) as e:
            errors.append(f"gleif: {type(e).__name__}")
            res.coverage_gaps.append(f"Layer D GLEIF lookup failed: {type(e).__name__}")

    is_gb = (subject.jurisdiction or "").upper() in ("GB", "UK")
    if is_gb and ch_client is not None and ch_api_key:
        attempted += 1
        check["sources"].append("companies_house")
        try:
            ch = companies_house_lookup(subject, ch_client, ch_api_key)
            layer_d["companies_house"] = ch
            succeeded += 1
            if ch:
                for off in ch["officers"]:
                    if off.get("resigned_on"):
                        continue
                    res.proposed_subjects.append({
                        "name": _officer_display_name(off["name"]), "type": "person",
                        "reason": f"active {off.get('role') or 'officer'} of {subject.name}",
                        "source": f"Companies House officers list, company {ch['company_number']}",
                    })
        except (httpx.HTTPError, KeyError, ValueError) as e:
            errors.append(f"companies_house: {type(e).__name__}")
            res.coverage_gaps.append(f"Layer D Companies House lookup failed: {type(e).__name__}")
    elif not ch_api_key:
        res.coverage_gaps.append("Layer D Companies House not queried: no API key configured")

    res.layer_d = layer_d
    if attempted and succeeded == 0:
        check["status"] = "failed"
        check["error"] = "; ".join(errors)
    return res
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_registry.py -v`
Expected: 10 passed.

- [ ] **Step 6: Commit**

```bash
git add screening/registry.py tests/test_registry.py tests/fixtures/gleif_records.json tests/fixtures/ch_*.json
git commit -m "feat: Layer D registry lookups via GLEIF and Companies House with proposed officers

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Guardrail linter

**Files:**
- Create: `screening/lint.py`, `tests/test_lint.py`

**Interfaces:**
- Consumes: record dicts (Task 3) and the report text (Task 11). The report's section headings are fixed by Task 11: `## Bottom line`, `## What was screened`, `## Limitations`, `## Findings`, `## Proposed additional subjects`, `## Coverage gaps`, `## Distribution note`.
- Produces:
  - `Violation(rule: str, message: str, where: str)` dataclass.
  - `lint_record(record: dict) -> list[Violation]`
  - `lint_report(report: str, records: list[dict]) -> list[Violation]`
  - `lint_all(records: list[dict], report: str | None) -> list[Violation]`
  - `FORBIDDEN_PHRASES`, `SECTION_8_MARKER = "This is not a commercial screening product"`.
  - `report_section(report: str, heading: str) -> str` returns the text under `## <heading>` up to the next `## `.

- [ ] **Step 1: Write the failing tests**

`tests/test_lint.py`:

```python
from screening import lint as L
from screening.record import new_media_item, new_record
from screening.subjects import Subject
from tests.conftest import NOW


def rec(**over):
    r = new_record(Subject(type="person", name="Mark Phillips"), "E", "P", NOW)
    r.update(over)
    return r


def item(**over):
    base = dict(title="Council fined", publisher="Example News", published="2024-06-01", url="https://news.example/1",
                retrieved=NOW, retrieval_status="full", identity="possible_subject", corroborator=None,
                legal_status="regulatory_action", source_type="wire")
    base.update(over)
    return new_media_item(**base)


def rules(vs):
    return sorted({v.rule for v in vs})


def test_forbidden_phrases_in_record_and_report():
    r = rec()
    r["coverage_gaps"].append("subject appears clear")
    assert "forbidden_phrase" in rules(L.lint_record(r))
    assert "forbidden_phrase" in rules(L.lint_report("## Bottom line\nNo risk found.\n", [rec()]))
    assert "forbidden_phrase" in rules(L.lint_report("## Bottom line\nNo adverse media found.\n", [rec()]))
    assert "forbidden_phrase" not in rules(L.lint_report("## Bottom line\nThe register clearly lists it.\n", [rec()]))


def test_confirmed_requires_corroborator():
    r = rec(media_items=[item(identity="confirmed_subject", corroborator=None)])
    assert "confirmed_without_corroborator" in rules(L.lint_record(r))
    r = rec(media_items=[item(identity="confirmed_subject", corroborator="employer: Acme")])
    assert "confirmed_without_corroborator" not in rules(L.lint_record(r))


def test_failed_check_needs_gap():
    r = rec(checks_run=[{"layer": "A", "provider": "opensanctions", "status": "failed", "timestamp": NOW}])
    assert "failed_check_without_gap" in rules(L.lint_record(r))
    r["coverage_gaps"].append("Layer A (opensanctions) call failed: ConnectError")
    assert "failed_check_without_gap" not in rules(L.lint_record(r))


def test_media_items_need_layer_b_close():
    r = rec(media_items=[item()])
    assert "media_without_layer_b_check" in rules(L.lint_record(r))
    r["checks_run"].append({"layer": "B", "provider": "web_search", "mode": "full", "status": "ok",
                            "queries_run": ['"Mark Phillips"'], "languages": ["en"], "timestamp": NOW})
    assert "media_without_layer_b_check" not in rules(L.lint_record(r))


def test_possible_subject_not_in_findings():
    r = rec(media_items=[item(title="Council fined", url="https://news.example/1")])
    report = "## Findings\nCouncil fined (Example News) https://news.example/1\n## Coverage gaps\n"
    assert "possible_subject_in_findings" in rules(L.lint_report(report, [r]))
    report = "## Findings\nnothing\n## Coverage gaps\nhttps://news.example/1\n"
    assert "possible_subject_in_findings" not in rules(L.lint_report(report, [r]))


def test_legal_status_words_must_match_record():
    r = rec(media_items=[item(identity="confirmed_subject", corroborator="x", legal_status="allegation")])
    report = "## Findings\nMr Phillips was convicted of fraud.\n"
    assert "legal_status_mismatch" in rules(L.lint_report(report, [r]))
    report = "## Findings\nMr Phillips was accused of fraud (allegation).\n"
    assert "legal_status_mismatch" not in rules(L.lint_report(report, [r]))


def test_section_8_required_when_layer_c_missing():
    assert "missing_section_8" in rules(L.lint_report("## Bottom line\nx\n", [rec()]))
    ok = f"## Bottom line\n{L.SECTION_8_MARKER}.\n"
    assert "missing_section_8" not in rules(L.lint_report(ok, [rec()]))
    with_c = rec(layer_c={"scan_id": "ps-1", "adverse_media": "ok"})
    with_c["checks_run"].append({"layer": "C", "provider": "namescan", "status": "ok", "timestamp": NOW})
    assert "missing_section_8" not in rules(L.lint_report("## Bottom line\nx\n", [with_c]))


def test_report_section_extraction():
    txt = "# T\n## Findings\na\nb\n## Coverage gaps\nc\n"
    assert L.report_section(txt, "Findings") == "a\nb\n"
    assert L.report_section(txt, "Nope") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_lint.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Write lint.py**

```python
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
        if m.get("identity") == "confirmed_subject" and not m.get("corroborator"):
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

    present = {m.get("legal_status") for r in records for m in r["media_items"]}
    for pat, status in LEGAL_WORDS.items():
        if re.search(pat, findings, re.I) and status not in present:
            vs.append(Violation("legal_status_mismatch",
                                f"Findings uses {pat.strip(chr(92) + 'b')!r} but no media item has legal_status {status!r} (§3.4)",
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_lint.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add screening/lint.py tests/test_lint.py
git commit -m "feat: guardrail linter for record and report language

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Markdown report renderer

**Files:**
- Create: `screening/report.py`, `tests/test_report.py`

**Interfaces:**
- Consumes: `Case` (Task 4), records, `lint.SECTION_8_MARKER`.
- Produces: `render(case: Case, records: list[dict], now: str) -> str`. Section headings exactly: `## Bottom line`, `## What was screened`, `## Limitations`, `## Findings`, `## Proposed additional subjects`, `## Coverage gaps`, `## Distribution note`. Findings cites only `confirmed_subject` media items and lists watchlist candidates and Layer C matches as candidates with provenance labels. `possible_subject` items are listed under `## Limitations` as "unresolved media items" with title and URL. Uses §3.6 phrasing for empty results and the §8 paragraph when any subject lacks a successful Layer C.

- [ ] **Step 1: Write the failing tests**

`tests/test_report.py`:

```python
from screening import lint as L
from screening import report as RP
from screening.case import Case
from screening.record import new_media_item
from screening.subjects import Subject
from tests.conftest import NOW


def build_case(cases_dir):
    c = Case.create(cases_dir, "GBC-BTN-2026-002", "Gelephu Mindfulness City Authority", NOW)
    c.add_subject(Subject(type="organization", name="Acme Pte Ltd", jurisdiction="SG"), NOW)
    c.add_subject(Subject(type="person", name="Mark Phillips", jurisdiction="AU"), NOW)
    return c


def test_render_has_sections_and_section_8_when_no_layer_c(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    for r in recs:
        r["checks_run"].append({"layer": "A", "provider": "opensanctions", "status": "ok", "timestamp": NOW,
                                "dataset": "default", "algorithm": "best", "threshold": 0.7, "limit": 10})
        r["checks_run"].append({"layer": "B", "provider": "web_search", "mode": "full", "mode_reason": "x",
                                "queries_run": ['"Mark Phillips"'], "languages": ["en"], "status": "ok", "timestamp": NOW})
    out = RP.render(c, recs, NOW)
    for h in ("Bottom line", "What was screened", "Limitations", "Findings", "Proposed additional subjects",
              "Coverage gaps", "Distribution note"):
        assert f"## {h}" in out
    assert L.SECTION_8_MARKER in out
    assert "GBC-BTN-2026-002" in out and "Gelephu Mindfulness City Authority" in out
    assert "No adverse media items meeting the identity-resolution criteria were returned" in out
    assert L.lint_report(out, recs) == []


def test_findings_cite_confirmed_only_and_possible_go_to_limitations(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    mp = recs[1]
    mp["checks_run"].append({"layer": "B", "provider": "web_search", "mode": "full", "mode_reason": "x",
                             "queries_run": [], "languages": ["en"], "status": "ok", "timestamp": NOW})
    mp["media_items"].append(new_media_item(
        title="Councillor fined", publisher="Example News", published="2024-06-01", url="https://news.example/1",
        retrieved=NOW, retrieval_status="full", identity="confirmed_subject", corroborator="role: councillor, Brisbane",
        legal_status="regulatory_action", source_type="wire", summary="A fine was imposed."))
    mp["media_items"].append(new_media_item(
        title="Different Mark Phillips arrested", publisher="Tabloid", published="2020-01-01", url="https://tab.example/2",
        retrieved=NOW, retrieval_status="snippet_only", identity="possible_subject", corroborator=None,
        legal_status="charged", source_type="low_accountability"))
    out = RP.render(c, recs, NOW)
    findings = L.report_section(out, "Findings")
    limits = L.report_section(out, "Limitations")
    assert "https://news.example/1" in findings and "regulatory_action" in findings
    assert "https://tab.example/2" not in findings and "https://tab.example/2" in limits
    assert L.lint_report(out, recs) == []


def test_layer_c_everywhere_drops_section_8_and_cites_scan_ids(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    for i, r in enumerate(recs):
        r["checks_run"].append({"layer": "C", "provider": "namescan", "tier": "sapphire", "status": "ok", "timestamp": NOW, "attempts": 1})
        r["layer_c"] = {"provider": "namescan", "tier": "sapphire", "scan_id": f"scan-{i}", "number_of_matches": i,
                        "adverse_media": "ok", "credits_consumed": 1.25, "reused_prior_scan": False, "matches": [],
                        "advanced_media_items": [], "test_mode": False, "scan_date": NOW}
    out = RP.render(c, recs, NOW)
    assert L.SECTION_8_MARKER not in out
    assert "scan-0" in out and "scan-1" in out


def test_watchlist_candidates_listed_as_unreviewed(cases_dir):
    c = build_case(cases_dir)
    recs = [c.record(s) for s in c.slugs()]
    recs[1]["watchlist_candidates"].append({"source": "opensanctions", "id": "Q1", "caption": "Mark Philips", "score": 0.74,
                                           "topics": ["role.pep"], "datasets": ["au_pep"], "entity": None,
                                           "assessment": "unreviewed", "proposed_disposition": "likely different person: different state"})
    out = RP.render(c, recs, NOW)
    assert "Q1" in out and "0.74" in out and "unreviewed" in out and "likely different person" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_report.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Write report.py**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_report.py -v`
Expected: 4 passed. If a lint assertion fails, the fix is in the report text, not the linter.

- [ ] **Step 5: Commit**

```bash
git add screening/report.py tests/test_report.py
git commit -m "feat: Markdown summary renderer with §3.6/§8 phrasing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: CLI

**Files:**
- Create: `screening/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: every module above.
- Produces: `main(argv: list[str] | None = None) -> int`. Exit codes: `0` ok, `1` usage or data error, `2` lint failure, `3` run aborted (credits/ceiling), `4` missing API key.
- Module-level factories the tests monkeypatch: `make_os_client() -> httpx.Client`, `make_ns_client() -> httpx.Client`, `make_gleif_client() -> httpx.Client`, `make_ch_client() -> httpx.Client`, `now() -> str`.
- `run C` fills `layer_c.authorised_by = case.commissioning_party` and `authorised_at = now()`.
- `store_path() -> Path` is `config.cases_dir() / "store.db"`.
- Commands exactly as in the design doc §3 `cli.py` block, plus `--languages` on `mode` and `--force` on `report` (write despite lint, printing violations; the file then carries a `LINT FAILED` banner).

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Write cli.py**

```python
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
            raise SystemExit(f"no subject {only!r} in {case.engagement}")
        return [only]
    return case.slugs()


def _read_stdin_json():
    data = json.load(sys.stdin)
    return data if isinstance(data, list) else [data]


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
    case = _case(a.engagement)
    slugs = _slugs(case, a.subject)
    return {"A": _run_a, "C": _run_c, "D": _run_d}[a.layer](case, slugs, a)


def _run_a(case: Case, slugs: list[str], a) -> int:
    key = config.get_secret("OPENSANCTIONS_API_KEY")
    if not key:
        err("OPENSANCTIONS_API_KEY not found in env or Keychain")
        for slug in slugs:
            rec = case.record(slug)
            rec["checks_run"].append({"layer": "A", "provider": "opensanctions", "status": "not_run", "error": "no API key", "timestamp": now()})
            R.add_coverage_gap(rec, "Layer A (opensanctions) not run: no API key")
            case.save_record(slug, rec)
        return EXIT_NOKEY
    client = make_os_client()
    for slug in slugs:
        res = OS.match(case.subject(slug), client, key, now())
        rec = case.record(slug)
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
        try:
            item = R.new_media_item(**{k: raw.get(k) for k in (
                "title", "publisher", "published", "url", "retrieved", "retrieval_status", "identity",
                "corroborator", "legal_status", "source_type", "query", "language", "summary", "proposed_disposition")})
        except TypeError as e:
            err(f"bad media item: {e}")
            return EXIT_ERR
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


def cmd_layerb_close(a) -> int:
    case = _case(a.engagement)
    rec = case.record(a.slug)
    queries = [q for q in Path(a.queries_file).read_text().splitlines() if q.strip()]
    langs = [l.strip() for l in a.languages.split(",")]
    m = M.select(rec.get("layer_c"))
    if m.mode != a.mode:
        err(f"warning: declared mode {a.mode} differs from computed mode {m.mode} ({m.reason}); recording declared mode")
        m = M.Mode(a.mode, f"declared by agent; computed was {m.mode}: {m.reason}", m.families, m.risk_window_months)
    rec["checks_run"] = [c for c in rec["checks_run"] if c["layer"] != "B"]
    rec["checks_run"].append(M.layer_b_check(m, queries, langs, now()))
    case.save_record(a.slug, rec)
    print(f"{a.slug}: Layer B closed, {len(queries)} query string(s), languages {','.join(langs)}")
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
    bal = NS.NameScanClient(make_ns_client(), key, test_mode=a.test).credits()
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

    lb = sp.add_parser("layerb").add_subparsers(dest="sub", required=True)
    lc = lb.add_parser("close"); lc.add_argument("engagement"); lc.add_argument("slug")
    lc.add_argument("--mode", required=True, choices=["full", "reduced"]); lc.add_argument("--queries-file", required=True)
    lc.add_argument("--languages", required=True); lc.set_defaults(fn=cmd_layerb_close)

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
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (FileNotFoundError, FileExistsError, SystemExit) as e:
        if isinstance(e, SystemExit) and isinstance(e.code, int):
            return e.code
        err(str(e))
        return EXIT_ERR
    except httpx.HTTPError as e:
        err(f"network error: {type(e).__name__}: {e}")
        return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: 8 passed. Then run the whole suite: `.venv/bin/pytest -q` — expected all green.

- [ ] **Step 5: Confirm the console script works**

```bash
.venv/bin/screen --help
```
Expected: usage text listing `case, subject, run, mode, media, layerb, registry, record, report, lint, credits, purge`.

- [ ] **Step 6: Commit**

```bash
git add screening/cli.py tests/test_cli.py
git commit -m "feat: screen CLI wiring all layers, lint-gated report

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 13: Skill and README

**Files:**
- Create: `.claude/skills/screen/SKILL.md`, `README.md`

**Interfaces:**
- Consumes: the CLI surface from Task 12 exactly as named there.
- Produces: the orchestration procedure the agent follows when the user types `/screen ...`.

- [ ] **Step 1: Write the skill**

`.claude/skills/screen/SKILL.md`:

````markdown
---
name: screen
description: Counterparty screening of named people and organisations (sanctions, PEP, adverse media, registries) via the `screen` CLI. Use when the user asks to background-check, screen, vet, or run due diligence on a person or company they are considering working with. Produces candidate matches for human review, never verdicts.
---

# /screen — counterparty screening

You orchestrate the `screen` CLI (`.venv/bin/screen`, run from the repo root). The CLI does vendor calls,
cost control, storage and the report. You do judgement: normalising subjects, Layer B web search and
classification, proposing directors, and reviewing the output. You never write records except through
CLI commands. Read `screening-tools-spec.md` §3, §6 and §7.1 before your first run in a session.

## Hard rules

1. **Named subjects only.** Screen exactly the people and organisations the user nominated, plus directors,
   officers and parent entities of a nominated organisation *once the user has added them*. Never screen
   someone who merely appears in a thread, article, or result. Propose; do not add (§7.1).
2. **Never invent identifiers.** Every `--id` needs `kind=value@source` where source is a document or
   statement the user supplied. If you do not know the DOB, omit it (§4, §7.5).
3. **Never output verdicts.** No "clear", "cleared", "no risk", "no adverse media found". The linter will
   block the report, but do not write them in chat either (§6.1).
4. **An article is not about the subject until a corroborating attribute beyond the name matches**:
   employer, role, registration number, city/country, DOB, a confirmed associate, or a photograph.
   Otherwise it is `possible_subject`. Never upgrade on plausibility or narrative fit (§3.2).
5. **Never summarise an article you did not fetch.** Fetch it with WebFetch or mark
   `retrieval_status: snippet_only` (§3.3).
6. **Preserve legal status verbatim.** Use exactly one of: `allegation`, `investigation_reported`,
   `charged`, `convicted`, `acquitted_or_dismissed`, `regulatory_action`, `settlement_no_admission`,
   `civil_claim`, `insolvency`, `commentary_only`. Check for later developments before recording (§3.4).
7. **Scope limit.** Record only what bears on counterparty risk. No family, health, relationships, home
   address, political or religious affiliation, even if a search surfaces it (§3.7).
8. **A failed call is a coverage gap, not a clean result** (§6.7).

## Procedure

### 0. Set up

```bash
.venv/bin/screen case new <ENGAGEMENT-ID> --commissioning-party "<who is commissioning this>"
```

Engagement IDs look like `GBC-BTN-2026-002`. Ask the user for the commissioning party if not stated.

### 1. Normalise and add subjects

For each nominated subject, decide `person` or `organization`, collect every name variant and script
you were given, and record identifiers **with sources**:

```bash
.venv/bin/screen subject add <ID> --type person --name "Mark Phillips" --alias "M. Phillips" \
  --jurisdiction AU --role "director, Carbon Capital Corporation" --id "dob=1970@passport copy in email 2026-08-01"
.venv/bin/screen subject add <ID> --type organization --name "Carbon Capital Corporation Pty Ltd" \
  --jurisdiction AU --id "lei=984500765B652F3C6A05@GLEIF search" --id "registration_number=32 667 478 471@ASIC extract"
```

Identifier kinds: `dob`, `country`, `gender`, `registration_number`, `tax_number`, `lei`,
`uk_company_number`, `passport`, `national_id`. Jurisdiction is *not* an identifier and is never sent to
NameScan as `country`.

### 2. Layer A

```bash
.venv/bin/screen run A <ID>
```

### 3. Layer C

```bash
.venv/bin/screen credits                 # optional: see balance first
.venv/bin/screen run C <ID>              # production key, adverse media on
.venv/bin/screen run C <ID> --test       # development only: no credits, no media, no history
```

If it exits 3 (credits or ceiling) or 4 (no key), report that to the user and continue; the record
already carries the gap and the report will carry the §8 paragraph.

### 4. Layer B — you run this

For each subject:

```bash
.venv/bin/screen mode <ID> <slug> --languages en,<jurisdiction languages>
```

Languages: English + official language(s) of the subject's jurisdiction + the regional lingua franca.
Run the families the plan lists using WebSearch. Keep every query string you actually ran in a file.
For each result that might concern the subject:

1. WebFetch the article. If it fails, `retrieval_status: unavailable`; if you only have the snippet, `snippet_only`.
2. Decide identity per rule 4. Name the corroborator.
3. Decide legal status per rule 6. Search the matter for later developments.
4. Decide source type: `primary` (regulator, court, registry, company statement), `wire`, `aggregator`, `low_accountability`.
5. Pipe it in:

```bash
echo '{"title":"...","publisher":"...","published":"2024-06-01","url":"https://...","retrieval_status":"full",
"identity":"possible_subject","corroborator":null,"legal_status":"allegation","source_type":"wire",
"query":"\"Mark Phillips\" fraud","language":"en","summary":"<your own words, one or two sentences>"}' \
  | .venv/bin/screen media add <ID> <slug>
```

Items you positively excluded (`different_person`) may be added too; they document the work.
When done with a subject:

```bash
.venv/bin/screen layerb close <ID> <slug> --mode <full|reduced> --queries-file <path> --languages en,dz
```

Do this for every subject, including those with zero items. A subject with no Layer B entry is a gap.

### 5. Layer D (organisations)

```bash
.venv/bin/screen run D <ID>
```

For jurisdictions without an API (ASIC, ACRA, RCS Luxembourg, Bhutan, others) look the company up in
the registry's public search using the browser tools, then record what you saw:

```bash
echo '{"registry":"ACRA","url":"https://www.bizfile.gov.sg/...","fields":{"status":"Live","incorporated":"2023-04-01","uen":"..."},"note":"public search, no extract purchased"}' \
  | .venv/bin/screen registry add <ID> <slug>
```

`run D` prints **proposed subjects** (active officers). Present them to the user with the reason.
Only after the user says which to include, add them with `subject add` and repeat steps 2 to 4 for them.

### 6. Report

```bash
.venv/bin/screen report <ID>
```

If lint fails, fix the *record* (wrong identity class, missing corroborator, missing `layerb close`),
not the linter. Then read `cases/<ID>/summary.md` and give the user a short account in chat: what was
screened, how many candidates per subject, what remains unresolved, and what identifiers would sharpen a
re-screen. Use the report's own phrasing for negatives. Do not add a verdict.

## Exit codes

`0` ok · `1` usage/data error · `2` lint failure · `3` run aborted before spend (credits/ceiling) · `4` API key missing
````

- [ ] **Step 2: Write the README**

`README.md`:

````markdown
# Counterparty screening

Screens named people and organisations against sanctions/PEP lists (OpenSanctions), a commercial
screening provider (NameScan Sapphire), agent-run adverse-media web search, and open company registries
(GLEIF, UK Companies House). Emits candidate matches and candidate media items for human review.
**It never emits a verdict.** See `screening-tools-spec.md` for the rules and
`docs/superpowers/specs/2026-09-13-counterparty-screening-design.md` for the design.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

### Accounts and keys

Create accounts under the entity that will own and pay for them (spec §9). Store each key in the macOS
Keychain under the exact service name; the CLI reads them with `security find-generic-password -s <NAME> -w`.

| Key | Where | Notes |
|---|---|---|
| `OPENSANCTIONS_API_KEY` | opensanctions.org | Free trial key on signup. Businesses need a data licence before production. |
| `NAMESCAN_API_KEY` | namescan.io account profile | Production key. Buy the **100-scan Sapphire package (~$250)**, not single scans. |
| `NAMESCAN_API_KEY_TEST` | namescan.io account profile | Test key: no credits, no scan history, no adverse media. Use for development. |
| `COMPANIES_HOUSE_API_KEY` | developer.company-information.service.gov.uk | Free. Only needed for GB subjects. |

```bash
security add-generic-password -s OPENSANCTIONS_API_KEY -a screening -w   # prompts for the value
```

Optional: `NAMESCAN_MAX_SUBJECTS_RUN` (default 20) caps subjects per Layer C run.
`SCREENING_CASES_DIR` relocates `cases/` (default: `./cases`, gitignored).

## Use

In Claude Code, from this directory: `/screen` and name the engagement and subjects. The skill in
`.claude/skills/screen/SKILL.md` walks the procedure. Or drive the CLI directly:

```bash
.venv/bin/screen case new GBC-BTN-2026-002 --commissioning-party "Gelephu Mindfulness City Authority"
.venv/bin/screen subject add GBC-BTN-2026-002 --type organization --name "Acme Pte Ltd" --jurisdiction SG
.venv/bin/screen run A GBC-BTN-2026-002
.venv/bin/screen run C GBC-BTN-2026-002
.venv/bin/screen mode GBC-BTN-2026-002 acme-pte-ltd --languages en,zh
# ... agent runs Layer B, pipes items into `screen media add`, then `screen layerb close`
.venv/bin/screen run D GBC-BTN-2026-002
.venv/bin/screen report GBC-BTN-2026-002
```

Output: `cases/<engagement>/summary.md` and `cases/<engagement>/<subject>/record.json`.

## Data handling

- `cases/` holds third-party personal data. It is gitignored. **Do not Syncthing-share this folder.**
- `screen purge` drops cached vendor text after 90 days and records after 12 months; scan IDs are kept.
- Request bodies are never logged.
- Vendor terms may restrict onward disclosure and require notifying data subjects. Check before sharing a summary outside the commissioning party.

## Open questions (spec §11)

- Is this deployment commercial for OpenSanctions licensing? Ask at signup.
- May NameScan findings be quoted verbatim downstream, or only cited by scan ID?
````

- [ ] **Step 3: Verify the skill is discoverable**

Run: `ls .claude/skills/screen/SKILL.md && head -3 .claude/skills/screen/SKILL.md`
Expected: the file exists and the frontmatter starts with `name: screen`.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/screen/SKILL.md README.md
git commit -m "docs: /screen skill procedure and README

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 14: End-to-end smoke against mocks and the test key

**Files:**
- Create: `tests/test_e2e.py`

**Interfaces:**
- Consumes: `cli.main` and the mock wiring pattern from `tests/test_cli.py`.

- [ ] **Step 1: Write the e2e test**

`tests/test_e2e.py`:

```python
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
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/test_e2e.py -v`
Expected: 1 passed. Then the full suite: `.venv/bin/pytest -q` — all green.

- [ ] **Step 3: Live smoke with the NameScan test key (only if the key exists)**

This is the only step that touches a vendor. It spends nothing.

```bash
security find-generic-password -s NAMESCAN_API_KEY_TEST -w >/dev/null 2>&1 && echo present || echo "no test key; skip"
```

If present:

```bash
.venv/bin/screen case new SMOKE-2026-001 --commissioning-party "dev smoke"
.venv/bin/screen subject add SMOKE-2026-001 --type person --name "Vladimir Putin"
.venv/bin/screen run C SMOKE-2026-001 --test
.venv/bin/screen record show SMOKE-2026-001 vladimir-putin | head -60
rm -rf cases/SMOKE-2026-001
```

Expected: `Layer C ok`, `test_mode: true`, `credits_consumed: 0.0`, `adverse_media: not_requested`, `number_of_matches` > 0. If the vendor returns a shape different from the fixtures, update `namescan.py` and the fixtures to match and note it in the commit.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test: end-to-end CLI flow against mocks

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Not in this plan

Ongoing monitoring, PDF report download from NameScan (`GET /reports/scans/{scanId}`), OpenCorporates,
filed-accounts parsing, multi-user review UI. Each is a follow-on plan if wanted.
