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
