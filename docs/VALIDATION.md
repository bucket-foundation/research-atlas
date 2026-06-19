# research-atlas — Validation Report

_Generated 2026-06-19T00:58:44Z_

**Status: PASS** — 36/36 hard checks passed, 0 failed.

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
| funder.atlas_id unique | PASS | 69 rows / 69 distinct |
| grant.atlas_id unique | PASS | 887,016 rows / 887,016 distinct |
| organization.atlas_id unique | PASS | 126,774 rows / 126,774 distinct |
| person.atlas_id unique | PASS | 1,193,750 rows / 1,193,750 distinct |
| work.atlas_id unique | PASS | 226,785 rows / 226,785 distinct |
| field.atlas_id unique | PASS | 5,308 rows / 5,308 distinct |
| funder_grant provenance (source+as_of) | PASS | all rows carry provenance |
| work provenance (source+as_of) | PASS | all rows carry provenance |
| funder provenance (source+as_of) | PASS | all rows carry provenance |
| work_field provenance (source+as_of) | PASS | all rows carry provenance |
| field provenance (source+as_of) | PASS | all rows carry provenance |
| organization provenance (source+as_of) | PASS | all rows carry provenance |
| grant_work provenance (source+as_of) | PASS | all rows carry provenance |
| person provenance (source+as_of) | PASS | all rows carry provenance |
| grant_org provenance (source+as_of) | PASS | all rows carry provenance |
| grant_person provenance (source+as_of) | PASS | all rows carry provenance |
| person_org provenance (source+as_of) | PASS | all rows carry provenance |
| grant provenance (source+as_of) | PASS | all rows carry provenance |
| grant.amount_usd > 0 or null | PASS | ok |
| grant amount/currency consistent | PASS | ok |
| grant FX columns consistent | PASS | ok |
| ROR ids well-formed | PASS | all ROR ids well-formed |
| one canonical org per ROR id | PASS | no duplicate canonical orgs |
| ORCID ids well-formed | PASS | all ORCIDs well-formed |

## Soft metrics (coverage, informational)

| metric | value |
|---|---|
| ROR org coverage | 39,672/126,774 orgs ROR-resolved = 31.3% |
| ROR grant-recipient coverage | 492,550/751,642 recipient edges ROR-resolved = 65.5% |
| ORCID person coverage | 728,499/1,193,750 people with ORCID = 61.0% |
| works ingested | 226,785 works |
| grant->work links | 285,604 links |
