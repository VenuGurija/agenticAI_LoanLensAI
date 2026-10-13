# AGENTS.md — Loan Lens AI

This is the operating manual for coding agents working in `loanlens-ai`.

## Source of truth

| Document | Role |
|----------|------|
| `documents/LOAN-LENS-SPEC.md` | Architecture and design source of truth |
| `documents/LOAN-LENS-DEV-CHECKLIST.md` | Implementation source of truth |
| This file | How to work in this repository |
| `.cursor/rules/build-from-spec.mdc` | Always-on reminder of the same rules |

If code and the spec disagree, **change the code** unless the user explicitly asked to revise the spec. If the checklist and spec disagree, **stop and align the documents** before implementing.

## How to work

1. Read **Current session** at the top of `documents/LOAN-LENS-DEV-CHECKLIST.md`.
2. Read that session in full (Goal, SPEC sections, files, tasks, tests, verify, expected output).
3. Read **only** the SPEC sections listed for that session. Do not reload the entire spec unless the session says to or you must resolve a named contradiction.
4. Implement **that session only**.
5. Do not implement later sessions, even if the change looks small or “obvious”.
6. Do not implement Future Roadmap items or SPEC non-goals.
7. Run the session **Verify commands**. Do not declare completion without them.
8. If verification succeeds, update the checklist: session status ✅ Complete, dashboard row, and **Current session** → next ⬜ session.
9. If verification fails, leave the session 🔧 In Progress or ⬜; do not skip ahead.

## Scope discipline

- V1 is defined in SPEC §3. V1.1 (draft inbox) is defined in SPEC §3.1 and checklist Phase 16. Out of scope: accounts/OAuth, RAG, vector databases, embeddings, Kafka, Temporal, Celery, Redis queues, multi-agent systems, microservices, native apps, and anything listed only in SPEC §24.
- Empty extension points already named (e.g. `VisionProvider`, `lender_aliases.json`) may exist; do not fill them with unused platforms.
- Do not add dependencies that the current session did not ask for.

## Domain rules (always)

- **Never silently invent financial or personal information.** Missing values are `null` / unknown and may be flagged for review.
- **Prefer deterministic validation in application code** over asking the LLM to compute fees, GST, APR, or consistency.
- Keep **extracted**, **calculated**, and **validation** data separate (SPEC §9, §11).
- Preserve the distinctions: sanctioned amount ≠ disbursed amount ≠ repayment amount ≠ fees.
- Vision/LLM access **only** through `VisionProvider`. Application services must not import a vendor SDK directly.
- `FakeVisionProvider` is the default for tests. Do not call a live vision API in CI.

## Security and privacy (always)

- Never commit `.env` or real `VISION_API_KEY` values.
- Never put secrets in frontend code, logs, or API responses.
- Do not log image bytes, the Authorization header, or `VISION_API_KEY`. Do not log borrower names, loan IDs, amounts, or model text, except `model_answer` on `vision_response_rejected` when `LOANLENS_ENV=development` (SPEC §16).
- Treat model output as untrusted; schema-validate it; never execute tools from the model.
- Unaccepted draft images are temporary; honor draft delete and draft TTL once those sessions exist. Accepted loan screenshots stay until the user deletes that loan.
- `book_id` is the capability token for the book on this phone. `analysis_id` is the capability token for one draft. Do not add a list-all-books or list-all-analyses API. Do not fine-tune a model from corrections. Same-lender hints are specified in SPEC §14.11.

## Product display

Human-readable summaries are grouped by lender and follow SPEC §4.2, including book totals and extra rupees with percentage when both inputs exist. Omit unknown pieces. Never fabricate a number to make a line look complete. The book stays on this phone until the user removes a loan.

## Definition of done for a session

The session’s Verify commands succeeded and the checklist dashboard was updated. Partial implementations are not complete.
