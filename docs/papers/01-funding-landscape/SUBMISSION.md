# Submitting this preprint to Zenodo / bioRxiv

This paper is **deposit-ready** but **not yet deposited**. No DOI exists yet, and
**none has been fabricated** — a real DOI is only issued by Zenodo at the moment
of deposit, against the depositor's account. Do not cite a DOI for this work
until the steps below have been run and Zenodo returns one.

## What is ready in this directory

| File | Purpose |
|---|---|
| `paper.md` | The manuscript (source of truth). |
| `paper.pdf` | The rendered preprint (`build_pdf.py`). The file to upload. |
| `.zenodo.json` | Zenodo deposition metadata (title, authors, license, keywords). |
| `build_pdf.py` | Regenerates `paper.pdf` from `paper.md`. |

The figures are embedded in `paper.pdf`; their sources live in
`analysis/figures/`. The reproducible analysis is in `analysis/` and is pinned by
`tests/test_funding_landscape.py`.

## What the founder must supply

Depositing requires credentials this automated build deliberately does not hold:

1. **A Zenodo account** (https://zenodo.org) and a **personal access token**
   with the `deposit:write` and `deposit:actions` scopes
   (Zenodo → Settings → Applications → Personal access tokens).
2. **Confirmation of authorship/affiliation** as it should appear on the record
   (currently `Dichio, Gianangelo` / `Bucket Foundation` in `.zenodo.json` — edit
   if needed before deposit).
3. For **bioRxiv/arXiv**: a corresponding-author account on that server (separate
   from Zenodo) and agreement to that server's licensing.

## Zenodo deposit (once the token is set)

```bash
export ZENODO_TOKEN=...   # founder-supplied; never commit this

# 1. create an empty deposition
DEP=$(curl -s -X POST "https://zenodo.org/api/deposit/depositions?access_token=$ZENODO_TOKEN" \
  -H "Content-Type: application/json" -d '{}')
ID=$(echo "$DEP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
BUCKET=$(echo "$DEP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["links"]["bucket"])')

# 2. upload the PDF
curl -s -X PUT "$BUCKET/paper.pdf?access_token=$ZENODO_TOKEN" \
  --upload-file docs/papers/01-funding-landscape/paper.pdf

# 3. attach metadata (wrap .zenodo.json under {"metadata": ...})
python3 -c 'import json;m=json.load(open("docs/papers/01-funding-landscape/.zenodo.json"));print(json.dumps({"metadata":m}))' \
  > /tmp/zmeta.json
curl -s -X PUT "https://zenodo.org/api/deposit/depositions/$ID?access_token=$ZENODO_TOKEN" \
  -H "Content-Type: application/json" -d @/tmp/zmeta.json

# 4. publish (mints the DOI) — review on the web UI first if unsure
curl -s -X POST "https://zenodo.org/api/deposit/depositions/$ID/actions/publish?access_token=$ZENODO_TOKEN"
```

Step 4 is the irreversible one — it mints the DOI. Consider depositing to the
**Zenodo Sandbox** (`https://sandbox.zenodo.org`) first to dry-run the whole flow.

## After a DOI is issued

1. Add the DOI to `.zenodo.json` (`"doi": "10.5281/zenodo.XXXXXXX"`) and to the
   paper header.
2. Add a `CITATION.cff` at the repo root pointing at the DOI.
3. Update `data/MANIFEST.json` `related_identifiers` with the DOI so the dataset
   and the paper cross-reference.

## bioRxiv note

bioRxiv accepts the same `paper.pdf`. It has no public deposit API for new
submissions — the founder must use the web submission flow at
https://www.biorxiv.org/submit-a-manuscript, select the subject area
("Scientific Communication and Education" / "Bioinformatics" fit this work),
upload `paper.pdf`, and supply author/affiliation details matching `.zenodo.json`.
