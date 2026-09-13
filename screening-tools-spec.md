# Counterparty Screening — Agent Integration Spec

**Status:** v7, 2026-08-09. Verify endpoint details against live docs before shipping.
**Scope:** sanctions, PEP, and adverse-media screening of named individuals and organisations.
**Not in scope:** identity verification, credit checks, beneficial-ownership tracing, litigation search.

**Change from v6:** Layer B is now **conditional on the Layer C result** (§3.0), and runs after it.
Execution order is A → C → B.

---

## 0. What this system does and does not do

This system returns **candidate matches and candidate media items**, not conclusions. A match is a
name resembling the query. A media item is an article mentioning a similar name. Deciding whether
either refers to the actual subject is a human judgement that depends on context the tools do not
have.

The agent runs the checks, normalises results, and presents them for review. The agent **must not**
emit a verdict of the form "subject is clear" or "no risk found". See §6.

---

## 1. Architecture

| Layer | Source | Covers | Cost | Invocation |
|---|---|---|---|---|
| A. Watchlist | OpenSanctions `/match` | Sanctions, PEP, criminal, watchlists | ~€0.10 per query entity | Automatic |
| B. Adverse media | Agent's own web search | Negative news, enforcement, litigation | Search cost only | Automatic |
| C. Commercial | NameScan Sapphire API | Analyst-curated profiles, licensed media index | ~$3.13 per subject | Automatic for **named subjects** — §7.1 |

All three layers run by default on **named subjects** — the counterparties the user actually
nominated for screening. Layer C does not run on incidental names the agent encounters along the way
(§7.1). That limit is about scope and privacy, not cost.

**Execution order is A → C → B.** Layer B's scope depends on what Layer C returned (§3.0).

The layers fail differently, which is the point. A is precise and narrow: it only knows what is on a
list. B is broad and noisy: it finds things no list contains, and also finds things about entirely
different people. C is curated and expensive: it carries research the other two structurally cannot
produce, and it is the only one that yields a third-party artefact.

§8 states what the output lacks when Layer C has not been run.

---

## 2. Layer A — OpenSanctions `/match`

**Base URL:** `https://api.opensanctions.org`
**Auth:** header `Authorization: ApiKey <OPENSANCTIONS_API_KEY>`

### Request

```
POST /match/default?algorithm=best&threshold=0.7&limit=10
Content-Type: application/json

{
  "queries": {
    "q1": {
      "schema": "Person",
      "properties": {
        "firstName": ["Aleksandr"],
        "lastName": ["Zacharov"],
        "birthDate": ["1965"]
      }
    }
  }
}
```

- `schema` — `Person`, `Company`, `Organization`, `LegalEntity`. Set it explicitly; it improves
  precision. Use `LegalEntity` only when genuinely unsure.
- Dataset is the path segment. `default` covers sanctions, PEPs and other risk-adjacent entities.
  `/match/sanctions` restricts to sanctions lists only.
- `name` accepts multiple values including non-Latin scripts — pass every transliteration you have.
- Batch via multiple keys under `queries`. Each query entity is billed separately.

### Tuning — these are URL QUERY PARAMETERS, not body fields

- `algorithm` — `best` is the recommended default, always resolving to the highest-quality matcher.
  Pin to `logic-v2` only if you need scores stable over time for reproducibility. `/algorithms`
  lists what is available.
- `threshold` — score at or above which a result counts as a match. Default `0.7`. Raise to
  `0.8`–`0.85` for low false-positive tolerance. With `regression-v2`, drop to ~`0.5`.
- `limit` — **defaults to 5.** Set it deliberately; 5 is low if you want to review near-misses.
- `topics` — filters to specific risk tags.

`ofac` is a name-only matcher (added June 2026) emulating the OFAC Sanctions List Search tool.
`logic-v1` is not recommended for new work. `name-based` and `name-qualified` retire 2027-01-18.

Feature weights (`0.0`–`1.0`) go in a top-level `weights` object in the request **body**.

### Response

```
{"responses": {"q1": {"results": [ { "id": ..., "caption": ..., "score": ..., "properties": {...} } ]}}}
```

Read `id`, `caption`, `score`, `properties.topics` (risk tagging), and source datasets. Call
`/entities/{id}` on anything above threshold to pull the full record including relatives and close
associates.

### Documented matcher limitations — read before trusting a clean result

Published openly by the vendor:

- **Non-Western writing systems** produce increased error rates, explicitly including the writing
  systems of China, Japan, Korea, Burmese, and many Indian languages.
- **Phonetic matching** (Soundex, Metaphone) does not function on non-alphabetic scripts.
- **Company names** match poorly when the legal-type suffix is misspelled (`Lymited` vs `Limited`).
- **No nickname dictionary** — `Alexander` will not match `Sasha`.

A clean result on a name transliterated from a non-Latin script is weaker evidence than the same
result on a Latin-script European name. Record this in `coverage_gaps`.

### Improving precision

Set `schema`; supply multiple aliases; split person names into `firstName`/`lastName`; supply
`birthDate` (even a year alone helps a great deal), `country`, `registrationNumber`, `taxNumber`
where genuinely known. A divergent supporting attribute *reduces* the score here — it does not
delete the candidate.

### Billing note

Only HTTP 200 responses are billed; errors are free. Retries are cheap — but log failures. A
silently failed screen is worse than no screen.

---

## 3. Layer B — Adverse media by web search

**Runs after Layer C, and its scope depends on what Layer C returned.**

This layer covers what a licensed negative-news index cannot: local-language sources, regional
outlets, and anything published since the index last ingested. It is also **far more prone to false
attribution**, because it has no entity resolution at all. The discipline below is not optional; it
is what makes the layer usable rather than dangerous.

### 3.0 Mode selection

NameScan's adverse media returns articles related to a **matched** entity. A subject with no Acuris
profile therefore may receive no media coverage from Layer C at all — and most ordinary business
counterparties have no profile. **An empty or absent `advancedMedia` is not evidence of clean media;
it may mean the check had nothing to attach to.**

Select the mode from the Layer C outcome for that subject:

| Layer C outcome | Mode | Scope |
|---|---|---|
| No match returned (`numberOfMatches: 0`) | **Full** | All query families, §3.1.1–5, all configured languages |
| Match returned, `advancedMedia` present | **Reduced** | Local-language queries and a recent window only (§3.1.4 plus risk terms restricted to the last 24 months) |
| Match returned, `advancedMedia` absent or failed | **Full** | The media check did not run. Treat as if Layer C returned nothing on media. |
| Layer C not run for this subject | **Full** | Layer B is the only media source |

Reduced mode exists because a curated index with source credibility ratings and entity linking is
the more reliable instrument where it has coverage. Layer B then adds only what the index
structurally lacks: local languages and recency. It does not re-derive what Layer C already found.

Full mode is the default whenever there is any doubt. Record the mode used in `checks_run`.

### 3.1 Query construction

In **full mode**, run all five families. In **reduced mode**, run families 4 and 5, plus family 3
restricted to the last 24 months.

1. **Bare identity:** `"<full name>"` and each known alias, quoted.
2. **Qualified identity:** name + employer, name + role, name + city, name + company registration
   number. This is what makes the results attributable.
3. **Risk terms:** name + each of — fraud, bribery, corruption, money laundering, sanctions,
   embezzlement, tax evasion, insider trading, investigation, indicted, charged, convicted,
   lawsuit, regulatory action, fine, penalty, insolvency, liquidation, disqualified.
4. **Local-language:** repeat families 1 and 3 in the primary language(s) of the subject's
   jurisdiction and in the original script where applicable. **This is the single highest-value
   step for regional subjects and the one a licensed index is most likely to have missed.**
5. **Official sources:** the relevant national company registry, securities regulator, court
   listings, and central bank or financial regulator enforcement pages.

Record the exact query strings run. The query list is part of the audit trail — it is what shows
what was and was not looked for.

### 3.2 Identity resolution — the hard rule

**An article is not evidence about the subject until it is linked to the subject by at least one
corroborating attribute beyond the name.** Acceptable corroborators: employer, job title, company
registration number, city or country of residence, date of birth, a named associate already
confirmed, or a photograph.

Every media item is classified into exactly one of:

- `confirmed_subject` — name plus at least one corroborating attribute matches.
- `possible_subject` — plausible but uncorroborated. Common names land here by default.
- `different_person` — positively excluded (wrong country, wrong era, wrong profession).

Items classed `possible_subject` are surfaced, never merged into the findings narrative. **The agent
may not upgrade `possible_subject` to `confirmed_subject` on the basis of plausibility, tone, or
narrative fit.**

### 3.3 Retrieval and quoting rules

- **Never summarise an article the agent has not actually retrieved.** A search-result snippet is
  not the article. Fetch it, or mark the item `snippet_only`.
- Record for every item: title, publisher, author if given, publication date, URL, retrieval date.
- Do not reproduce article text at length. Short attributed extracts only; summarise in the agent's
  own words otherwise.
- If a source cannot be retrieved (paywall, dead link), record it as such rather than inferring its
  contents from the headline.

### 3.4 Legal status — do not flatten this

Classify each confirmed item by stage, and preserve the distinction in all downstream output:

`allegation` · `investigation_reported` · `charged` · `convicted` · `acquitted_or_dismissed` ·
`regulatory_action` · `settlement_no_admission` · `civil_claim` · `insolvency` · `commentary_only`

An allegation is not a finding. A settlement without admission is not a conviction. A reported
investigation is not a charge. **The agent must not paraphrase any of these into language implying
guilt**, and must not describe a person as having done something when the source says they were
accused of it.

Check for later developments before reporting: a 2019 charge may have been dismissed in 2021. Search
the matter, not just the name.

### 3.5 Source quality

Prefer primary sources — regulator publications, court records, registry filings, company statements
— over aggregators. Note when a claim rests on a single source, or on outlets with no editorial
accountability. Blogs, forums, and content farms are recorded with that status attached, not
silently mixed in with wire reporting.

### 3.6 Negative results

**A search that returns nothing is weak evidence.** It is bounded by the languages searched, the
indexing of the sources, and the queries the agent happened to run. The permitted phrasing is:

> No adverse media items meeting the identity-resolution criteria were returned by the queries
> listed in `queries_run`, searched in <languages> on <date>.

Not: "no adverse media found", and never "subject has no adverse media".

### 3.7 Scope limit

This layer exists to assess a counterparty in a defined business context. It is not open-ended
profiling. Do not collect personal information unrelated to the diligence question — family details,
health, personal relationships, home address, political or religious affiliation — even where a
search surfaces it. If an item is not relevant to the counterparty risk being assessed, it does not
go in the record.

---

## 4. Procedure

1. **Normalise the subject.** Resolve to `Person` or `Organization`. Record the source of every
   identifier (email thread, filed document, user statement). Never invent one.
2. **Layer A:** OpenSanctions `/match`, `algorithm=best`, `threshold=0.7`, `limit=10`. All name
   variants. Enrich hits via `/entities/{id}`.
3. **Layer C:** every named subject (§7.1), adverse media on. Not on incidental names.
4. **Layer B:** select the mode from the Layer C outcome (§3.0), then run the applicable query
   families. Classify per §3.2 and §3.4.
5. **Emit the record** in the §5 format. Do not summarise away the candidate list.
6. **If Layer C was not run** for a subject, attach the §8 limitation statement to that subject's
   output.

For organisations, screen the named directors, officers, and any parent entity as separate subjects.
A clean company name on its own means very little.

---

## 5. Output contract

```json
{
  "subject": {"name": "...", "type": "person|organization",
              "identifiers_supplied": ["..."], "identifier_sources": ["..."]},
  "checks_run": [
    {"layer": "A", "provider": "opensanctions", "dataset": "default",
     "algorithm": "best", "threshold": 0.7, "limit": 10,
     "status": "ok", "timestamp": "..."},
    {"layer": "B", "provider": "web_search",
     "mode": "full|reduced", "mode_reason": "...",
     "queries_run": ["..."], "languages": ["en", "..."],
     "status": "ok", "timestamp": "..."}
  ],
  "watchlist_candidates": [
    {"source": "opensanctions", "id": "...", "caption": "...", "score": 0.0,
     "topics": ["..."], "datasets": ["..."], "assessment": "unreviewed"}
  ],
  "media_items": [
    {"title": "...", "publisher": "...", "published": "...", "url": "...",
     "retrieved": "...", "retrieval_status": "full|snippet_only|unavailable",
     "identity": "confirmed_subject|possible_subject|different_person",
     "corroborator": "...",
     "legal_status": "allegation|charged|convicted|...",
     "source_type": "primary|wire|aggregator|low_accountability",
     "relevance": "unassessed"}
  ],
  "coverage_gaps": ["no DOB supplied", "no local-language sources indexed",
                    "transliterated name — matcher precision reduced", "..."],
  "human_review_required": true
}
```

`assessment` and `relevance` stay unreviewed unless a human sets them. The agent may *propose* a
disposition in a separate field; it may not write into these.

---

## 6. Guardrails

1. **Never output "clear", "no risk", or "cleared".** The permitted negative finding is a statement
   about the checks, not about the subject. See §3.6.
2. **Never merge provenance.** Watchlist findings, web-search findings, and model inference are
   three different things in three different fields. Layer B output is *not* a screening result and
   must never be presented as one.
3. **Never fabricate identifiers** to improve a match.
4. **Never upgrade `possible_subject` to `confirmed_subject`** without a named corroborating
   attribute.
5. **Never state an allegation as a fact.** Preserve legal status verbatim (§3.4).
6. **Escalate, don't resolve.** High match counts on common names are expected and evidence of
   nothing. Surface them; a person dispositions them.
7. **A failed call or unreachable source is a coverage gap, not a clean result.** This includes an
   absent `advancedMedia` property — it means the media check did not run, and Layer B goes to full
   mode (§3.0).
8. **PII handling.** Subject names and DOBs are personal data sent to a third-party processor. Do
   not log full request bodies into general traces. Observe §3.7 scope limits.
9. **Licence limits.** OpenSanctions is free for non-commercial use; **businesses require a data
   licence.** Confirm which applies before production.

---

## 7. Layer C — NameScan Sapphire (integrated, gated)

Real money per call. Everything below exists to make sure it is spent deliberately.

### 7.1 Scope gate — who gets screened

At package pricing a scan costs about as much as a coffee, so cost is no longer a reason to hold
back. **Run Layer C by default on every named subject.**

A **named subject** is one the user explicitly nominated for screening, or a director, officer, or
parent entity of a nominated organisation. These need no per-subject authorisation.

The gate that remains is about scope, and it is a privacy limit rather than a budget one. Every
Layer C call sends a named individual's identity to a third-party data processor and returns a
curated profile about them. That is a meaningful act regardless of price.

**Do not run Layer C on:**

- Names the agent merely encountered — people copied on an email thread, mentioned in passing,
  quoted in an article, or appearing in a document but not part of the engagement.
- Relatives and close associates surfaced by Layer A enrichment. They are context for assessing the
  subject, not subjects themselves.
- Anyone outside the counterparty relationship being assessed. See §3.7.

If the agent believes one of these should be screened, it says so and names the reason. A human adds
them to the subject list. The list is the authorisation.

Cheap credits make it tempting to screen everyone who appears. Don't — the constraint was never the
money.

### 7.2 Cost controls — implement all of these

1. **Pre-flight the balance.** Call the Sapphire credits endpoint before a run. If the balance is
   below the run's cost, stop and report — do not partially execute. Warn below 20 credits.
2. **Ceiling per run.** `NAMESCAN_MAX_SUBJECTS_RUN` aborts an oversized run rather than continuing.
   This is a runaway-loop guard, not a budget control.
3. **Deduplicate before spending.** Keep a local store of `scanId` keyed by normalised subject
   identity. Retrieving an existing scan by ID is free; running a new one is not. Reuse within the
   dedup window (§11).
4. **`includeAdvancedMedia` defaults to `true`.** The extra 0.25 credit is ~$0.63 and media is the
   weakest part of Layers A and B. This is the best available use of the credit budget.
5. **Bound retries.** Failed calls do not consume credits, but an unbounded retry loop against a
   degraded endpoint is still a hazard. Cap at two retries with backoff, then fail loudly.
6. **Fan-out follows the subject list, not the results.** Directors and parent entities of a
   nominated organisation are in scope. Names that merely appear in the results are not (§7.1).
7. **Test key for all development.** It consumes no credits and writes no scan history.

### 7.3 Cost basis

| Purchase | Unit cost | Notes |
|---|---|---|
| Single scan | **$15.00** | Ad-hoc; 6× the package rate |
| 100-scan package | **$250.00** (~$2.50/scan) | Valid 12 months from purchase |

**Buy the 100-scan package.** At single-scan pricing, seventeen checks cost more than a hundred. The
package is valid for a year, there is no contract, and unused credits are the cheapest possible form
of headroom.

A full subject with adverse media enabled — now the default — consumes 1.25 credits, roughly
**$3.13**, giving about **80 full subjects** per package. With media off it is 1.00 credit, ~$2.50,
100 subjects.

For scale: a five-subject engagement — one organisation plus four individuals — costs around $12.50
with media off, or $15.63 with it on. The gating in §7.1 is about deliberate use and auditability,
not about the money at this scale.

### 7.4 API reference

**Base URL:** `https://api.namescan.io/v3.1`
**Auth:** header `api-key: <NAMESCAN_API_KEY>`
**Content-Type:** `application/json-patch+json`

Accounts issue a **test key and a production key**. Test-key requests consume no credits and are not
written to scan history — **and do not support adverse media.**

#### Person scan

```
POST /v3.1/person-scans/sapphire

{
  "firstName": "string",
  "middleName": "string",
  "lastName": "string",
  "originalName": "string",
  "gender": "string",
  "dob": "DD/MM/YYYY or YYYY",
  "country": "string",
  "idNumber": "string",
  "exact": false,
  "matchRate": 75,
  "maxResultCount": 100,
  "includeAdvancedMedia": false
}
```

`firstName`/`lastName` **or** `originalName` is required. `originalName` takes a full name in
non-Latin script, or a full Latin name that cannot be reliably split.

#### Organisation scan

```
POST /v3.1/organisation-scans/sapphire

{
  "name": "string",
  "country": "string",
  "registrationNumber": "string",
  "exact": false,
  "matchRate": 75,
  "maxResultCount": 100,
  "includeAdvancedMedia": false
}
```

#### Retrieval and account endpoints

- `GET /v3.1/person-scans/sapphire/{scanId}` — re-fetch a scan. Free; use it instead of re-scanning.
- `GET /v3.1/organisation-scans/sapphire/{scanId}` — same for organisations.
- Sapphire credits endpoint — balance check (§7.2.1).
- Reports endpoint — PDF artefact for the file.

**Persist every `scanId`.** It is both the audit handle and the deduplication key.

### 7.5 CRITICAL — optional fields behave destructively

On Sapphire, if `dob`, `country`, or `gender` are supplied and do **not** match the database record,
**the entity is removed from the results entirely.** A wrong or guessed DOB silently suppresses a
true hit, and the output is indistinguishable from a clean screen.

This is the opposite of OpenSanctions, where a divergent attribute reduces the score but keeps the
candidate visible.

**Rule: never populate `dob`, `country`, `gender`, or `idNumber` with inferred, assumed, or
model-generated values. Populate them only from a document the user supplied, and record the source.
If uncertain, omit the field and accept the wider result set.**

### 7.6 Response handling

Person results: `numberOfMatches`, and per match `matchRate`, `matchedFields`, `category`, then under
`person`: `officialLists` (with `isCurrent`), `roles`, `nationalities`, `locations`,
`profileOfInterests`, `linkedIndividuals`, `linkedCompanies`, `sources`, `disqualifiedDirectors`.

Organisation results sit under `corporates[].entity` with `primaryName`, `nameDetails`,
`officialLists`, `identifiers`, `linkedIndividuals`, `linkedCompanies`, `generalInfo`.

Both return `taxHavenCountryResults` and `sanctionedCountryResults`. **Indicative only** — the vendor
notes these derive from name-matching across inconsistent source data and are not exhaustive.

Adverse media, when requested, returns under `advancedMedia` with per-article `title`, `link`,
`sourceName`, `publishedDate`, `summary`, `body`.

- **May take up to 45 seconds.** Set the client timeout to at least 60s on these calls.
- On failure the `advancedMedia` property is simply **absent** and no extra credit is charged.
  **Absence means the check did not run — not that no adverse media exists.** These two cases must
  be distinguished in the output.

### 7.7 Recording Layer C in the output

```json
"layer_c": {
  "authorised_by": "...", "authorised_at": "...", "trigger": "...",
  "provider": "namescan", "tier": "sapphire", "scan_id": "...",
  "match_rate_floor": 75,
  "adverse_media": "ok|failed|not_requested",
  "credits_consumed": 1.25,
  "reused_prior_scan": false
}
```

Layer C findings go in their own field. They are not merged with Layer A or B results — see §6.2.

---

## 8. What the output lacks without Layer C — state this alongside it

When only Layers A and B have run, the output is a triage product built on one open dataset and
general web search. It is **not commercial-grade screening**, and the difference is not cosmetic.
Anyone relying on it should be told:

**No proprietary research.** Commercial providers employ analyst teams building curated subject
profiles from sources that are not publicly indexed. Layers A and B see official lists and whatever
the open web surfaces.

**No licensed media index.** Layer B is live web search, not a curated negative-news archive with
entity linking. It misses paywalled, archived, and non-indexed reporting, and has no systematic
historical depth.

**Records absent.** Disqualified-director registers, source credibility ratings, curated
linked-company and linked-individual relationships, and analyst-written profile summaries.

**No third-party artefact.** No dated report from a named provider with a scan ID. The audit trail is
self-generated — queries, datasets, parameters, timestamps. Honest and reconstructible, but not
independent.

**No professional liability.** A commercial provider carries contractual obligations about its data.
Open data carries none, and neither does a search engine.

### Language for reports

> Screening performed against the OpenSanctions consolidated dataset (sanctions, PEP and watchlist
> sources) and by structured web search for adverse media in <languages> on <date>. This is not a
> commercial screening product: it does not include proprietary analyst-curated risk profiles or a
> licensed negative-news index, and no third-party screening provider has reviewed these results.

Delete this paragraph only when Layer C has actually been run, and replace it with the provider,
tier, scan ID and date. Do not let it get edited out of a summary otherwise — it is the part that
makes the rest honest.

---

## 9. Account setup

Accounts must be created by the party who will own and pay for them. Keys are issued per account and
billed to that account; scan history and the vendor relationship sit with the account holder.

**OpenSanctions:** register at `opensanctions.org`. A business email is issued a free trial key.
Confirm licensing before production — free for non-commercial use, businesses need a data licence,
and free keys are issued for journalism, civil-society advocacy and academic research. Decide hosted
(metered per query) vs self-hosted `yente` (no metering, bulk data licence required, query data
stays in your infrastructure).

**NameScan (Layer C):** register at `namescan.io`, retrieve the test and production keys from the
account profile, and buy the **100-scan Sapphire package at $250** (§7.3). Not the single-scan
option at $15. Pay-as-you-go, no annual contract, valid 12 months from purchase.

**Keys** move through a password manager or secrets store — never chat, email, or a shared document.
Never commit them; never expose them client-side.

---

## 10. Environment

```
OPENSANCTIONS_API_KEY
NAMESCAN_API_KEY            # production — Layer C only, metered
NAMESCAN_API_KEY_TEST       # development; no credits, no scan history, no adverse media
NAMESCAN_MAX_SUBJECTS_RUN   # hard ceiling per authorisation (§7.2.2)
```

Search tooling for Layer B must support non-English queries and return publication dates.

Timeouts: 30s default; **60s minimum** on any Layer C call with `includeAdvancedMedia: true`.

---

## 11. Defaults — resolved, surface during integration

These were open questions. Each now has a working default so implementation is not blocked. Every
one is a judgement call, not a requirement — raise them during integration if the reasoning does not
hold.

| Item | Default | Reasoning |
|---|---|---|
| OpenSanctions licence | Assume **commercial** — acquire a data licence | Cheaper to be right than to retrofit. Build on the trial key; licence before production. If the deployment qualifies as non-commercial, confirm with the vendor and downgrade. |
| `threshold` | **0.7** | Vendor-calibrated default for `algorithm=best`. Raise to 0.8 only if false positives become the bottleneck — and never lower it to make a subject look clean. |
| `limit` | **10** | Twice the vendor default. The dataset is de-duplicated so extra results are near-misses, which are worth seeing at this volume. |
| Layer B mode | **Full unless Layer C returned a match with media present** | Errs toward more searching. An unmatched subject is the normal case and the one where Layer C contributes least. |
| Layer B languages | **English + official language(s) of the subject's stated jurisdiction + the regional lingua franca** | A rule, not a fixed list — subject jurisdictions vary. Record the languages actually searched in `checks_run`; unsearched languages are a `coverage_gap`. |
| `includeAdvancedMedia` | **true** | ~$0.63 per subject to shore up the weakest part of the system. At package pricing this is the best use of the budget. |
| Layer C invocation | **Automatic for named subjects**; never for incidental names | Cost no longer justifies a gate; scope and privacy still do (§7.1). |
| `NAMESCAN_MAX_SUBJECTS_RUN` | **20** | Runaway-loop guard, not a budget control. Generous enough that legitimate engagements never hit it. |
| Dedup window | **90 days** | Reuse a prior `scanId` if the subject was scanned within 90 days. Beyond that, list changes make a refresh worth a credit. |
| Retention — screening records | **12 months** | Matches the credit package validity and covers a normal engagement cycle. |
| Retention — cached vendor profile text | **90 days**, then purge; keep the `scanId` | Licence terms restrict storage. The scan ID is the durable audit handle; the profile text is re-fetchable for free. |
| Match disposition | **The commissioning party**, named in the record | The agent never dispositions. See §6.6. |

**Still genuinely unresolved and needing a human answer:**

- Whether this deployment is commercial for OpenSanctions licensing purposes. Ask them at signup.
- Whether Layer C findings may be quoted verbatim in a downstream report, or only cited by `scanId`.
  This is a licence question for NameScan, not a design choice.
