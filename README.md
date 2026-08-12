# Thirdeye — GO Intelligence Engine (Phase 1)

An evidence-first acquisition pipeline for Tamil Nadu Government Orders.

> Nothing enters the platform unless it can be traced back to an official
> government source and an original document.

Phase 1 is measured on source authenticity, document completeness,
traceability and extraction accuracy — not on UI or citizen features.

---

## The guarantee, and how it is enforced

The zero-hallucination principle is enforced *structurally*, not by convention:

| Rule | Enforcement |
|---|---|
| Only approved government sources | Host allowlist checked at registration **and** re-checked at download, including after redirects (`config.py`, `registry.assert_approved`) |
| Original PDF always retained | `documents` rows are write-once; SQLite triggers reject `UPDATE` of the hash/path and reject `DELETE` outright |
| Every field traceable | `go_fields.source_page` and `source_text` are `NOT NULL` — a value with no evidence is *not representable* |
| Corrections never destroy history | A correction inserts a new row and marks the old one `superseded_by`; the machine's original answer stays queryable |
| Every action auditable | `audit_log` is append-only, guarded by `BEFORE UPDATE`/`BEFORE DELETE` triggers |
| No publication without verification | `verified_records()` reads only `status='approved'`; approval refuses missing core fields unless explicitly overridden, and the override is audited |

If the extractor finds no evidence for a field, the field is simply absent.
There is no code path that emits a value without a page and a source span.

---

## Quick start

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt
```

Run the whole pipeline offline against synthetic GOs — no network, no live portal:

```bash
.venv/Scripts/python.exe -m goengine.cli demo
```

Then open the Validation Workbench:

```bash
.venv/Scripts/python.exe -m goengine.cli --data-dir data/demo serve
```

Against real sources:

```bash
.venv/Scripts/python.exe -m goengine.cli init
```

```bash
.venv/Scripts/python.exe -m goengine.cli run --force
```

---

## Architecture

```
Official Source  →  Crawler  →  Downloader  →  Repository
                                                   ↓
Verified GO DB  ←  Workbench  ←  Metadata Eng.  ←  Text Extraction
```

| Module | Code | Notes |
|---|---|---|
| 1 Source Registry | `registry.py` | Allowlist + seeded TN sources |
| 2 Source Discovery | `discovery/` | Per-source adapters, dedupe, status lifecycle |
| 3 Acquisition | `acquisition.py` | SHA256, unchanged bytes, PDF sniffing |
| 4 Repository | `repository.py` | Content-addressed, write-once, versioned |
| 5 Text Extraction | `extraction/text.py` | pymupdf → pdfplumber → pypdf, page-preserving |
| 6 Metadata Extraction | `extraction/metadata.py` | Evidence-bound fields + confidence |
| 7 Validation Workbench | `workbench/` | FastAPI review UI |
| 8 Audit & Traceability | `audit.py` | Append-only trail, provenance chains |

Documents are stored at `data/documents/<sha[0:2]>/<sha[2:4]>/<sha>.pdf`.
Content addressing means identical bytes are stored once, the path *is* the
fingerprint, and integrity is verifiable by rehashing (`thirdeye verify-repo`).

### Extraction confidence

Two different confidences are recorded, and they mean different things:

- **Text-layer confidence** (`extractions.confidence`) — how well the PDF's
  text came out. Near zero means a scanned document, flagged `needs_ocr`
  rather than silently parsed into garbage.
- **Field confidence** (`go_fields.confidence`) — how sure the extractor is
  about one value, from pattern precision × page × *region*, then adjusted for
  corroboration and contradiction.

Region matters most. A GO cites the orders it was issued under in a `Read:`
block, so a GO number found there belongs to a *different* order. Matches are
classified `@header`, `@references` or `@body`, and a reference-block match is
weighted down to ~0.22 of a header match. This is the single biggest source of
false positives on real GOs.

---

## Command reference

| Command | Purpose |
|---|---|
| `init` | Create the DB and seed official sources |
| `sources list \| add \| disable` | Manage the registry |
| `crawl` / `download` / `parse` | Run one stage |
| `run` | All three in one pass |
| `ingest <pdf> --source-id N --source-url U` | Archive a PDF obtained out of band |
| `status` / `queue` / `show <id>` | Inspect the pipeline |
| `approve` / `reject` / `correct` | Verification decisions |
| `verified` | Dump the Verified GO Database as JSON |
| `audit --document-id N` | Full provenance for one document |
| `verify-repo` | Re-hash every archived file |
| `golden init \| score` | Accuracy measurement |
| `serve` | Validation Workbench |
| `demo` | Offline end-to-end run |

---

## Golden dataset

```bash
.venv/Scripts/python.exe -m goengine.cli golden init --dataset golden
```

Drop the PDFs into `golden/`, re-run `golden init` to refresh the template,
fill in the ground truth in `golden/annotations.csv`, then:

```bash
.venv/Scripts/python.exe -m goengine.cli golden score --dataset golden --json report.json
```

A **blank cell means "this field does not appear in this order"**, and the
extractor is scored correct only if it also reports nothing. That is what makes
the metric measure hallucination rather than just recall — `hallucinated` is
counted and reported as its own column.

Comparison is field-appropriate, not string equality: GO numbers match on
digits + series, dates on parsed value, budgets on rupee amount, subjects on
containment or ≥90% token overlap.

| Field | Phase 1 target | Field | Phase 2 target |
|---|---|---|---|
| GO Number | 99% | Budget | 95% |
| GO Date | 99% | District | 95% |
| Department | 99% | Scheme Name | 90% |
| Subject | 95% | | |

`golden score` exits non-zero when Phase 1 targets are not met, so it can gate CI.

---

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

83 tests, fully offline — `OfflineFetcher` serves canned responses and
`sampledata.py` renders realistic two-page GO PDFs, so the whole pipeline is
exercised without touching a government server. The suite covers the
governance rules directly: unofficial sources rejected, documents
un-overwritable, audit log un-editable, corrections non-destructive, and
nothing invented from an empty document.

---

## Status against the Phase 1 exit criteria

| Criterion | Status |
|---|---|
| Official source registry operational | ✅ |
| Documents automatically discovered | ✅ engine complete; **live portal selectors need confirmation** |
| PDFs downloaded and archived | ✅ |
| Metadata extraction works reliably | ✅ on synthetic fixtures; **needs the real golden dataset** |
| Validation workbench functional | ✅ |
| Audit trail available | ✅ |
| Accuracy targets achieved | ⚠️ **not yet demonstrated — requires 150 annotated real GOs** |

### What is not done, and why

Two exit criteria cannot be closed from code alone:

1. **The seeded source URLs are unverified.** Department paths on
   `cms.tn.gov.in` change between site revisions. The crawler is
   adapter-based so adapting is a one-file change, but someone has to run
   `thirdeye crawl --force --source-id N` against the live portal and confirm
   the listing selectors. Until then treat the seed list as a starting point,
   not as validated configuration.

2. **The golden dataset contains no real GOs.** The harness, scoring and
   targets are built and tested, but the blueprint's 150 manually annotated
   orders (50 Health / 50 Education / 50 Public Works) is human work. The
   100% scores in `thirdeye golden init --with-samples` are a smoke test of
   the *harness* against synthetic fixtures whose layout the patterns were
   written for — they are **not** evidence of real-world accuracy. Expect the
   first real run to score lower, especially on scanned orders and Tamil-language
   text, and to need OCR (no OCR backend is wired up yet).

### Before this is exposed beyond localhost

The workbench has **no authentication** — the reviewer name is a free-text form
field, which is fine for a single-operator POC and not fine for anything else.
Reviewer identity is what the audit trail attributes approvals to, so real
deployment needs real auth first.
