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
