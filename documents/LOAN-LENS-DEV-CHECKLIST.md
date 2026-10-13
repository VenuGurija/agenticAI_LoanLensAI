# Loan Lens AI — Development Checklist

**Repository:** `loanlens-ai`  
**Companion spec:** `documents/LOAN-LENS-SPEC.md`  
**Rule:** Implement **one session at a time**. Do not start the next session until this file marks the current session complete.  
**Current session:** none. Session 16.7 is the last session.

All sessions start as **Not Started**. Change a session to **Complete** only after its Verify commands succeed.

Status legend:

- ⬜ Not Started
- 🔧 In Progress
- ✅ Complete
- ⛔ Blocked

---

## Progress Dashboard

| Session | Name | Status |
|---------|------|--------|
| 0.1 | Repository/application skeleton | ✅ Complete |
| 1.1 | FastAPI app, config, health | ✅ Complete |
| 1.2 | Error envelope, IDs, PII-safe logging | ✅ Complete |
| 1.3 | SQLite persistence for analyses and images | ✅ Complete |
| 2.1 | React mobile app shell and routes | ✅ Complete |
| 2.2 | Typed API client and session analysis id | ✅ Complete |
| 3.1 | Image upload and delete API | ✅ Complete |
| 3.2 | Mobile upload UI | ✅ Complete |
| 4.1 | Image type/size/dimension validation | ✅ Complete |
| 4.2 | Preprocess: EXIF strip, downscale, checksum | ✅ Complete |
| 5.1 | VisionProvider interface and FakeVisionProvider | ✅ Complete |
| 6.1 | Extraction / money / date Pydantic schemas | ✅ Complete |
| 6.2 | Vision JSON contracts and prompt constants | ✅ Complete |
| 7.1 | OpenAI-compatible vision adapter | ✅ Complete |
| 8.1 | Document classification pipeline step | ✅ Complete |
| 9.1 | Lender identity normalization | ✅ Complete |
| 9.2 | Loan grouping and field merge | ✅ Complete |
| 10.1 | Money/date parsing and calculated fields | ✅ Complete |
| 10.2 | Deterministic financial validation rules | ✅ Complete |
| 11.1 | Summary formatter and summary API | ✅ Complete |
| 11.2 | Mobile results (grouped summary) UI | ✅ Complete |
| 11.3 | Loan detail UI | ✅ Complete |
| 12.1 | Review flags and terminal analysis status | ✅ Complete |
| 12.2 | Review badges and unknown-value UI | ✅ Complete |
| 12.3 | Save corrections and same-lender hints | ✅ Complete |
| 13.1 | Retention, purge, secrets, log redaction | ✅ Complete |
| 14.1 | Fake-provider analyze integration tests | ✅ Complete |
| 14.2 | Frontend tests for summary and unknowns | ✅ Complete |
| 15.1 | Limits, CORS, runbook, production checks | ✅ Complete |
| 16.1 | Queue statuses, enqueue Analyze, release DB | ✅ Complete |
| 16.2 | In-process worker (one draft at a time) | ✅ Complete |
| 16.3 | Per-image vision checkpoints | ✅ Complete |
| 16.4 | Book inbox API | ✅ Complete |
| 16.5 | Book inbox UI, leave-and-return, Retry | ✅ Complete |
| 16.6 | Worker tests, vision timeout env, README | ✅ Complete |
| 16.7 | Phase progress line on inbox and preview | ✅ Complete |

When a session is completed, set **Current session** at the top of this file to the next ⬜ session. After 16.7 there is no further session.

---

## Phase 0 — Foundation

### Session 0.1 — Repository/application skeleton

- **Status:** ✅ Complete
- **Goal:** Create the empty repository layout so later sessions have a stable place to put code. No HTTP behavior, no UI behavior, no LLM, no database runtime required beyond folders and stub package files.
- **SPEC sections used:** 5 (Technology Stack), 16 (secrets file names only), 20 (Project Directory Structure)
- **Exact files to create/edit:**
  - `README.md`
  - `.gitignore`
  - `.env.example`
  - `backend/pyproject.toml`
  - `backend/app/__init__.py`
  - `backend/tests/__init__.py`
  - `frontend/package.json`
  - `frontend/index.html`
  - `data/tmp/images/.gitkeep`
  - `backend/app/services/grouping/lender_aliases.json` (empty list `[]`)
- **Do not create in this session:** FastAPI routes, React components, vision clients, SQLite usage, application dependencies installation beyond what is required to show the files exist.
- **Implementation tasks:**
  1. Add `.gitignore` covering `.env`, `data/*.db`, `data/tmp/images/*` (keep `.gitkeep`), `node_modules`, `__pycache__`, `dist`, `.venv`.
  2. Add `.env.example` with the variable **names** from SPEC §5 (including `LOANLENS_CORS_ORIGINS` and `VITE_API_BASE_URL`) and empty/placeholder values (no real secrets).
  3. Add `backend/pyproject.toml` with project name `loanlens-ai-backend`, `requires-python = ">=3.12"`, and a **commented or listed-but-not-installed** intended dependency set matching SPEC §5 (fastapi, uvicorn, pydantic, sqlalchemy, pillow, pytest, httpx). Do not add RAG/vector/kafka libraries.
  4. Add `frontend/package.json` with name `loanlens-ai-frontend`, private, and placeholder scripts `"dev"`, `"build"`, `"test"` that can be filled in Session 2.1. Do not add UI framework code yet beyond `index.html` title `Loan Lens AI`.
  5. Add a short `README.md` that states: spec-driven project, do not implement features except via the checklist, pointers to `documents/LOAN-LENS-SPEC.md` and this checklist, V1 is mobile-first loan screenshot analysis.
  6. Create directory placeholders listed above.
- **Tests:** None automated yet. Manual file presence is the test.
- **Verify commands:**
  - From repo root (PowerShell):
    - `Test-Path README.md, .gitignore, .env.example, backend/pyproject.toml, backend/app/__init__.py, backend/tests/__init__.py, frontend/package.json, frontend/index.html, data/tmp/images/.gitkeep, backend/app/services/grouping/lender_aliases.json`
    - `Get-Content backend/app/services/grouping/lender_aliases.json` must be `[]`
    - `Select-String -Path .gitignore -Pattern "^\.env$"`
    - Confirm `.env.example` contains `VISION_API_KEY` and `VISION_PROVIDER`
- **Expected output:** All `Test-Path` items `True`. Alias file is `[]`. `.env` is gitignored. README mentions the spec and checklist. No FastAPI `app = FastAPI()` and no React components exist yet.

---

## Phase 1 — Backend foundation

### Session 1.1 — FastAPI app, config, health

- **Status:** ✅ Complete
- **Goal:** Runnable backend that loads env config and serves `GET /api/v1/health`.
- **SPEC sections used:** 5, 14.1, 20, 23
- **Exact files to create/edit:**
  - `backend/pyproject.toml` (real dependencies: fastapi, uvicorn, pydantic-settings)
  - `backend/app/core/config.py`
  - `backend/app/api/health.py`
  - `backend/app/main.py`
  - `backend/tests/test_health.py`
  - `backend/tests/conftest.py`
- **Implementation tasks:**
  1. Settings class reading variables from SPEC §5 with documented defaults.
  2. FastAPI app titled `Loan Lens AI`.
  3. Health returns `{"status":"ok"}` and does **not** call a vision provider.
  4. pytest client test for health.
- **Tests:** `backend/tests/test_health.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_health.py -q`
- **Expected output:** pytest pass. Health JSON `status=ok`.

### Session 1.2 — Error envelope, IDs, PII-safe logging

- **Status:** ✅ Complete
- **Goal:** Shared error JSON, ULID helpers, logging that records ids and error codes only.
- **SPEC sections used:** 14.2, 14.8, 14.9, 16, 23
- **Exact files to create/edit:**
  - `backend/app/core/errors.py`
  - `backend/app/core/ids.py`
  - `backend/app/core/logging.py`
  - `backend/app/main.py` (exception handlers)
  - `backend/tests/test_errors.py`
  - `backend/tests/test_ids.py`
- **Implementation tasks:**
  1. `AppError` with `code`, `message`, `retryable`, HTTP status from SPEC table.
  2. Handlers emit the error envelope; never attach request bodies.
  3. IDs: `bok_`, `anl_`, `img_`, `lon_`, `ldg_` + ULID.
  4. Configure structured logging; unit-test that a helper `safe_log_extra` does not accept keys `borrower_name`, `loan_id`, `amount` (reject or drop).
- **Tests:** `test_errors.py`, `test_ids.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_errors.py tests/test_ids.py -q`
- **Expected output:** Unknown path still 404 with envelope or FastAPI default replaced for `AppError`. IDs match prefixes.

### Session 1.3 — SQLite persistence for analyses and images

- **Status:** ✅ Complete
- **Goal:** Create/read/delete a book that does not expire, and a draft analysis that does, per SPEC §15.
- **SPEC sections used:** 14.3, 15, 18 (`created` only at this stage)
- **Exact files to create/edit:**
  - `backend/app/models/enums.py`
  - `backend/app/models/database.py`
  - `backend/app/persistence/analyses.py`
  - `backend/app/api/analyses.py` (`POST /api/v1/books`, `GET` book, `POST` draft, `GET`/`DELETE` draft, without images)
  - `backend/app/schemas/analysis.py` (create/get stubs)
  - `backend/tests/test_analyses_crud.py`
- **Implementation tasks:**
  1. Tables `books`, `analyses`, and `images` as specified. `books` has no `expires_at`.
  2. `POST /api/v1/books` returns `201` with `book_id` and `created_at` only.
  3. `POST /api/v1/books/{book_id}/drafts` returns `201` with `analysis_id`, `status=created`, and `expires_at`.
  4. `GET` book 404 `BOOK_NOT_FOUND` for an unknown book. `GET` draft 404 `ANALYSIS_NOT_FOUND` for an unknown draft.
  5. `DELETE` draft removes that draft row. Do not delete accepted book loans in this session. File cleanup may be a no-op until 13.1 if no files yet.
  6. Enums: `AnalysisStatus` including `accepted`, and `ImageStatus`, from SPEC §15.2.
  7. No list-all-books and no list-all-analyses route.
- **Tests:** `test_analyses_crud.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_analyses_crud.py -q`
- **Expected output:** Create book → create draft → get both → delete draft. GET draft after delete is 404. GET book still succeeds. No list-all route exists.

---

## Phase 2 — Frontend/mobile foundation

### Session 2.1 — React mobile app shell and routes

- **Status:** ✅ Complete
- **Goal:** Vite + React + TypeScript app with a mobile-first shell and empty screens for the book, upload, preview, and loan detail routes.
- **SPEC sections used:** 5, 19
- **Exact files to create/edit:**
  - `frontend/package.json`
  - `frontend/vite.config.ts`
  - `frontend/tsconfig.json`
  - `frontend/index.html`
  - `frontend/src/main.tsx`
  - `frontend/src/App.tsx`
  - `frontend/src/screens/BookScreen.tsx`
  - `frontend/src/screens/UploadScreen.tsx`
  - `frontend/src/screens/PreviewScreen.tsx`
  - `frontend/src/screens/LoanDetailScreen.tsx`
  - `frontend/src/styles/tokens.css`
- **Implementation tasks:**
  1. Routes `/` (book), `/add` (upload), `/a/:analysisId` (preview), `/loans/:loanInternalId` (saved loan).
  2. Tokens: spacing, 44px min tap target, single column max-width ~40rem.
  3. Screens can be placeholders with visible titles: `Book`, `Upload`, `Preview`, `Loan detail`.
- **Tests:** Optional smoke: `frontend` build. If Vitest is not yet wired, `npm run build` is the verify.
- **Verify commands:**
  - `cd frontend; npm install; npm run build`
- **Expected output:** Production build succeeds. App title Loan Lens AI.

### Session 2.2 — Typed API client and session analysis id

- **Status:** ✅ Complete
- **Goal:** Frontend types matching SPEC DTOs and a client for health, create book, and get book. Persist `book_id` in `localStorage` key `loanlens.book_id`. Keep an in-progress draft id in `sessionStorage` key `loanlens.analysis_id`.
- **SPEC sections used:** 14, 19.4
- **Exact files to create/edit:**
  - `frontend/src/api/types.ts`
  - `frontend/src/api/client.ts`
  - `frontend/src/screens/BookScreen.tsx` (create the book on first visit when `loanlens.book_id` is absent)
  - `frontend/tests/apiClient.test.ts` (or colocated)
- **Implementation tasks:**
  1. Types for analysis, error envelope, statuses.
  2. `createBook`, `getBook`, `health`.
  3. Base URL from `import.meta.env.VITE_API_BASE_URL` default `http://127.0.0.1:8002`. Vite dev server port `5175`.
  4. Do not store image bytes in `localStorage`. Do not put `book_id` only in `sessionStorage`.
- **Tests:** Mock fetch for createAnalysis.
- **Verify commands:**
  - `cd frontend; npm test` (wire Vitest in this session if build-only in 2.1)
- **Expected output:** Tests pass. Storage key is `loanlens.book_id` in `localStorage`.

---

## Phase 3 — Image upload

### Session 3.1 — Image upload and delete API

- **Status:** ✅ Complete
- **Goal:** Multipart upload `files` onto a draft, delete one draft image, status `images_pending`, enforce max count. Uploading does not change accepted book loans.
- **SPEC sections used:** 14.4, 14.9, 22, 7.2 (count/size MIME light check)
- **Exact files to create/edit:**
  - `backend/app/api/analyses.py`
  - `backend/app/services/images/` (light MIME/size only if needed)
  - `backend/tests/test_image_upload.py`
- **Implementation tasks:**
  1. `POST .../images` field `files`.
  2. Reject `TOO_MANY_IMAGES`, `IMAGE_TOO_LARGE`, `IMAGE_UNSUPPORTED_TYPE`.
  3. `DELETE .../images/{image_id}` only while `created` or `images_pending`.
  4. Analyze still not implemented (404 or 409 `INVALID_STATUS` if called — prefer 409 if analyze route exists as stub; **do not** add analyze pipeline yet).
- **Tests:** `test_image_upload.py` using tiny generated PNG bytes.
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_image_upload.py -q`
- **Expected output:** Upload 1 image → GET shows it. Oversize rejected. Delete works.

### Session 3.2 — Mobile upload UI

- **Status:** ✅ Complete
- **Goal:** Add/remove photos, thumbnails, camera/file input, disabled Analyze until ≥1 image.
- **SPEC sections used:** 4.1, 19
- **Exact files to create/edit:**
  - `frontend/src/components/ImageThumbList.tsx`
  - `frontend/src/screens/UploadScreen.tsx`
  - `frontend/src/api/client.ts` (upload/delete)
- **Implementation tasks:**
  1. Multiple file input + `capture` friendly attributes.
  2. Object URL previews; revoke on remove.
  3. Call upload API; show server errors via envelope `message`.
  4. Analyze button visible but **must not** call a real pipeline until Session 8.1+; it may be disabled with helper text “Analyze arrives in a later session” **or** call POST analyze only after that route exists. Prefer **disabled with label Analyze** until Session 8.1, then enable in 8.1/11.x. For this session: button disabled.
- **Tests:** Component test: adding two files renders two thumbs (if feasible with Testing Library).
- **Verify commands:**
  - `cd frontend; npm test`
- **Expected output:** Tests pass. Analyze disabled. No vision calls.

---

## Phase 4 — Image processing

### Session 4.1 — Image type/size/dimension validation

- **Status:** ✅ Complete
- **Goal:** Pillow decode, min 200px edge, allowlisted MIME, unreadables → `IMAGE_UNREADABLE` / `IMAGE_TOO_SMALL`.
- **SPEC sections used:** 7
- **Exact files to create/edit:**
  - `backend/app/services/images/preprocess.py` (validate functions)
  - `backend/tests/test_image_validation.py`
- **Implementation tasks:**
  1. Pure functions `validate_image_bytes`.
  2. Tests for 200×200 PNG ok, 1×1 PNG fail (`IMAGE_TOO_SMALL`), random bytes fail (`IMAGE_UNREADABLE`).
- **Tests:** `test_image_validation.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_image_validation.py -q`
- **Expected output:** All listed cases pass.

### Session 4.2 — Preprocess: EXIF strip, downscale, checksum

- **Status:** ✅ Complete
- **Goal:** Strip EXIF, downscale longest edge to 2048 for vision copy, SHA-256, persist paths without putting bytes in SQLite.
- **SPEC sections used:** 7.1, 7.4, 16
- **Exact files to create/edit:**
  - `backend/app/services/images/preprocess.py`
  - `backend/app/persistence/analyses.py` (checksum, width, height, storage_path)
  - `backend/tests/test_image_preprocess.py`
- **Implementation tasks:**
  1. Write under `{tmp}/{analysis_id}/{image_id}`.
  2. GPS/EXIF stripped (test with synthetic EXIF if practical; otherwise assert output JPEG/PNG has no `exif` key).
  3. Checksum stored.
- **Tests:** `test_image_preprocess.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_image_preprocess.py -q`
- **Expected output:** Tests pass. No image bytes in DB assertions (query row columns).

---

## Phase 5 — Fake AI provider

### Session 5.1 — VisionProvider interface and FakeVisionProvider

- **Status:** ✅ Complete
- **Goal:** Protocol + fake adapter + factory. Fake maps fixture keys to JSON. Default test provider is fake.
- **SPEC sections used:** 6
- **Exact files to create/edit:**
  - `backend/app/services/vision/base.py`
  - `backend/app/schemas/vision.py`
  - `backend/app/services/vision/fake.py`
  - `backend/app/services/vision/factory.py`
  - `backend/tests/fixtures/vision/classify_loan.json`
  - `backend/tests/fixtures/vision/extract_sample.json`
  - `backend/tests/test_fake_vision.py`
- **Implementation tasks:**
  1. `VisionRequest` / `VisionResponse` types as SPEC §6.2.
  2. Fake never invents amounts not in fixtures.
  3. Factory: `VISION_PROVIDER=fake` | `openai_compatible` (openai class may `NotImplementedError` until 7.1).
- **Tests:** `test_fake_vision.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_fake_vision.py -q`
- **Expected output:** Classify/extract return fixture JSON. Unknown fixture key errors without fabricating loans.

---

## Phase 6 — Extraction schema

### Session 6.1 — Extraction / money / date Pydantic schemas

- **Status:** ✅ Complete
- **Goal:** Canonical Pydantic models for fields, `source`, `confidence`; reject float money; null allowed.
- **SPEC sections used:** 9
- **Exact files to create/edit:**
  - `backend/app/schemas/extraction.py`
  - `backend/tests/test_extraction_schema.py`
- **Implementation tasks:**
  1. `StringField`, `MoneyField`, `DateField`, `EnumField`, `RateField`, `TenureField`, `PenaltyField`, `ExtractedLoan`, `ImageExtractResult`.
  2. `source`: `extracted` | `calculated` | `user_corrected` | `unknown`.
  3. Money `value` as decimal string or null.
- **Tests:** Parse sample JSON from SPEC §9.7; invalid float type fails or is coerced only from int/string, never invent.
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_extraction_schema.py -q`
- **Expected output:** Fixture JSON validates. Missing fields → null, not default `0`.

### Session 6.2 — Vision JSON contracts and prompt constants

- **Status:** ✅ Complete
- **Goal:** JSON schema (or Pydantic dump) plus system prompt text that forbids invention.
- **SPEC sections used:** 6.4, 6.5, 8.1, 9.7
- **Exact files to create/edit:**
  - `backend/app/services/vision/contracts.py`
  - `backend/app/services/vision/prompts.py`
  - `backend/tests/test_prompts.py`
- **Implementation tasks:**
  1. Separate contracts for `classify` and `extract`.
  2. Prompt includes “If a field is not clearly present, return null”.
  3. Test: prompt string contains `null` instruction and does not ask the model to calculate fees.
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_prompts.py -q`
- **Expected output:** Tests pass.

---

## Phase 7 — Real vision provider

### Session 7.1 — OpenAI-compatible vision adapter

- **Status:** ✅ Complete
- **Goal:** `OpenAICompatibleVisionProvider` using env key/base URL/model, 60s timeout, one JSON retry, no key in logs/errors.
- **SPEC sections used:** 6, 17, 22
- **Exact files to create/edit:**
  - `backend/app/services/vision/openai_compatible.py`
  - `backend/app/services/vision/factory.py`
  - `backend/tests/test_openai_compatible_vision.py`
- **Implementation tasks:**
  1. HTTP mock (respx or httpx mock) for success, non-JSON, 503.
  2. Non-JSON → retry once → `VisionResponseInvalidError`.
  3. 503 → `VISION_UNAVAILABLE` mapping at API boundary may wait until analyze session; at this layer raise typed errors.
  4. Never implement other vendors.
- **Tests:** `test_openai_compatible_vision.py` (mocked network only)
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_openai_compatible_vision.py -q`
- **Expected output:** No real network. Key not in exception messages.

---

## Phase 8 — Document classification

### Session 8.1 — Document classification pipeline step

- **Status:** ✅ Complete
- **Goal:** Add `POST /api/v1/analyses/{id}/analyze` that preprocesses, classifies, and (when any image is `loan_document`) extracts raw per-image JSON onto the **draft**. Do **not** copy loans into the book, group lenders, or run money validation in this session.
- **SPEC sections used:** 8, 13, 18
- **Exact files to create/edit:**
  - `backend/app/services/classification.py`
  - `backend/app/services/extraction.py` (vision extract + schema parse only)
  - `backend/app/api/analyses.py` (analyze)
  - `backend/tests/test_classification_analyze.py`
- **Implementation tasks:**
  1. Analyze requires ≥1 image else `NO_IMAGES`.
  2. All `not_loan_document` → `failed` / `NO_LOAN_DOCUMENTS`.
  3. Only `unclear` → `completed_needs_review`, empty `lender_groups`, no extract call.
  4. At least one `loan_document` → classify then extract those images; store `result_json.raw_extracts`; `lender_groups` may be `[]` until Session 9.2; terminal status `completed_needs_review` is acceptable.
  5. Idempotent: second analyze on a terminal status does not call vision again (assert mock call count).
- **Tests:** Fake provider fixtures for the three classify outcomes.
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_classification_analyze.py -q`
- **Expected output:** Three outcomes match SPEC §8.2. Fake provider invoked. No grouping keys required yet except empty `lender_groups`.

---

## Phase 9 — Lender / loan grouping

### Session 9.1 — Lender identity normalization

- **Status:** ✅ Complete
- **Goal:** Canonical brand keys, unknown group `_unknown_lender`, alias file support.
- **SPEC sections used:** 10.2, 10.5
- **Exact files to create/edit:**
  - `backend/app/services/grouping/group.py` (normalize helpers)
  - `backend/app/services/grouping/lender_aliases.json`
  - `backend/tests/test_lender_normalize.py`
- **Implementation tasks:**
  1. Trim, casefold, collapse whitespace.
  2. Alias map applied after normalize.
  3. Display still uses original extracted strings when present; grouping key is canonical.
- **Tests:** `test_lender_normalize.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_lender_normalize.py -q`
- **Expected output:** `" DhanRishi "` groups with `"dhanrishi"`. Empty brand+entity → `_unknown_lender`.

### Session 9.2 — Loan grouping and field merge

- **Status:** ✅ Complete
- **Goal:** Pages → loans → lender groups on the draft preview; conflicts do not merge; FIELD_CONFLICT clears values. Grouping is a proposal. Include a pure function that reapplies a user image-to-loan assignment and re-merges. Do not add the HTTP route in this session.
- **SPEC sections used:** 10
- **Exact files to create/edit:**
  - `backend/app/services/grouping/group.py`
  - `backend/app/services/extraction.py` (merge into GET shape)
  - `backend/tests/test_grouping.py`
- **Implementation tasks:**
  1. Implement identity rules in SPEC §10.2–10.4.
  2. LLM `same_loan_as_image_ids` is hint only.
  3. Sort groups/loans per §10.5.
  4. Wire into analyze after extract.
- **Tests:** Two pages same loan_id merge; conflicting sanctioned amounts do not merge / FIELD_CONFLICT.
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_grouping.py -q`
- **Expected output:** Tests encode SPEC merge rules. No invented third amount.

---

## Phase 10 — Financial validation

### Session 10.1 — Money/date parsing and calculated fields

- **Status:** ✅ Complete
- **Goal:** Decimal parsing; calculated `implied_fees_total`, `fee_plus_gst`, `implied_cost`, and `extra_percent` only when inputs exist; short sanctioned display helper. `extra_percent` is null when disbursed is null, zero, or negative.
- **SPEC sections used:** 9.3, 9.4, 9.6, 11.3
- **Exact files to create/edit:**
  - `backend/app/services/validation/money.py`
  - `backend/app/services/summary.py` (short amount helper may live here)
  - `backend/tests/test_calculated_fields.py`
- **Implementation tasks:**
  1. No float arithmetic.
  2. Null in → calculated unknown.
  3. Do not treat disbursed as sanctioned.
- **Tests:** `test_calculated_fields.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_calculated_fields.py -q`
- **Expected output:** Sanctioned 15000 and disbursed 13230 → implied fees 1770.00. Repayment 16950 and disbursed 13230 → implied cost 3720.00 and extra percent 28.1. Missing disbursed → implied fees, implied cost, and extra percent null.

### Session 10.2 — Deterministic financial validation rules

- **Status:** ✅ Complete
- **Goal:** Implement all `V_*` checks with `passed`/`failed`/`skipped`.
- **SPEC sections used:** 11
- **Exact files to create/edit:**
  - `backend/app/services/validation/rules.py`
  - `backend/tests/test_validation_rules.py`
- **Implementation tasks:**
  1. One test per check ID including skipped.
  2. ₹1.00 tolerance only for `V_FEES_RECONCILE`.
  3. Failed checks do not rewrite extracted amounts.
  4. Attach `validations` and `validation_summary` on each loan during analyze.
- **Tests:** `test_validation_rules.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_validation_rules.py -q`
- **Expected output:** All IDs covered. Disbursed > sanctioned fails `V_DISBURSED_LTE_SANCTIONED`.

---

## Phase 11 — Results UI

### Session 11.1 — Summary formatter and summary API

- **Status:** ✅ Complete
- **Goal:** Backend `summary_line` plus draft `GET .../analyses/{id}/summary` and book `GET .../books/{id}/summary` matching SPEC §4.2, §14.7, and §14.10. The extra clause appears only when both extra rupees and extra percent exist. Book totals sum only non-null inputs and set `incomplete` when any accepted loan is missing disbursed or repayment.
- **SPEC sections used:** 4.2, 14.7, 19.3
- **Exact files to create/edit:**
  - `backend/app/services/summary.py`
  - `backend/app/api/analyses.py`
  - `backend/tests/test_summary.py`
- **Implementation tasks:**
  1. Omit unknown pieces; do not print “null”.
  2. Unknown sanctioned → line starts with `amount unknown`.
  3. Unsummarizable loan → `Needs review`.
  4. Header `brand (legal)` when legal known.
  5. Book summary includes `totals.received`, `totals.still_to_pay`, `totals.extra`, and `totals.incomplete`.
- **Tests:** `test_summary.py` using the three illustrative lenders **only as unit examples** (not product requirements for those brands).
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_summary.py -q`
- **Expected output:** Example lines match format, including `extra 3720 (28.1%)` when both amounts exist. Missing disbursed omits `got …` and omits the extra clause.

### Session 11.2 — Mobile results (grouped summary) UI

- **Status:** ✅ Complete
- **Goal:** Book screen renders totals, groups, and lines. Analyze on the upload screen opens the preview. **Add to book** accepts the draft and returns to `/`. Removing a loan deletes only that loan.
- **SPEC sections used:** 4, 14.10, 19
- **Exact files to create/edit:**
  - `frontend/src/screens/BookScreen.tsx`
  - `frontend/src/screens/PreviewScreen.tsx`
  - `frontend/src/screens/UploadScreen.tsx` (enable Analyze → POST analyze → navigate to the preview)
  - `frontend/src/components/SummaryGroup.tsx`
  - `frontend/src/api/client.ts`
- **Implementation tasks:**
  1. Prefer backend summary DTO text, including totals.
  2. Progress label while request in flight.
  3. Failed status shows `error.message`, not a fake summary, and does not change the book.
  4. **Remove loan** calls `DELETE /api/v1/books/{book_id}/loans/{loan_id}` and leaves `loanlens.book_id` in place.
  5. A new upload creates a draft on the existing book. It does not replace accepted loans.
- **Tests:** Render groups from fixture DTO, including the three totals.
- **Verify commands:**
  - `cd frontend; npm test`
- **Expected output:** Fixture with two groups renders two headers and the totals.

### Session 11.3 — Loan detail UI

- **Status:** ✅ Complete
- **Goal:** Detail screen sections Extracted / Calculated / Checks / Review, a screenshot button, and a way to correct a wrong amount before or after accept.
- **SPEC sections used:** 4, 9, 10.6, 11, 12.3, 14.10, 19
- **Exact files to create/edit:**
  - `frontend/src/screens/LoanDetailScreen.tsx`
  - `frontend/src/screens/PreviewScreen.tsx` (grouping correction and field correction on the preview)
  - `frontend/src/components/ReviewBadge.tsx` (placeholder ok if 12.2 restyles it)
  - `backend/app/api/analyses.py` (`PATCH` loan, `POST` grouping, image GET)
- **Implementation tasks:**
  1. Tap a summary line to detail.
  2. Null shown as `unknown`, never `0` unless the stored value is 0.
  3. Show `source` on fields, including `user_corrected`.
  4. Screenshot button loads `GET /api/v1/books/{book_id}/loans/{loan_id}/images/{image_id}` and keeps the image hidden until the tap.
  5. Saving a correction calls the preview or book `PATCH` and shows the returned extra rupees and percentage.
  6. Moving a draft image between proposed loans calls `POST .../grouping`.
- **Tests:** Detail render with mixed extracted, calculated, `user_corrected`, and unknown values. The screenshot image is absent until the audit button is clicked.
- **Verify commands:**
  - `cd frontend; npm test`
- **Expected output:** Calculated section distinct from extracted. Image is hidden until the button is used.

---

## Phase 12 — Confidence / review

### Session 12.1 — Review flags and terminal analysis status

- **Status:** ✅ Complete
- **Goal:** Apply SPEC §12 flags; `completed` vs `completed_needs_review`.
- **SPEC sections used:** 12, 18
- **Exact files to create/edit:**
  - `backend/app/services/extraction.py` or `backend/app/services/review.py`
  - `backend/tests/test_review_flags.py`
- **Implementation tasks:**
  1. Implement flag table §12.1.
  2. `needs_review` rules §12.2.
  3. `completed` only when ≥1 loan and none need review.
- **Tests:** `test_review_flags.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_review_flags.py -q`
- **Expected output:** Null disbursed ⇒ needs_review. Clean fixture ⇒ `completed`.

### Session 12.2 — Review badges and unknown-value UI

- **Status:** ✅ Complete
- **Goal:** Visible Needs review on analysis header and loans; muted unknown labels.
- **SPEC sections used:** 12.3, 19
- **Exact files to create/edit:**
  - `frontend/src/components/ReviewBadge.tsx`
  - `frontend/src/screens/BookScreen.tsx`
  - `frontend/src/screens/PreviewScreen.tsx`
  - `frontend/src/screens/LoanDetailScreen.tsx`
  - `frontend/tests/reviewUi.test.ts`
- **Implementation tasks:**
  1. Do not auto-fill numbers.
  2. Badge uses the words `Needs review`.
- **Tests:** `completed_needs_review` fixture shows badge; `completed` does not.
- **Verify commands:**
  - `cd frontend; npm test`
- **Expected output:** Tests pass.

### Session 12.3 — Save corrections and same-lender hints

- **Status:** ✅ Complete
- **Goal:** Persist each user correction on the loan and as a same-lender hint. The next extract prompt for that lender includes those hints. Other lenders do not receive them. Do not fine-tune a model.
- **SPEC sections used:** 12.4, 14.11, 6.4
- **Exact files to create/edit:**
  - `backend/app/services/vision/prompts.py`
  - `backend/app/persistence/analyses.py`
  - `backend/tests/test_correction_hints.py`
- **Implementation tasks:**
  1. `PATCH` stores `source=user_corrected` and appends a hint `{ lender_key, field_name, previous_value, corrected_value }` on the book.
  2. Extract prompt builder accepts hints and includes them only when the lender key matches.
  3. A hint for `dhanrishi` is absent from a prompt built for `snappaisa`.
  4. No training job, no embeddings, no extra model call whose only purpose is learning.
- **Tests:** `test_correction_hints.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_correction_hints.py tests/test_prompts.py -q`
- **Expected output:** Same-lender prompt contains the corrected value. Other-lender prompt does not.

---

## Phase 13 — Security

### Session 13.1 — Retention, purge, secrets, log redaction

- **Status:** ✅ Complete
- **Goal:** TTL sweep deletes unaccepted drafts only. Deleting one loan removes that loan’s files and row data. An accepted loan is still readable after 24 hours. Secrets stay out of responses. Logs stay redacted.
- **SPEC sections used:** 15.3, 16, 17
- **Exact files to create/edit:**
  - `backend/app/services/retention.py`
  - `backend/app/persistence/analyses.py`
  - `backend/app/core/logging.py`
  - `.env.example`
  - `backend/tests/test_retention.py`
- **Implementation tasks:**
  1. Sweep on GET or startup: unaccepted draft past `expires_at` → delete that draft’s files, status `expired` or row gone; GET draft 404. Do not delete `{tmp}/books/{book_id}/`.
  2. `DELETE` draft removes `{tmp}/{analysis_id}/`.
  3. `DELETE` loan removes `{tmp}/books/{book_id}/{loan_id}/` and that loan from `books.result_json`. The book id still loads.
  4. Assert `/api/v1/health` and error JSON never include `VISION_API_KEY`.
  5. Confirm `.env` gitignored (already 0.1).
- **Tests:** `test_retention.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_retention.py -q`
- **Expected output:** After TTL, the unaccepted draft is gone and its GET is 404. The accepted book loan and its file are still present.

---

## Phase 14 — Testing

### Session 14.1 — Fake-provider analyze integration tests

- **Status:** ✅ Complete
- **Goal:** End-to-end API: create book, upload a new draft, analyze, preview summary, accept into the book, book summary with totals, delete one loan. Use fake vision fixtures with multiple lenders and multiple loans. The book still loads after the draft would have expired.
- **SPEC sections used:** 13, 14, 21
- **Exact files to create/edit:**
  - `backend/tests/test_analyze_integration.py`
  - `backend/tests/fixtures/vision/*` (additional)
- **Implementation tasks:**
  1. Multi-image draft groups by lender and, after accept, appears on the book.
  2. Assert extracted vs calculated vs validations keys all present, including `implied_cost` and `extra_percent` when both inputs exist.
  3. Assert no real HTTP to vendor.
  4. Conflict fixture keeps null rather than average.
  5. Accepting a second draft adds loans. It does not remove the first loan.
- **Tests:** `test_analyze_integration.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_analyze_integration.py -q`
- **Expected output:** Pass. Book summary groups length ≥ 1 for the happy fixture, and totals are present.

### Session 14.2 — Frontend tests for summary and unknowns

- **Status:** ✅ Complete
- **Goal:** UI tests covering unknown fields, grouping display, error envelope.
- **SPEC sections used:** 4, 12, 21
- **Exact files to create/edit:**
  - `frontend/src/lib/summaryFormat.ts` (only if needed for tests; prefer DTO)
  - `frontend/tests/*.ts`
- **Implementation tasks:**
  1. Unknown not rendered as `0` or `NaN`.
  2. Failed analysis shows message.
- **Verify commands:**
  - `cd frontend; npm test`
- **Expected output:** Pass.

---

## Phase 15 — Production readiness

### Session 15.1 — Limits, CORS, runbook, production checks

- **Status:** ✅ Complete
- **Goal:** Document how to run V1; CORS allowlist; confirm limits from SPEC §22; no extra infrastructure.
- **SPEC sections used:** 5, 16, 22, 23, 24
- **Exact files to create/edit:**
  - `README.md`
  - `backend/app/main.py` (CORS)
  - `backend/app/core/config.py`
  - `backend/tests/test_health.py` (unchanged behavior)
- **Implementation tasks:**
  1. README: env vars, `uvicorn`, `npm run dev`, `VISION_PROVIDER=fake` for local, never commit `.env`.
  2. CORS from config, not `*`, in production default.
  3. Do **not** add RAG, Kafka, Temporal, Redis, or microservices.
  4. Confirm analyze timeout/settings exist.
- **Tests:** CORS preflight unit if practical; health still ok.
- **Verify commands:**
  - `cd backend; python -m pytest -q`
  - `cd frontend; npm test`
  - `cd frontend; npm run build`
- **Expected output:** Full test suites pass. README run steps work on paper. No new infrastructure directories (`kafka/`, `temporal/`, `chroma/`, etc.).

---

## Phase 16 — V1.1 Draft inbox

Do not start these sessions until the user asks to implement V1.1. Spec §3.1 and §13.1 are the source of truth. Do **not** add Kafka, Temporal, Celery, or Redis.

### Session 16.1 — Queue statuses, enqueue Analyze, release DB

- **Status:** ✅ Complete
- **Goal:** `POST .../analyze` returns `202` with `status: queued` and does not call the vision provider. Add `queued` / `running` plus `queued_at`, `pages_done`, `pages_total`. Existing completed-preview behavior stays for already-finished drafts.
- **SPEC sections used:** 3.1, 13.1, 13.2, 14.5, 15.1, 15.2, 18
- **Exact files to create/edit:**
  - `backend/app/models/enums.py`
  - `backend/app/models/database.py`
  - `backend/app/schemas/analysis.py`
  - `backend/app/persistence/analyses.py`
  - `backend/app/api/analyses.py`
  - `backend/app/services/classification.py` (stop calling vision from the request if still inlined)
  - `backend/tests/test_analyze_enqueue.py`
- **Implementation tasks:**
  1. Add enum values `queued` and `running`.
  2. Add columns `queued_at`, `pages_done`, `pages_total` (SQLite-safe migrate or recreate in tests).
  3. `images_pending` or retryable `failed` → set `queued`, return `202`. **No** `VisionProvider` call in this request.
  4. Second POST while `queued`/`running`/pipeline → `202` current draft, no duplicate row.
  5. POST on `completed` / `completed_needs_review` still `200` stored preview.
  6. Do not hold a SQLAlchemy session across a future vision call (prepare helpers; worker uses them in 16.2).
- **Tests:** `test_analyze_enqueue.py` with `FakeVisionProvider` injected but **assert zero vision calls** on enqueue.
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_analyze_enqueue.py -q`
- **Expected output:** Enqueue is `202` / `queued`. Fake provider call count is 0. Completed drafts still return `200`.

### Session 16.2 — In-process worker (one draft at a time)

- **Status:** ✅ Complete
- **Goal:** One asyncio worker in the API process claims the oldest `queued` draft, runs today’s pipeline, never accepts into the book. Startup re-queues stuck `running` / pipeline statuses.
- **SPEC sections used:** 3.1, 13.1, 18
- **Exact files to create/edit:**
  - `backend/app/services/worker.py`
  - `backend/app/main.py` (lifespan)
  - `backend/app/core/config.py` (`LOANLENS_WORKER_ENABLED`)
  - `.env.example`
  - `backend/tests/test_worker.py`
- **Implementation tasks:**
  1. FIFO by `queued_at`. One draft at a time.
  2. Close the DB session before each vision call; write after.
  3. Lifespan starts the worker when `LOANLENS_WORKER_ENABLED=true`. Tests may set it false and call `run_one_queued_draft`.
  4. Worker never calls accept.
  5. No Kafka/Celery/Redis.
- **Tests:** `test_worker.py` with fake vision: enqueue two drafts; after `run_one` only the older is terminal; second still `queued`. Restart helper moves a stuck `running` draft back to `queued`.
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_worker.py tests/test_analyze_enqueue.py -q`
- **Expected output:** One-at-a-time. Book `result_json` unchanged until a later accept test.

### Session 16.3 — Per-image vision checkpoints

- **Status:** ✅ Complete
- **Goal:** Classify and extract **one image per** vision call. Persist checkpoints and `pages_done` / `pages_total`. A failure after image 1 does not drop image 1.
- **SPEC sections used:** 6.5, 13.1, 17
- **Exact files to create/edit:**
  - `backend/app/services/classification.py`
  - `backend/app/services/extraction.py`
  - `backend/app/services/worker.py`
  - `backend/tests/test_checkpoints.py`
- **Implementation tasks:**
  1. Stop batching all images in one classify/extract request.
  2. After each image, commit `raw_extracts` / classify results and increment `pages_done`.
  3. Retry resumes at the first image without a checkpoint.
  4. JSON retry remains once **per image**.
- **Tests:** Fake provider that fails on the second image. After failure, first image’s classify/extract is still stored. Retry does not re-call vision for the first image.
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_checkpoints.py -q`
- **Expected output:** Checkpoint retained. No invented amounts to fill the failed page.

### Session 16.4 — Book inbox API

- **Status:** ✅ Complete
- **Goal:** `GET /api/v1/books/{book_id}/inbox` returns counts and rows per SPEC §14.12. No amounts on the list. Other books’ drafts never appear.
- **SPEC sections used:** 4.5, 14.1, 14.12, 16
- **Exact files to create/edit:**
  - `backend/app/api/analyses.py` (or a books router if that is where book GET lives)
  - `backend/app/schemas/analysis.py`
  - `backend/tests/test_inbox.py`
- **Implementation tasks:**
  1. Bucket mapping exactly as §14.12.
  2. Sort: needs_review, ready, failed, running, queued; oldest `created_at` within a bucket.
  3. Unknown book → `404 BOOK_NOT_FOUND`.
  4. Response JSON has no money fields, borrower, or summary lines.
- **Tests:** `test_inbox.py`
- **Verify commands:**
  - `cd backend; python -m pytest tests/test_inbox.py -q`
- **Expected output:** Counts match fixtures. A second book’s draft is absent.

### Session 16.5 — Book inbox UI, leave-and-return, Retry

- **Status:** ✅ Complete
- **Goal:** Book home shows the count line. User returns to `/` immediately after Analyze `202`. Ready / needs-review rows open the existing preview. Failed shows Retry (same images).
- **SPEC sections used:** 4.1, 4.5, 19
- **Exact files to create/edit:**
  - `frontend/src/components/InboxSummary.tsx`
  - `frontend/src/screens/BookScreen.tsx`
  - `frontend/src/screens/UploadScreen.tsx`
  - `frontend/src/screens/PreviewScreen.tsx`
  - `frontend/src/api/client.ts`
  - `frontend/src/api/types.ts`
  - `frontend/tests/inboxUi.test.ts` (or equivalent)
- **Implementation tasks:**
  1. Count line like `2 ready · 1 needs review · 3 queued`.
  2. Analyze navigates home; no multi-minute spinner.
  3. Retry calls `POST .../analyze` on a failed draft; no re-upload required.
  4. Do not auto-accept.
- **Tests:** Inbox fixture renders counts; completed_needs_review is not labeled Ready.
- **Verify commands:**
  - `cd frontend; npm test`
- **Expected output:** Tests pass. Unknown amounts are not shown on the inbox list.

### Session 16.6 — Worker tests, vision timeout env, README

- **Status:** ✅ Complete
- **Goal:** Wire `LOANLENS_VISION_TIMEOUT_SECONDS` into the OpenAI-compatible adapter. Document V1.1. Keep CI on `VISION_PROVIDER=fake`. Full suites green. No extra infrastructure.
- **SPEC sections used:** 5, 16, 21, 22, 23
- **Exact files to create/edit:**
  - `backend/app/core/config.py`
  - `backend/app/services/vision/openai_compatible.py`
  - `.env.example`
  - `README.md`
  - existing backend/frontend tests if timeouts are hardcoded
- **Implementation tasks:**
  1. Default vision timeout remains 60; env can raise it for Ollama.
  2. README: Analyze enqueues; worker is in-process; inbox is per book; never commit `.env`.
  3. Confirm no `kafka/`, `temporal/`, `redis` service added.
- **Tests:** Existing suites plus Phase 16 tests.
- **Verify commands:**
  - `cd backend; python -m pytest -q`
  - `cd frontend; npm test`
  - `cd frontend; npm run build`
- **Expected output:** All pass. README mentions inbox and enqueue.

### Session 16.7 — Phase progress line on inbox and preview

- **Status:** ✅ Complete
- **Goal:** Queued and in-progress drafts show `{step}/6 {label}` from SPEC §4.6. The step is the processing phase. Screenshot counts stay on the API and are not this line.
- **SPEC sections used:** 4.5, 4.6, 19
- **Exact files to create/edit:**
  - `frontend/src/lib/phaseProgress.ts`
  - `frontend/src/components/InboxSummary.tsx`
  - `frontend/src/screens/PreviewScreen.tsx`
  - `frontend/tests/phaseProgress.test.ts`
  - `frontend/tests/inboxUi.test.tsx`
- **Implementation tasks:**
  1. Map `queued` through `validating` to the six lines in §4.6. `running` and `preprocessing` share `2/6 preparing photos`.
  2. Show that line on queued and running inbox rows and on the preview while the draft is in flight.
  3. Omit the line for ready, needs review, failed, and accepted.
- **Tests:** `phaseProgress.test.ts`, `inboxUi.test.tsx`
- **Verify commands:**
  - `cd frontend; npm test`
- **Expected output:** A queued draft renders `1/6 queued`. `classifying` maps to `3/6 classifying`. `3/8 pages` is not the status line.

---

## Completion rule

A session is complete only when:

1. Listed files exist with the session’s behavior.
2. Verify commands were run and succeeded.
3. This dashboard row is set to ✅ Complete.
4. **Current session** at the top points to the next ⬜ session.
5. No out-of-V1 or future-session behavior was added except empty extension points already named in the spec (e.g. alias JSON file).

Do not mark complete on lint-only or “looks done” without Verify commands.
