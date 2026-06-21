# research-atlas — Validation Report

_Generated 2026-06-21T18:04:18Z_

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
| funder.atlas_id unique | PASS | 75 rows / 75 distinct |
| grant.atlas_id unique | PASS | 1,670,434 rows / 1,670,434 distinct |
| organization.atlas_id unique | PASS | 192,720 rows / 192,720 distinct |
| person.atlas_id unique | PASS | 1,438,636 rows / 1,438,636 distinct |
| work.atlas_id unique | PASS | 278,839 rows / 278,839 distinct |
| field.atlas_id unique | PASS | 6,782 rows / 6,782 distinct |
| funder_grant provenance (source+as_of) | PASS | all rows carry provenance |
| grant_person provenance (source+as_of) | PASS | all rows carry provenance |
| grant provenance (source+as_of) | PASS | all rows carry provenance |
| funder provenance (source+as_of) | PASS | all rows carry provenance |
| work provenance (source+as_of) | PASS | all rows carry provenance |
| work_field provenance (source+as_of) | PASS | all rows carry provenance |
| person provenance (source+as_of) | PASS | all rows carry provenance |
| person_org provenance (source+as_of) | PASS | all rows carry provenance |
| grant_work provenance (source+as_of) | PASS | all rows carry provenance |
| grant_org provenance (source+as_of) | PASS | all rows carry provenance |
| field provenance (source+as_of) | PASS | all rows carry provenance |
| organization provenance (source+as_of) | PASS | all rows carry provenance |
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
| ROR org coverage | 45,826/192,720 orgs ROR-resolved = 23.8% |
| ROR grant-recipient coverage | 1,026,741/1,533,828 recipient edges ROR-resolved = 66.9% |
| ORCID person coverage | 804,248/1,438,636 people with ORCID = 55.9% |
| works ingested | 278,839 works |
| grant->work links | 470,269 links |
| source[nih] grants | 1,125,130 grants, 1,122,983 w/ amount, $570,475,651,367 USD |
| source[nsf] grants | 201,676 grants, 201,670 w/ amount, $107,981,151,478 USD |
| source[ukri] grants | 174,405 grants, 39,509 w/ amount, $26,458,416,968 USD |
| source[cordis] grants | 92,463 grants, 92,075 w/ amount, $202,797,986,304 USD |
| source[gates] grants | 40,814 grants, 40,813 w/ amount, $98,510,355,586 USD |
| source[wellcome] grants | 26,050 grants, 23,772 w/ amount, $24,698,726,693 USD |
| source[czi] grants | 5,503 grants, 5,502 w/ amount, $5,924,313,945 USD |
| source[sloan] grants | 3,419 grants, 3,419 w/ amount, $1,223,494,410 USD |
| source[dfg] grants | 974 grants, 0 w/ amount, $0 USD |
