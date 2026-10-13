import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiClientError,
  ensureBook,
  fetchLoanImage,
  getBook,
  patchBookLoan,
  readBookId,
} from "../api/client";
import type { CalculatedFields, CalculatedValue, Loan, LoanFields } from "../api/types";
import { FieldCorrectionForm, formatReturnedExtra } from "../components/FieldCorrectionForm";
import { ReviewBadge } from "../components/ReviewBadge";

const EXTRACTED_FIELDS: Array<{ key: keyof LoanFields; label: string }> = [
  { key: "lender_brand", label: "Lender" },
  { key: "legal_lending_entity", label: "Legal lender" },
  { key: "borrower_name", label: "Borrower" },
  { key: "loan_id", label: "Loan id" },
  { key: "sanctioned_amount", label: "Sanctioned amount" },
  { key: "disbursed_amount", label: "Disbursed amount" },
  { key: "processing_fee", label: "Processing fee" },
  { key: "gst_amount", label: "GST amount" },
  { key: "other_fees_total", label: "Other fees" },
  { key: "interest_rate", label: "Interest rate" },
  { key: "apr", label: "APR" },
  { key: "tenure", label: "Tenure" },
  { key: "sanction_date", label: "Sanction date" },
  { key: "disbursement_date", label: "Disbursement date" },
  { key: "repayment_amount", label: "Repayment amount" },
  { key: "repayment_date", label: "Repayment date" },
  { key: "repayment_frequency", label: "Repayment frequency" },
];

const CALCULATED_FIELDS: Array<{ key: keyof CalculatedFields; label: string }> = [
  { key: "implied_fees_total", label: "Implied fees" },
  { key: "fee_plus_gst", label: "Fee plus GST" },
  { key: "implied_cost", label: "Extra rupees" },
  { key: "extra_percent", label: "Extra percent" },
  { key: "display_sanctioned_short", label: "Sanctioned short" },
];

export function LoanDetailScreen() {
  const { loanInternalId } = useParams();
  const [bookId, setBookId] = useState<string | null>(readBookId());
  const [loan, setLoan] = useState<Loan | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>("Opening loan…");
  const [extraText, setExtraText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [screenshots, setScreenshots] = useState<Record<string, string>>({});
  const screenshotsRef = useRef(screenshots);
  screenshotsRef.current = screenshots;

  useEffect(() => {
    return () => {
      for (const url of Object.values(screenshotsRef.current)) {
        URL.revokeObjectURL(url);
      }
    };
  }, []);

  useEffect(() => {
    if (!loanInternalId) {
      setStatusMessage(null);
      setErrorMessage("This loan is not in the book.");
      return;
    }
    let cancelled = false;
    setStatusMessage("Opening loan…");
    setErrorMessage(null);
    setLoan(null);
    ensureBook()
      .then((ready) => getBook(ready.book_id))
      .then((book) => {
        if (cancelled) {
          return;
        }
        setBookId(book.book_id);
        const found = findLoan(book.lender_groups, loanInternalId);
        setLoan(found);
        setStatusMessage(null);
        if (!found) {
          setErrorMessage("This loan is not in the book.");
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        setLoan(null);
        setErrorMessage(messageFrom(error, "Could not open this loan."));
        setStatusMessage(null);
      });
    return () => {
      cancelled = true;
    };
  }, [loanInternalId]);

  async function onSave(loanId: string, patch: Parameters<typeof patchBookLoan>[2]) {
    const id = bookId ?? readBookId();
    if (!id || busy) {
      return;
    }
    setBusy(true);
    setErrorMessage(null);
    setStatusMessage("Saving correction…");
    try {
      const updated = await patchBookLoan(id, loanId, patch);
      setLoan(updated);
      setExtraText(formatReturnedExtra(updated));
    } catch (error: unknown) {
      setErrorMessage(messageFrom(error, "Could not save the correction."));
    } finally {
      setBusy(false);
      setStatusMessage(null);
    }
  }

  async function onShowScreenshot(imageId: string) {
    const id = bookId ?? readBookId();
    if (!id || !loan || screenshots[imageId] || busy) {
      return;
    }
    setBusy(true);
    setErrorMessage(null);
    try {
      const blob = await fetchLoanImage(id, loan.loan_internal_id, imageId);
      const url = URL.createObjectURL(blob);
      setScreenshots((current) => ({ ...current, [imageId]: url }));
    } catch (error: unknown) {
      setErrorMessage(messageFrom(error, "Could not load the screenshot."));
    } finally {
      setBusy(false);
    }
  }

  const imageIds = loan?.image_ids ?? [];

  return (
    <section className="screen">
      <Link className="back-link" to="/">
        Book
      </Link>
      <h1 className="screen-title">Loan detail</h1>
      {loan ? <ReviewBadge needsReview={loan.needs_review} /> : null}
      {errorMessage ? <p role="alert">{errorMessage}</p> : null}
      {statusMessage ? <p role="status">{statusMessage}</p> : null}
      {loan ? (
        <>
          <section className="detail-section" aria-labelledby="extracted-heading">
            <h2 id="extracted-heading">Extracted</h2>
            <ul className="field-list">
              {EXTRACTED_FIELDS.map((item) => (
                <FieldLine key={item.key} label={item.label} field={loan.fields?.[item.key]} />
              ))}
              <li>Penalties: {penaltyText(loan.fields?.penalties)}</li>
            </ul>
          </section>
          <section className="detail-section" aria-labelledby="calculated-heading">
            <h2 id="calculated-heading">Calculated</h2>
            <ul className="field-list">
              {CALCULATED_FIELDS.map((item) => (
                <FieldLine key={item.key} label={item.label} field={loan.calculated?.[item.key]} />
              ))}
            </ul>
          </section>
          <section className="detail-section" aria-labelledby="checks-heading">
            <h2 id="checks-heading">Checks</h2>
            {(loan.validations ?? []).length === 0 ? (
              <p>No checks.</p>
            ) : (
              <ul className="check-list">
                {loan.validations.map((check) => (
                  <li key={check.id}>
                    {check.id}: {check.status}
                    {check.message ? `. ${check.message}` : ""}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section className="detail-section" aria-labelledby="review-heading">
            <h2 id="review-heading">Review</h2>
            {(loan.review_flags ?? []).length === 0 ? (
              <p>No review flags.</p>
            ) : (
              <ul className="check-list">
                {loan.review_flags.map((flag) => (
                  <li key={flag}>{flag}</li>
                ))}
              </ul>
            )}
          </section>
          <FieldCorrectionForm
            loans={[{ loan_internal_id: loan.loan_internal_id, label: loan.summary_line || loan.loan_internal_id }]}
            disabled={busy}
            extraText={extraText}
            onSave={(loanId, patch) => {
              void onSave(loanId, patch);
            }}
          />
          {imageIds.map((imageId) => (
            <button
              key={imageId}
              className="secondary-button"
              type="button"
              disabled={busy || Boolean(screenshots[imageId])}
              onClick={() => {
                void onShowScreenshot(imageId);
              }}
            >
              {imageIds.length === 1 ? "Show screenshot" : `Show screenshot ${imageId}`}
            </button>
          ))}
          {imageIds.map((imageId) =>
            screenshots[imageId] ? (
              <img
                key={imageId}
                className="audit-image"
                alt={imageIds.length === 1 ? "Loan screenshot" : `Loan screenshot ${imageId}`}
                src={screenshots[imageId]}
              />
            ) : null,
          )}
        </>
      ) : null}
    </section>
  );
}

function FieldLine({ label, field }: { label: string; field: unknown }) {
  const shown = readPresented(field);
  return (
    <li>
      {label}: <span className={shown.missing ? "unknown-value" : undefined}>{shown.text}</span> ({shown.source})
    </li>
  );
}

function readPresented(field: unknown): { text: string; source: string; missing: boolean } {
  if (field === null || field === undefined || typeof field !== "object") {
    return { text: "unknown", source: "unknown", missing: true };
  }
  const record = field as { value?: unknown; source?: unknown; unit?: unknown };
  const source = typeof record.source === "string" && record.source !== "" ? record.source : "unknown";
  if (record.value === null || record.value === undefined || record.value === "") {
    return { text: "unknown", source, missing: true };
  }
  const unit = typeof record.unit === "string" && record.unit !== "" ? ` ${record.unit}` : "";
  if (typeof record.value === "string" || typeof record.value === "number") {
    return { text: `${record.value}${unit}`, source, missing: false };
  }
  return { text: "unknown", source, missing: true };
}

function penaltyText(penalties: LoanFields["penalties"]): string {
  if (!penalties || penalties.length === 0) {
    return "none extracted";
  }
  return penalties
    .map((penalty) => {
      const amount = presentedAmount(penalty.amount);
      return amount === "unknown" ? penalty.name : `${penalty.name} ${amount}`;
    })
    .join(", ");
}

function presentedAmount(field: CalculatedValue | LoanFields["sanctioned_amount"] | null | undefined): string {
  return readPresented(field).text;
}

function findLoan(groups: Array<{ loans?: Loan[] }> | undefined, loanId: string): Loan | null {
  for (const group of groups ?? []) {
    for (const loan of group.loans ?? []) {
      if (loan?.loan_internal_id === loanId) {
        return loan;
      }
    }
  }
  return null;
}

function messageFrom(error: unknown, fallback: string): string {
  if (error instanceof ApiClientError && error.message.trim() !== "") {
    return error.message;
  }
  return fallback;
}
