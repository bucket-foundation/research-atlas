# Researcher Data Policy

**Version 0.1.0** · applies to the researcher/users (CRM) layer
(`atlas/users/`, `data/processed/researchers.parquet`, the
`researchers_public` / `researchers_contactable` DuckDB views).

The binding contract for how the platform collects, stores, and
uses researcher contact data. It exists to protect both researchers and the
project. **If anything below conflicts with code, the document wins and the code
is the bug.**

---

## 1. Scope

The researcher/users layer turns the atlas's **Person** nodes into rich,
segmented, *contactable* profiles, the platform's users, so that relevant
research tools and collaboration can be offered to the right researchers. It is
the all-field generalization of the single-field biophysics-PhD outreach funnel
prototype (`advisors_to_email.csv` / `funnel_targets.csv`).

It **is** a CRM for **targeted, relevant outreach** about computational research
tools and collaboration, the right tool offered to a researcher who
works in that area.

It is **NOT** a marketing spam list, **NOT** a sold/rented dataset, and **NOT**
a collection of private or purchased contact data.

---

## 2. Public-source-only

Every piece of contact data is drawn from **public professional sources** and
nothing else:

| Source (`email_source`) | What it is | Why it is public |
|---|---|---|
| `europepmc` / `pubmed` | Corresponding-author email embedded in the PubMed/EuropePMC record's affiliation string (`"Electronic address: …"`) | The author published it as their point of contact on a public paper |
| `orcid` | Email on the researcher's ORCID profile | Returned by the ORCID **public** API only when the researcher set it public, explicit opt-in |
| `crossref` | Author email in public Crossref metadata (author object or `affiliation[].name`), when present | Public scholarly metadata deposited by the publisher; citeable to the DOI |
| `labpage` | Email present on the researcher's **own** public homepage / lab page (a `mailto:` link or visible address), read from a URL the researcher listed on their public ORCID `researcher-urls` | Public web page the researcher/institution publishes; citeable to the exact URL. **Conservative**: public pages only, capped count + size, boilerplate (`info@`, `no-reply@`, …) filtered, never guessed |
| `official_directory` | Email printed on the researcher's profile page on their university's own website, in the page's structured data or a `mailto:` link, with an address under the domain listed in the institution's ROR record | The institution publishes it as the official point of contact; citeable to the profile URL. One adapter per institution (`atlas/users/directories/`), robots.txt obeyed, one request per second per host, 30-day cache, stored only under the gitignored `data/raw/contacts/official/` |

**Source priority is per-field.** Biomed/chemistry authors are tried against
**EuropePMC corresponding-author metadata first** (their highest-yield public
source); other fields lead with the ORCID public email (explicit opt-in, highest
trust). In all cases the first source that yields a literal, public, provenanced
email wins; `labpage` is the conservative last resort. An anti-noise filter
removes obvious institutional/automated mailboxes (`info@`, `no-reply@`,
`webmaster@`, placeholder/example domains), it only ever *removes* candidates,
it never invents one.

**Hard prohibitions, enforced in code (`atlas/users/schema.py::coerce_user`):**

- **No fabricated / guessed emails.** We never construct `firstname.lastname@inst.edu`
  or any pattern guess and present it as real. Unknown contact = `null`. A row
  that carries an `email` without a known-public `email_source` **plus**
  `email_source_url` **plus** `email_as_of` is **rejected** (raises). Tested in
  `tests/test_users_schema.py` and `tests/test_users_contacts.py`.
- **No private data.** No personal/home addresses, phone numbers, non-public
  emails, or any data behind authentication.
- **No purchased or scraped contact lists.**

Coverage: we report the **real** contact-coverage % per field
(`data/processed/sample/researchers_aggregates.json`). We never claim more
contacts than we sourced.

---

## 3. Provenance on every contact

Every contact row carries, inline:

- `email_source`, which public source (must be in the allowed set above)
- `email_source_url`, the citeable URL the email was read from
- `email_as_of`, ISO-8601 UTC timestamp it was harvested
- `email_method`, `corresponding-author-metadata` | `orcid-public` | …

A contact with no provenance cannot exist in the dataset, `coerce_user` refuses
to build it.

---

## 3a. Inferred addresses

Set by the founder on 2026-09-28 for his private advisor review. It amends the no-guessed-emails rule in section 2 for one private use only.

- An inferred address is stored in `email_inferred`, a separate field with `email_inferred_pattern`, `email_inferred_domain` and the ids of the colleagues whose verified addresses show the pattern. It never enters `email`, `researchers.parquet`, any view, or `coerce_user`, and `ALLOWED_EMAIL_SOURCES` never includes it.
- A pattern counts only when at least 3 colleagues at the same ROR institution have addresses from the section 2 sources that fit it and no verified colleague contradicts it.
- No SMTP check, test send or other probe of an inferred address. The code that infers has no network path.
- Every display labels it "inferred, unverified". Sending to one is a manual decision by the founder and happens outside this project's tools.
- Storage is the founder's private machine only; nothing inferred is committed, synced to Drive, or served by atlas-api.
- `opt_out` removes the inferred address along with every other contact field.

## 4. `contactable` and `opt_out`

- **`contactable`** defaults to *public-source-only*: it is `true` **only** when
  a public-sourced email exists **and** `opt_out` is `false`. No public email →
  `contactable = false`.
- **`opt_out`** defaults `false` but the column always exists so an unsubscribe
  or removal request can be honored **without deleting the profile**: set
  `opt_out = true` and the row is forced `contactable = false` and
  `engagement_status = opted-out`. The `researchers_contactable` view excludes
  opted-out rows, and the enrichment pipeline skips them entirely.

To honor a removal request: set `opt_out = true` for that `atlas_id` (or by
ORCID) and rebuild; the profile remains for graph integrity but is never
contacted again.

---

## 5. Regulatory awareness

This is operational awareness and carries no legal weight. Consult counsel before any
large outreach campaign.

### GDPR
- **Lawful basis:** legitimate interest in relevant, low-volume, professional
  research-tool outreach to researchers whose work is directly relevant, *not*
  bulk marketing. Keep volume low and relevance high; the segmentation +
  `tool_fit` fields exist precisely to keep outreach relevant.
- **Public-source + transparency:** all data is from public professional
  sources; provenance is retained so any subject can be told exactly where their
  data came from (`email_source_url`).
- **Right to object / erasure:** honored via `opt_out` (stops processing for
  outreach) and, on request, full removal of contact fields.
- **Data minimization:** we store the minimum, a professional email + the
  public scholarly metadata needed to make outreach relevant. No special-category
  data.

### CAN-SPAM
- Accurate `From`/subject lines; clear identification as outreach.
- A working **unsubscribe** mechanism in every message, honored promptly →
  recorded as `opt_out = true`.
- A valid physical postal address in the message.
- No deceptive routing.

### Targeted and Relevant
Outreach must be **relevant** to the recipient's actual research (matched via
`field_slug` / `top_topics` / `tool_fit`) and **low-volume**. A blast to the
whole table is a policy violation regardless of legal technicalities.

---

## 6. What is committed to git

The repository is public. Therefore:

- **NEVER committed:** the email-bearing `data/processed/researchers.parquet`,
  `data/processed/users.parquet`, and the raw contact-lookup cache
  `data/raw/contacts/`. All are gitignored (explicitly, in `.gitignore`).
- **Committed:** the schema (`atlas/users/`), the pipeline scripts, this policy,
  the needs/roadmap doc, the **email-free** sample
  (`data/processed/sample/researchers_sample.parquet`, PII columns dropped), and
  **counts-only** aggregates (`…/researchers_aggregates.json`).

`scripts/build_users_sample.py` drops every PII column
(`atlas/users/schema.py::PII_COLUMNS`) and asserts there is **zero** email-like
string in the sample before writing. A secret-scan step (`grep`/`gitleaks`) is
run before commit. If an email ever appears in a committed file, treat it as an
incident: remove it, rotate history if needed, and record it.

---

## 7. Retention & review

- Contact data is refreshed from source (provenance `email_as_of` shows age);
  stale contacts can be re-verified or dropped.
- This policy is reviewed whenever the source set, the lawful basis, or the
  outreach model changes.
