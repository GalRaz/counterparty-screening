# Counterparty Screening System — Design

**Date:** 2026-09-13
**Source spec:** `screening-tools-spec.md` (v7, 2026-08-09)
**Purpose:** Background-check people and companies we are considering collaborating with, producing
candidate matches and candidate media items for human review — never verdicts.

## 1. Decisions taken

| Question | Decision |
|---|---|
| Form factor | Claude Code skill `/screen` orchestrating a Python CLI `screen`. Layer B (adverse media) is run by the agent with its own WebSearch/WebFetch. |
| Scope | Spec Layers A (OpenSanctions), B (web search), C (NameScan Sapphire), plus a light Layer D for organisations: open registry lookups (GLEIF, UK Companies House, agent-driven lookups elsewhere). No credit checks, no beneficial-ownership tracing, no filed-accounts parsing, no court-record search. |
| Storage | `cases/` in this repo, gitignored. SQLite dedup store at `cases/store.db`. Records at `cases/<engagement>/<subject-slug>/`. This folder must never be Syncthing-shared. |
| Language / tooling | Python 3.14, venv, `httpx`, stdlib `sqlite3`, `pytest`. |
| Secrets | macOS Keychain via `security find-generic-password -s <name> -w`; environment variables override. Never in files. |
| Version control | git, code only. First commit: spec + this design. |

## 2. Architecture

Two halves with a hard boundary:

- **CLI (deterministic).** API calls, cost guards, dedup, storage, the §5 JSON record, the Markdown
  report, and a guardrail linter. Everything here is testable without a model.
- **Skill (judgement).** Normalising subjects, choosing Layer B queries and languages, fetching and
  classifying articles, proposing directors as new subjects, writing the narrative. The skill never
  bypasses the CLI to write records.

Execution order per subject: **A → C → B**, then **D** for organisations. Layer B mode is computed
by the CLI from the Layer C outcome (spec §3.0) and handed to the agent.

## 3. Components

Each module has one purpose and a narrow interface.

### `screening/subjects.py`
Subject model: `type` (person | organization), `names` (all variants and scripts), `identifiers`
(each `{kind, value, source}`), `jurisdiction`, `role_in_engagement`. An identifier without a
`source` is rejected. `normalised_key()` returns the dedup key (lowercased, whitespace-collapsed
primary name + type + DOB/registration number if present).

### `screening/opensanctions.py` — Layer A
`match(subject) -> LayerAResult`. Builds `POST /match/default?algorithm=best&threshold=0.7&limit=10`
with explicit `schema`, all name variants, split first/last for persons, and supplied attributes only.
Enriches every result via `GET /entities/{id}`. Adds coverage gap `transliterated name — matcher
precision reduced` when any name variant is non-Latin. HTTP errors → `status: failed`.

### `screening/namescan.py` — Layer C
`scan(subject, *, test=False) -> LayerCResult`. Before spending:
1. `credits()` pre-flight; abort run if balance < cost; warn if < 20.
2. Ceiling `NAMESCAN_MAX_SUBJECTS_RUN` (default 20) → abort oversized run.
3. Dedup: `store.find_scan(key, within_days=90)` → `GET /{scanId}` (free) instead of a new scan.
4. `includeAdvancedMedia: true` by default; 60 s timeout; two retries with backoff.
5. Only ever populates `dob`, `country`, `gender`, `idNumber` from identifiers with a source.
Result distinguishes `advanced_media: ok | failed | not_requested` (property absent ⇒ `failed`).
Persists `scanId` on success. Test key: no credits, no history, no media — flagged in result.

### `screening/registry.py` — Layer D (organisations only)
`lookup(subject) -> LayerDResult`. Sources:
- GLEIF (`api.gleif.org`, no key): LEI record, legal name, registered address, registration authority id.
- UK Companies House (`api.company-information.service.gov.uk`, free key `COMPANIES_HOUSE_API_KEY`):
  status, incorporation date, SIC codes, filing history summary, officers, accounts due/overdue.
- Manual slot: for registries with no API (ASIC, ACRA, RCS Luxembourg, Bhutan), the agent looks up
  and pipes structured findings via `screen registry add`, each with URL and retrieval date.
Officers found are emitted as `proposed_subjects` with the reason. They are never auto-screened.

### `screening/store.py`
SQLite at `cases/store.db`. Tables: `scans(key, scan_id, provider, created_at, subject_type)`,
`records(engagement, subject_slug, path, created_at)`, `vendor_text(scan_id, body, cached_at)`.
`purge()` deletes `vendor_text` older than 90 days and `records` older than 12 months; `scans` is
kept indefinitely as the audit handle.

### `screening/mode.py`
`select(layer_c: LayerCResult | None) -> Mode` implementing spec §3.0. Returns `full` or `reduced`,
a `mode_reason`, and the list of query families to run (§3.1). Printed for the agent.

### `screening/record.py`
Builds the §5 JSON record. `assessment` and `relevance` are fixed at `unreviewed`; the agent may set
`proposed_disposition` on any candidate. `layer_c` and `layer_d` are separate top-level fields —
provenance is never merged. `human_review_required` is always `true`. Validated with a JSON schema.

### `screening/lint.py`
`lint(record, report_md) -> list[Violation]`. Rules:
- Forbidden phrases: `clear`, `cleared`, `no risk`, `no adverse media found`, `has no adverse media`.
- Any `media_item` with `identity: confirmed_subject` must have a non-empty `corroborator`.
- No `media_item` with `identity: possible_subject` may be cited in the report's findings narrative.
- Legal-status words in the report must match a `legal_status` present in the record
  (e.g. "convicted" in prose requires a `convicted` item).
- If any subject lacks a successful Layer C, the report must contain the §8 limitation paragraph.
- Every `checks_run` entry with `status: failed` must have a matching `coverage_gaps` entry.
Lint failure blocks `report` from writing.

### `screening/report.py`
Renders `summary.md` in the shape of the GBC-BTN example: header (engagement, subjects, tools, dates,
CONFIDENTIAL), bottom line using only §3.6 permitted phrasing, subjects table (per layer: candidate
count, status), limitations, findings by layer with provenance labels, proposed subjects, coverage
gaps, distribution note. Includes the §8 paragraph unless Layer C succeeded for every subject.

### `screening/cli.py`
```
screen case new <engagement-id> --commissioning-party "<name>"
screen subject add <engagement-id> --type person|organization --name ... [--alias ...] [--id kind=value@source] [--jurisdiction XX]
screen run A|C|D <engagement-id> [--subject <slug>] [--test]
screen mode <engagement-id> <slug>               # prints B mode + query families
screen media add <engagement-id> <slug>          # one media_item as JSON on stdin
screen registry add <engagement-id> <slug>       # one manual registry finding on stdin
screen record show <engagement-id> <slug>
screen report <engagement-id>                    # lint, then write summary.md
screen lint <engagement-id>
screen credits
screen purge
```

### `.claude/skills/screen/SKILL.md`
The orchestration procedure. States §3.1 query families, §3.2 identity rule, §3.3 retrieval rules,
§3.4 legal statuses, §3.7 scope limit and §7.1 named-subject gate as a checklist. The agent runs
queries in English plus the official language(s) of the subject's jurisdiction plus the regional
lingua franca, records every query string, fetches articles before summarising, and pipes each item
to `screen media add`. It proposes — never adds — subjects it thinks should be screened.

## 4. Data flow

1. User: `/screen GBC-BTN-2026-002 "Acme Pte Ltd (SG)", "Jane Doe, director"`.
2. Skill normalises, calls `case new`, `subject add` for each, recording identifier sources.
3. `run A` for all subjects. `run C` for all subjects (pre-flight, dedup, ceiling).
4. `mode` per subject → agent runs Layer B, classifies, `media add` per item.
5. `run D` for organisations → proposed directors printed → agent asks user; user adds via `subject add`; steps 3–4 repeat for them.
6. `report` → lint → `cases/<engagement>/summary.md` plus per-subject `record.json`.

## 5. Error handling

- Any HTTP failure: `checks_run[].status = failed`, matching `coverage_gaps` entry. Never treated as clean.
- Missing API key: that layer is skipped with `status: not_run`; §8 paragraph attached if Layer C.
- Credits below run cost: abort before any spend; report balance.
- Ceiling exceeded: abort with count.
- `advancedMedia` absent: `adverse_media: failed`, Layer B forced to `full`.
- Lint violation: `report` refuses to write; violations printed with line references.
- PII: request bodies are never logged; log lines carry subject slug only.

## 6. Testing

Pytest, mocked `httpx` transports, fixtures recorded from vendor docs:
- Mode selection: all four rows of §3.0.
- Dedup: second scan within 90 days reuses scanId; after 90 days re-scans.
- Credits abort and low-credit warning; ceiling abort.
- `advancedMedia` present-empty vs absent produce different results.
- Identifier without source is rejected; inferred DOB never reaches NameScan body.
- Each lint rule has a failing and a passing case.
- Record validates against schema; report renders and includes §8 paragraph when required.
- CLI smoke test end-to-end against mocks producing `record.json` and `summary.md`.
No live vendor calls in tests. Live smoke uses the NameScan test key only.

## 7. Out of scope (for this design)

Identity verification, credit checks, beneficial-ownership tracing, litigation search, filed-accounts
parsing, ongoing monitoring, multi-user web UI. Registry lookups for jurisdictions without an API are
agent-driven and recorded, not automated.

## 8. Open items needing a human answer (from spec §11)

- Whether this deployment is commercial for OpenSanctions licensing. Ask at signup.
- Whether NameScan findings may be quoted verbatim downstream or only cited by scanId.
- Accounts to create: OpenSanctions, NameScan (buy the 100-scan package), Companies House developer key.
