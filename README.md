# Thirdeye — GO Intelligence Engine (Phase 1 + 2 + 3 + 3.1)

An evidence-first acquisition pipeline for Tamil Nadu Government Orders.

> Nothing enters the platform unless it can be traced back to an official
> government source and an original document.

Phase 1 built the acquisition pipeline: registry, crawler, write-once
repository, extraction, workbench, audit trail. Phase 2 built the
*certification* layer on top of it — OCR, Tamil language support, and
precision/recall/F1 measured against human-verified ground truth, not
synthetic fixtures. Phase 3 builds the **Operations Control Center**: the
same platform, made operable by a non-technical administrator entirely
through a browser — geography configuration, a versioned source registry,
background certification jobs, typed review queues, publication gating, and
role-based access control over all of it. See
[Status against the exit criteria](#status-against-the-exit-criteria) for
what each phase could and could not close from code alone — every engine is
complete and tested; the actual certification of 10 sources, 200 real
orders, and real operational usage are data-collection and adoption
exercises that happen next, by humans running this tool against the live
internet and the live organization. Phase 3.1 builds the **public-facing
experience** on top of the same Phase 1-3 engines: a "Third Eye" brand and
6-theme design system, a public landing page, a redesigned login with
role-based routing, an Executive Command Center restyle of the Operations
Dashboard, and deployment-readiness config cleanup — no new backend logic,
no Firebase/Firestore migration (that is explicitly the next activity, not
part of this one).

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

Phase 2 adds its own governance layer on top, enforced the same way:

| Rule | Enforcement |
|---|---|
| No benchmarking against synthetic data | `golden_documents.document_id` is a foreign key into the real `documents` table — a document that never went through real acquisition cannot be added |
| Human annotations are the ground truth | `golden_annotations` is written only by `certification/golden.py`, never auto-filled from the extractor's own output |
| Corrections never destroy history | Same supersede pattern as `go_fields`: a re-annotation writes a new row and marks the old one `superseded_by` |
| Every failure is recorded | Every mismatch a certification run finds becomes a permanent `extraction_failures` row, classified by root cause |
| Confidence is checked against reality | Every certification run buckets predictions by stated confidence and compares to actual accuracy (`calibration_snapshots`) |

Phase 3 adds operational governance: every write action requires an
authenticated session, and the identity behind it is no longer a free-text
field a reviewer could type anything into.

| Rule | Enforcement |
|---|---|
| Every write action requires a real identity | Every mutating route depends on `require_login`/`require_permission`; the Phase 1/2 free-text "reviewer name" fields are gone, replaced by the session's authenticated username |
| Source edits are never destructive | `source_versions` is append-only (same triggers as `documents`); every edit inserts a new version, `sources` always reflects the current one |
| No publication without certification, approval, or provenance | `publish_district`/`publish_department` check all three, every call, with no direct write path that bypasses the check |
| Role permissions are structural, not advisory | `User.has_permission()` is checked server-side on every write route; a denied action returns 403 before touching the database, not just a hidden button |
| State admins are scoped to their own state | `can_act_on_state()` is checked on every district/source/publish action a state_admin can reach |

---

## Quick start

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt
```

Run the whole pipeline offline against synthetic GOs — no network, no live portal:

```bash
.venv/Scripts/python.exe -m goengine.cli demo
```

Then open the Operations Control Center:

```bash
.venv/Scripts/python.exe -m goengine.cli --data-dir data/demo serve
```

The first visit redirects to a one-time setup page to create the first
Platform Administrator — no CLI step, no seeded password. From there,
everything (states, districts, departments, sources, certification jobs,
review, publication, users) is reachable from **Administration** in the nav
bar. See [First Operational Scenario](#first-operational-scenario-phase-3)
below for a full walkthrough.

Against real sources:

```bash
.venv/Scripts/python.exe -m goengine.cli init
```

```bash
.venv/Scripts/python.exe -m goengine.cli run --force
```

### OCR setup (Module 4, optional)

Scanned Government Orders need Tesseract. Everything degrades gracefully
without it — documents just stay flagged `needs_ocr` for later reprocessing.

1. Install the Tesseract binary (e.g. `winget install UB-Mannheim.TesseractOCR`
   on Windows, `apt install tesseract-ocr` on Linux, `brew install tesseract`
   on macOS).
2. Put `eng.traineddata` and `tam.traineddata` in `vendor/tessdata/` (from
   [tesseract-ocr/tessdata](https://github.com/tesseract-ocr/tessdata)), or
   point `THIRDEYE_TESSDATA_DIR` at wherever your Tesseract install already
   keeps its language files.
3. `pip install pytesseract pillow` (or `pip install -e .[ocr]`).

Check it worked: `python -c "from goengine.extraction.ocr import is_available; print(is_available())"`

---

## Architecture

```
Official Source → Crawler → Downloader → Repository → Text Extraction → OCR
                                                                          ↓
                                          Metadata Extraction ← Language/Category ID
                                                   ↓
                    Golden Dataset ←  Validation Workbench  →  Verified GO DB
                          ↓
        Benchmark (P/R/F1) → Failure Intelligence + Confidence Calibration
                          ↓
                  Certification Dashboard
```

### Phase 1 — acquisition pipeline

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

### Phase 2 — certification layer (`certification/`)

| Module | Code | Notes |
|---|---|---|
| 1 Source Certification | `certification/sources.py` | 5 live checks: connectivity, discovery, download, stability, authenticity |
| 2 Acquisition Program | `certification/categorize.py` | Department/language/scan-type tagging, progress vs. the 200-GO target |
| 3 Golden Dataset Workbench | `certification/golden.py`, `workbench/templates/golden_*.html` | Human annotation on *real* documents only |
| 4 OCR Intelligence | `extraction/ocr.py` | Tesseract (eng+tam), merges only pages the digital layer left weak |
| 5 Tamil Language Processing | `certification/language.py` | Unicode-script classification: English / Tamil / Mixed |
| 6 Benchmark & Accuracy | `certification/benchmark.py` | Precision/recall/F1 per field, by department, by language |
| 7 Failure Intelligence | `certification/failures.py` | Every mismatch classified by root cause, permanently recorded |
| 8 Confidence Calibration | `certification/calibration.py` | Stated confidence vs. actual accuracy, bucketed by decile |
| 9 Certification Dashboard | `workbench/templates/certification.html` | All of the above, one page: `/certification` |

### Phase 3 — Operations Control Center (`operations/`)

| Module | Code | Notes |
|---|---|---|
| 1 State Management | `operations/geography.py` | NEW → CONFIGURED → ACTIVE → RETIRED, forward-only |
| 2 District Management | `operations/geography.py` | Per-state; certification/publication status computed from applicable sources |
| 3 Department Management | `operations/departments.py` | Enable/disable; real metrics where a Phase 2 bucket exists, honest "not tracked" where none does |
| 4 Source Registry (versioned) | `operations/sources.py` | Add/Edit/Disable/Retire/Clone/Test; every edit is a new `source_versions` row, nothing overwritten |
| 5 Source Certification Center | `operations/sources.py` + Phase 2's `certification/sources.py` | The admin UI over Phase 2's certify_source engine — not a second implementation |
| 6 Certification Job Center | `operations/jobs.py` | Background thread per job, DB-backed live progress (found/downloaded/parsed/needs-OCR/failed) |
| 7 Review Workbench (extended) | `operations/review.py` | Typed queues (Extraction/OCR/Metadata/Failure) over the existing Phase 1 queue; adds Escalate |
| 8 Publication Control | `operations/publication.py` | Publish/unpublish district & department, gated on certification + approval + provenance |
| 9 Operations Dashboard | `operations/dashboard.py` | Composition only — every number comes from Modules 1-8 |
| 10 Audit & Governance Center | `audit.py` (Phase 1) + filter UI | Same append-only log every phase writes to; `/audit` gained action/entity filters |
| 11 Users & Roles | `operations/auth.py` | Session-cookie auth, 5 roles, scrypt password hashing (stdlib, no new dependency) |
| 12 System Health Center | `operations/health.py` | Source availability, OCR health, storage, job queue — computed from existing state, not a new monitoring daemon |

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

### OCR merge (Module 4)

OCR runs per *page*, not per document — a digital cover page next to a
scanned annexure is common in real GOs, and re-OCRing pages that already
extracted cleanly would only add noise. A weak page's digital text is
replaced only when the OCR result is both longer and above a confidence
floor (0.35); otherwise the sparse original is kept rather than risk seeding
metadata extraction with confident-sounding OCR garbage. `extractions.confidence`
is recomputed after a merge — it is never left showing the pre-OCR score for
a document OCR just fixed.

### Failure classification priority (Module 7)

A mismatch is classified by the *most likely root cause*, checked in order:
hallucination (any field) → OCR (document was scanned) → Tamil parsing
(document is Tamil-dominant, and the patterns are English-oriented) →
reference misclassification (picked a cited order's number/date over the
order's own) → table extraction (table-heavy document, budget/district
field) → the field-specific bucket as a fallback.

### Roles and permissions (Module 11)

| Role | Can do |
|---|---|
| `platform_admin` | Everything, everywhere |
| `state_admin` | Manage districts/sources/certification/review/publication **within their assigned state only** — `can_act_on_state()` blocks every write route otherwise |
| `reviewer` | Approve/reject/correct/escalate records |
| `auditor` | Read-only, including the audit trail |
| `read_only` | Read-only |

The first account (Platform Administrator) is created through a one-time
`/setup` page that only exists while zero users do — after that it 303s to
`/login` like everything else. There is no seeded password anywhere in the
codebase.

### Geography modeling note (Modules 1-2, 8)

Phase 1/2's source model is fundamentally **state-wide**: a GO portal serves
an entire state, and individual documents carry no per-district tag (that
mapping is Phase 4's job — Geography Intelligence). So `sources.district_id`
is optional: a source with a district set is district-specific; a source
with `district_id NULL` is state-wide and implicitly covers every district
in its state for certification/publication purposes. District-level
publication is therefore a coarse, honest proxy — "are this district's
sources certified, and does at least one approved, evidenced record exist
anywhere" — not a claim that any specific order is *about* that district.
That precision doesn't exist until Phase 4.

### Phase 3.1 — Public experience & deployment prep (`workbench/static`, `workbench/templates`)

Presentation layer only, built on the existing FastAPI/Jinja2 stack — no new
routes' worth of business logic, no rewrite of Phases 1-3. Every number shown
on every new page is read through the same functions Phases 1-3 already
built and tested (`operations.dashboard.operations_summary`, `audit.trail`,
`operations.health.system_health`); Phase 3.1 only composes and styles them.

| Piece | Code | Notes |
|---|---|---|
| Design system (6 themes) | `static/theme.css` | Light/Dark/Glass mandatory + Aurora/Emerald/Midnight; CSS custom properties per `[data-theme]`, no build step |
| Third Eye mark | `templates/_partials.html` (`mark()` macro) | An original SVG (ring + node graph), not the literal OrchestrAI asset — avoids reproducing third-party wordmark/trademark, stays theme-adaptive via CSS-var gradient stops |
| Theme persistence | `static/theme.js` | `localStorage`, applied via an inline pre-paint script (`theme_init_script()` macro) to avoid a flash of the wrong theme |
| Public landing page | `templates/landing.html`, `GET /` | No login required; hero, evidence-flow, live metrics pulled from real `operations_summary()` data, not mocked |
| Login / first-run setup | `templates/login.html`, `templates/setup.html` | Same `auth.py` session logic as Phase 3 (untouched) — only the template and post-login redirect changed |
| Role-based post-login routing | `workbench/app.py` (`_post_login_destination`) | `reviewer` → Review Workbench, `auditor` → Audit Center, everyone else → Operations Dashboard |
| `/admin` entry point | `workbench/app.py` | Logged-in shortcut that 303s to the role-appropriate page above |
| Executive Command Center | `templates/ops_dashboard.html`, `operations_routes.py` | KPI cards, progress rings (computed in Jinja, no chart library), alerts banner (`system_health()["alerts"]`), recent-activity feed (`audit.trail(limit=8)`) — all reused data, new presentation |

The Phase 1 dashboard (verification queue) moved from `/` to `/workbench`
so `/` could become the public landing page; every protected page still
requires the same session auth Phase 3 built.

---

## First Operational Scenario (Phase 3)

The blueprint's own acceptance scenario, walked end to end through the
browser with a real running server (no CLI beyond seeding demo documents to
stand in for a live crawl of unverified portal URLs — see the exit-criteria
notes below):

1. **Setup** → create the Platform Administrator at `/setup`.
2. **Administration → States** → add Tamil Nadu (`TN`).
3. **Administration → Districts** → add Chennai under Tamil Nadu.
4. **Administration → Departments** → seed the blueprint's example list
   (Health/Education/Public Works/Rural Development/Agriculture/Transport/
   Fisheries); all start active.
5. **Administration → Source Registry** → add "TN GO Portal", scoped to
   Tamil Nadu.
6. **Source detail → Run full certification** → this is a real network call
   (Phase 2's `certify_source`); against the still-unverified seed URL it
   correctly and honestly returns `FAILED` — proving the button, not
   fabricating a result.
7. **Review Workbench** → open a pending record, **Approve** it as the
   logged-in administrator (no free-text name field to type into).
8. **Districts → Refresh certification** → Chennai recomputes to
   `CERTIFIED` once its source is certified.
9. **Publication Control → Publish** → Chennai flips to `PUBLISHED`, blocked
   until both the certification and approval gates above were satisfied.
10. **Operations Dashboard** → reflects all of it: active districts,
    certified sources, documents processed, districts published.

No SQL, no Python REPL, no CLI flags — every step above is a browser click
in the actual running app.

---

## Command reference

### Phase 1

| Command | Purpose |
|---|---|
| `init` | Create the DB and seed official sources |
| `sources list \| add \| disable` | Manage the registry |
| `crawl` / `download` / `parse` | Run one stage (parse now includes OCR + categorization) |
| `run` | All three in one pass |
| `ingest <pdf> --source-id N --source-url U` | Archive a PDF obtained out of band |
| `status` / `queue` / `show <id>` | Inspect the pipeline |
| `approve` / `reject` / `correct` | Verification decisions |
| `verified` | Dump the Verified GO Database as JSON |
| `audit --document-id N` | Full provenance for one document |
| `verify-repo` | Re-hash every archived file |
| `golden init \| score` | CSV-based accuracy harness (see note below) |
| `serve` | Validation Workbench + Certification Dashboard |
| `demo` | Offline end-to-end run |

### Phase 2 — `thirdeye certify ...`

| Command | Purpose |
|---|---|
| `sources [--source-id N]` | Run the 5 certification checks against live sources |
| `status` | Certification summary + per-source detail |
| `progress` | Real GO Acquisition Program progress (200-GO target) |
| `golden add <document_id> --added-by X` | Add a real archived document to the golden set |
| `golden annotate <golden_id> <field> <value> --annotator X [--absent]` | Record ground truth |
| `golden list` / `golden candidates` | Golden set / documents not yet in it |
| `benchmark` | Score the golden set: P/R/F1 + failures + calibration in one pass |
| `benchmark-show [--run-id N]` | Print a past run's full JSON summary |
| `failures [--type/--field/--department/--language]` | Query recorded failures |
| `calibration` | Stated confidence vs. actual accuracy, by bucket |

---

## Golden datasets — two, on purpose

**`thirdeye golden ...`** (Phase 1) is a CSV-driven harness against
*synthetic* fixtures (`sampledata.py`) — a developer regression tool for the
extraction patterns themselves, never used for certification numbers.

```bash
.venv/Scripts/python.exe -m goengine.cli golden init --dataset golden
```

**`thirdeye certify golden ...`** (Phase 2) is the real one: annotations live
in the database (`golden_annotations`), attached only to documents that came
through actual discovery + acquisition (`golden_documents.document_id` is a
foreign key into `documents` — a synthetic fixture cannot enter this set
short of deliberately running it through `ingest`, at which point it has a
real, verifiable source URL and SHA256 and is no longer synthetic in any way
that matters).

```bash
.venv/Scripts/python.exe -m goengine.cli certify golden candidates
.venv/Scripts/python.exe -m goengine.cli certify golden add 3 --added-by alex
.venv/Scripts/python.exe -m goengine.cli certify golden annotate 1 go_number "G.O.(Ms) No.123" --annotator alex
.venv/Scripts/python.exe -m goengine.cli certify benchmark
```

Both harnesses share the same field-appropriate comparison: GO numbers match
on digits + series, dates on parsed value, budgets on rupee amount, subjects
on containment or ≥90% token overlap. A blank/absent annotation is scored
correct only if the extractor also reports nothing — measuring hallucination,
not just recall.

| Field | Phase 1 target | Field | Phase 2 target |
|---|---|---|---|
| GO Number | 99% | Budget | 95% |
| GO Date | 99% | District | 95% |
| Department | 99% (98% Phase 2) | Scheme Name | 90% |
| Subject | 95% | | |

`golden score` / `certify benchmark` both exit non-zero when targets are not
met, so either can gate CI.

---

## Tests

```bash
.venv/Scripts/python.exe -m pytest
```

284 tests, fully offline — `OfflineFetcher` serves canned responses and
`sampledata.py` renders realistic two-page GO PDFs, so the whole pipeline is
exercised without touching a government server. The one exception, by
necessity, is Module 4: one test makes a real call into the locally
installed Tesseract binary (skipped automatically if it isn't present) to
prove OCR genuinely recovers text from an image-only PDF — everything
downstream of that (the merge into `extraction_pages`, categorization,
benchmarking) is still exercised offline. The HTTP-triggered "run live
certification" button is tested the same way: the route's fetcher is a
FastAPI dependency, overridden with the offline fixture in tests so the
suite never reaches the real internet, while still proving the actual route
logic (not a mock of it). Phase 3's background job runner gets the same
treatment via an injectable `fetcher_factory` (a background thread can't use
FastAPI's per-request dependency overrides, since it outlives the request).

The suite covers the governance rules directly: unofficial sources rejected,
documents un-overwritable, audit log un-editable, corrections
non-destructive, nothing invented from an empty document, a golden document
must reference a real archived document, a wrong/hallucinated/missed
prediction is scored and classified correctly, source versions are
append-only, publication is refused without certification/approval, RBAC
denies a role its write actions with a real 403 (not just a hidden button),
and a state_admin cannot act outside their assigned state.

---

## Status against the exit criteria

### Phase 1

| Criterion | Status |
|---|---|
| Official source registry operational | ✅ |
| Documents automatically discovered | ✅ engine complete; **live portal selectors need confirmation** |
| PDFs downloaded and archived | ✅ |
| Metadata extraction works reliably | ✅ on synthetic fixtures; **needed the real golden dataset — see Phase 2** |
| Validation workbench functional | ✅ |
| Audit trail available | ✅ |
| Accuracy targets achieved | ⚠️ **only against synthetic fixtures — see Phase 2** |

### Phase 2

| Criterion | Status |
|---|---|
| 10+ sources certified | ❌ **engine complete and tested; zero real certifications run** |
| 200+ real Government Orders | ❌ **acquisition tooling complete; zero real documents acquired** |
| OCR implemented and validated | ✅ implemented, proven against a real scanned PDF; ⚠️ not validated against real scanned GOs |
| Tamil support validated | ✅ classification implemented and tested; ⚠️ not validated against real Tamil GOs |
| Accuracy meets all target thresholds | ❌ **cannot be claimed — no real golden documents exist yet** |
| 100% provenance coverage | ✅ every Phase 2 table extends the same audit trail as Phase 1 |
| Benchmarking uses real documents only | ✅ structurally enforced (FK into `documents`), not just a process rule |

**Phase 2 is a certification *engine*, not a certification.** Every module
the blueprint asked for is built, wired together, and covered by tests that
exercise real logic (including a real Tesseract OCR call) — but "10 sources
certified" and "200 real GOs" are claims about the *world*, not the code, and
nothing in this repository can manufacture that evidence. That has to happen
next, as an actual operational exercise:

1. **Run `thirdeye certify sources` against the seeded URLs** (and fix the
   ones that fail — Phase 1's README already flagged these as unverified) to
   accumulate the 10 certified sources.
2. **Run `thirdeye run` for real** across those certified sources, then
   `thirdeye certify golden add` + `certify golden annotate` by hand against
   real archived orders — 50 each in Health, Education, Public Works, Rural
   Development — to build the 200-document acquisition target and its
   golden subset.
3. **Run `thirdeye certify benchmark`** against that real golden set and see
   where the *actual* numbers land. Expect them to be lower than the
   synthetic-fixture numbers, especially for scanned and Tamil-language
   orders — that gap is exactly what Module 7 (Failure Intelligence) and
   Module 8 (Confidence Calibration) exist to surface and quantify.

Only after that real measurement meets the blueprint's thresholds should
citizen-facing work begin, per the blueprint's own governance rule: "No
citizen-facing feature may be developed until extraction accuracy is
validated against real government documents." (Phase 3, built next, is
still pre-citizen-launch infrastructure — the Operations Control Center
that makes running that real measurement possible without a CLI.)

### Phase 3

| Criterion | Status |
|---|---|
| State Registry Operational | ✅ |
| District Registry Operational | ✅ |
| Department Registry Operational | ✅ |
| Source Registry Operational | ✅ |
| Source Versioning Operational | ✅ append-only, proven with an `sqlite3.IntegrityError` test on direct UPDATE/DELETE |
| Source Certification UI Operational | ✅ wraps Phase 2's engine; live-tested against a real (unverified) URL, returned a real `FAILED` |
| Certification Job Center Operational | ✅ background thread, DB-backed live progress, tested via HTTP polling |
| Review Workbench Operational | ✅ typed queues + escalation, on top of Phase 1's existing queue |
| Publication Control Operational | ✅ certification+approval+provenance gates enforced in code, not just UI copy |
| Audit Center Operational | ✅ same append-only log every phase writes to, now filterable |
| User & Role Management Operational | ✅ 5 roles, session auth, scrypt hashing, state-scoped admin, all RBAC-tested |
| System Health Monitoring Operational | ✅ source availability / OCR / storage / queue depth, computed from existing state |

**Every Phase 3 module is built, wired into a real running app, and covered
by tests that exercise the actual HTTP routes** — not just the underlying
Python functions. The [First Operational Scenario](#first-operational-scenario-phase-3)
above was walked through a real browser against a real running server, not
narrated from the plan.

What Phase 3 does **not** claim:

- **Real operational adoption.** The exit criteria list UI *capability*, not
  organizational rollout — an actual state government team using this
  day-to-day is a separate, much larger undertaking than shipping the screens.
- **The Certification Job Center's background execution is a daemon thread,
  not a task queue.** It has no retry policy, no worker pool, and doesn't
  survive a process restart mid-job (the job row would just stay `RUNNING`
  forever). Fine for a single-operator POC; a production deployment with
  concurrent jobs and real durability guarantees would want Celery/RQ or
  similar.
- **`/api/health` is an authenticated operator dashboard, not a liveness
  probe.** Infrastructure monitoring (uptime checks, paging) is a different,
  unauthenticated, much simpler concern this endpoint deliberately doesn't try
  to be.

### Phase 3.1

| Criterion | Status |
|---|---|
| Third Eye brand (icon only, no wordmark/tagline reused) | ✅ original SVG mark, not the reference asset |
| 6-theme system (Light/Dark/Glass + Aurora/Emerald/Midnight) | ✅ all six pass WCAG AA contrast (text ≥4.5:1, primary buttons ≥6:1), verified against the literal CSS token values |
| Public landing page | ✅ hero, evidence-flow, live metrics wired to real data — no mocked numbers |
| Redesigned login + role-based routing | ✅ browser-verified for `platform_admin`/`reviewer`/`auditor` destinations |
| Executive Command Center restyle | ✅ KPI cards, progress rings, alerts, recent-activity feed — all composed from existing Phase 2/3 data functions |
| Responsive (desktop/tablet/mobile) | ✅ spot-checked at 375/768/1280px; no horizontal overflow, mobile nav collapses correctly |
| Accessibility | ⚠️ **partial** — focus-visible outlines, `aria-pressed`/`aria-expanded` states, and labeled form fields are in place and were checked; this was a manual spot-check, not a full automated audit (e.g. axe-core) or a screen-reader pass |
| Deployment readiness prep | ⚠️ **prep only, not a migration** — see below; Firebase/Firestore migration is explicitly the next activity, not part of Phase 3.1 |

### Before this is exposed beyond localhost

Real session auth now exists (Phase 3), which closes the biggest gap from
Phase 1/2. Phase 3.1 made the session cookie's `Secure` attribute
environment-driven (`THIRDEYE_ENV=production` → `secure=True`; the default,
`development`, keeps it `False` so local `http://localhost` still works) —
but a deployment still has to remember to set that variable, and a few
things still matter beyond it: there is no password-reset flow, no rate
limiting on `/login`, and no CSRF protection on the form-based POST routes
(mitigated somewhat by `samesite=lax` cookies, not eliminated).

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `THIRDEYE_DATA_DIR` | `./data` | Where the SQLite DB and document repository live |
| `THIRDEYE_ENV` | `development` | Set to `production` when serving over real TLS — flips the session cookie's `Secure` attribute on |
| `THIRDEYE_TESSERACT_CMD` | auto-detected | Path to the Tesseract binary, if not on `PATH` |
| `THIRDEYE_TESSDATA_DIR` | `vendor/tessdata/` | Where `eng.traineddata`/`tam.traineddata` live |

None of these are secrets — there is no `SECRET_KEY` in this codebase to
manage, because sessions are opaque random tokens looked up in the `sessions`
table (`secrets.token_urlsafe`), not signed/encrypted cookies. A production
deployment's actual remaining checklist is: put a real TLS-terminating
reverse proxy in front, set `THIRDEYE_ENV=production`, point
`THIRDEYE_DATA_DIR` at persistent storage, and address the auth gaps listed
above — none of which is Firebase/Firestore-shaped, which is why that
migration is scoped as its own separate activity rather than folded in here.
