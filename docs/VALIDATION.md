# research-atlas — Validation Report

_Generated 2026-06-19T04:03:45Z_

**Status: PASS** — 39/39 hard checks passed, 0 failed.

## Hard checks (gate the build)

| check | result | detail |
|---|---|---|
| no orphan funder_grant.src_id -> funder | PASS | 0 orphans |
| no orphan funder_grant.dst_id -> grant | PASS | 0 orphans |
| no orphan grant_org.src_id -> grant | PASS | 0 orphans |
| no orphan grant_org.dst_id -> organization | PASS | 0 orphans |
| no orphan grant_person.src_id -> grant | PASS | 0 orphans |
| no orphan grant_person.dst_id -> person | PASS | 0 orphans |
| no orphan grant_work.src_id -> grant | PASS | 0 orphans |
| no orphan grant_work.dst_id -> work | PASS | 0 orphans |
| no orphan person_org.src_id -> person | PASS | 0 orphans |
| no orphan person_org.dst_id -> organization | PASS | 0 orphans |
| no orphan work_field.src_id -> work | PASS | 0 orphans |
| no orphan work_field.dst_id -> field | PASS | 0 orphans |
| funder.atlas_id unique | PASS | 73 rows / 73 distinct |
| grant.atlas_id unique | PASS | 958,273 rows / 958,273 distinct |
| organization.atlas_id unique | PASS | 140,631 rows / 140,631 distinct |
| person.atlas_id unique | PASS | 1,223,332 rows / 1,223,332 distinct |
| work.atlas_id unique | PASS | 226,785 rows / 226,785 distinct |
| field.atlas_id unique | PASS | 6,720 rows / 6,720 distinct |
| person_org provenance (source+as_of) | PASS | all rows carry provenance |
| grant_work provenance (source+as_of) | PASS | all rows carry provenance |
| work_field provenance (source+as_of) | PASS | all rows carry provenance |
| grant provenance (source+as_of) | PASS | all rows carry provenance |
| funder provenance (source+as_of) | PASS | all rows carry provenance |
| person provenance (source+as_of) | PASS | all rows carry provenance |
| grant_person provenance (source+as_of) | PASS | all rows carry provenance |
| organization provenance (source+as_of) | PASS | all rows carry provenance |
| grant_org provenance (source+as_of) | PASS | all rows carry provenance |
| funder_grant provenance (source+as_of) | PASS | all rows carry provenance |
| work provenance (source+as_of) | PASS | all rows carry provenance |
| field provenance (source+as_of) | PASS | all rows carry provenance |
| grant.amount_usd > 0 or null | PASS | ok |
| grant amount/currency consistent | PASS | ok |
| grant FX columns consistent | PASS | ok |
| ROR ids well-formed | PASS | all ROR ids well-formed |
| one canonical org per ROR id | PASS | no duplicate canonical orgs |
| ORCID ids well-formed | PASS | all ORCIDs well-formed |
| every grant has an awarder | PASS | all grants attributed to a funder |
| grant amount_usd = amount_original * fx | PASS | all USD amounts reconcile with their FX rate |
| money rows carry an FX date | PASS | all money rows stamp an fx_as_of |

## Soft metrics (coverage, informational)

| metric | value |
|---|---|
| ROR org coverage | 41,517/140,631 orgs ROR-resolved = 29.5% |
| ROR grant-recipient coverage | 538,569/822,477 recipient edges ROR-resolved = 65.5% |
| ORCID person coverage | 728,499/1,223,332 people with ORCID = 59.6% |
| works ingested | 226,785 works |
| grant->work links | 285,604 links |
| source[nih] grants | 529,262 grants, 528,207 w/ amount, $301,046,686,747 USD |
| source[ukri] grants | 174,405 grants, 39,509 w/ amount, $26,458,416,968 USD |
| source[nsf] grants | 126,764 grants, 126,764 w/ amount, $72,887,859,132 USD |
| source[cordis] grants | 56,585 grants, 56,585 w/ amount, $133,340,567,672 USD |
| source[gates] grants | 40,814 grants, 40,813 w/ amount, $98,510,355,586 USD |
| source[wellcome] grants | 26,050 grants, 23,772 w/ amount, $24,698,726,693 USD |
| source[sloan] grants | 3,419 grants, 3,419 w/ amount, $1,223,494,410 USD |
| source[dfg] grants | 974 grants, 0 w/ amount, $0 USD |
