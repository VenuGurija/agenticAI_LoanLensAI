import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  acceptDraft,
  analyzeDraft,
  ApiClientError,
  clearAnalysisId,
  ensureBook,
  getAnalysis,
  getAnalysisSummary,
  patchPreviewLoan,
  postGrouping,
  readBookId,
} from "../api/client";
import type { Analysis, LoanCorrection, SummaryGroup as SummaryGroupDto } from "../api/types";
import { FieldCorrectionForm, formatReturnedExtra } from "../components/FieldCorrectionForm";
import { ReviewBadge } from "../components/ReviewBadge";
import { SummaryGroup } from "../components/SummaryGroup";
import { phaseProgress } from "../lib/phaseProgress";

const READY_STATUSES = new Set(["completed", "completed_needs_review", "accepted"]);
const IN_FLIGHT_STATUSES = new Set([
  "created",
  "images_pending",
  "queued",
  "running",
  "preprocessing",
  "classifying",
  "extracting",
  "grouping",
  "validating",
]);

interface PreviewLoan {
  loan_internal_id: string;
  label: string;
  image_ids: string[];
}

export function PreviewScreen() {
  const { analysisId } = useParams();
  const navigate = useNavigate();
  const [groups, setGroups] = useState<SummaryGroupDto[] | null>(null);
  const [headerNeedsReview, setHeaderNeedsReview] = useState(false);
  const [loans, setLoans] = useState<PreviewLoan[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [canAccept, setCanAccept] = useState(false);
  const [canRetry, setCanRetry] = useState(false);
  const [busy, setBusy] = useState(false);
  const [extraText, setExtraText] = useState<string | null>(null);
  const [moves, setMoves] = useState<Record<string, string>>({});
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!analysisId) {
      setStatusMessage(null);
      setCanAccept(false);
      return;
    }
    let cancelled = false;
    setStatusMessage(null);
    setGroups(null);
    setHeaderNeedsReview(false);
    setLoans([]);
    setMoves({});
    setCanAccept(false);
    setCanRetry(false);
    setErrorMessage(null);
    setExtraText(null);
    getAnalysis(analysisId)
      .then(async (analysis) => {
        if (cancelled) {
          return;
        }
        if (analysis.status === "failed") {
          const failedMessage = analysis.error?.message ?? "";
          setGroups(null);
          setHeaderNeedsReview(false);
          setLoans([]);
          setCanAccept(false);
          setCanRetry(true);
          setErrorMessage(failedMessage.trim() !== "" ? failedMessage : null);
          setStatusMessage(null);
          return;
        }
        if (!READY_STATUSES.has(analysis.status)) {
          setGroups(null);
          setHeaderNeedsReview(false);
          setLoans([]);
          setCanAccept(false);
          setCanRetry(false);
          setStatusMessage(IN_FLIGHT_STATUSES.has(analysis.status) ? phaseProgress(analysis.status) : null);
          return;
        }
        const summary = await getAnalysisSummary(analysisId);
        if (cancelled) {
          return;
        }
        const nextLoans = loansFrom(analysis, summary.groups);
        setGroups(summary.groups);
        setHeaderNeedsReview(summary.status === "completed_needs_review");
        setLoans(nextLoans);
        setMoves(initialMoves(nextLoans));
        setCanAccept(true);
        setStatusMessage(null);
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        setGroups(null);
        setHeaderNeedsReview(false);
        setLoans([]);
        setCanAccept(false);
        setCanRetry(false);
        setErrorMessage(messageFrom(error, "Could not open the preview."));
        setStatusMessage(null);
      });
    return () => {
      cancelled = true;
    };
  }, [analysisId]);

  async function onRetry() {
    if (!analysisId || busy) {
      return;
    }
    setBusy(true);
    setErrorMessage(null);
    try {
      const updated = await analyzeDraft(analysisId);
      if (!mounted.current) {
        return;
      }
      if (updated.status === "failed") {
        const failedMessage = updated.error?.message ?? "";
        setCanRetry(true);
        setErrorMessage(failedMessage.trim() !== "" ? failedMessage : "Could not retry this draft.");
        return;
      }
      clearAnalysisId();
      navigate("/");
    } catch (error: unknown) {
      if (!mounted.current) {
        return;
      }
      setErrorMessage(messageFrom(error, "Could not retry this draft."));
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  }

  async function onAddToBook() {
    if (!analysisId || !canAccept || busy) {
      return;
    }
    setBusy(true);
    setErrorMessage(null);
    setStatusMessage("Adding to book…");
    try {
      const bookId = readBookId() ?? (await ensureBook()).book_id;
      await acceptDraft(bookId, analysisId);
      clearAnalysisId();
      navigate("/");
    } catch (error: unknown) {
      if (!mounted.current) {
        return;
      }
      setErrorMessage(messageFrom(error, "Could not add these loans to the book."));
      setStatusMessage(null);
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  }

  async function onSaveCorrection(loanId: string, patch: LoanCorrection) {
    if (!analysisId || busy) {
      return;
    }
    setBusy(true);
    setErrorMessage(null);
    setStatusMessage("Saving correction…");
    try {
      const updated = await patchPreviewLoan(analysisId, loanId, patch);
      if (!mounted.current) {
        return;
      }
      setExtraText(formatReturnedExtra(updated));
      const summary = await getAnalysisSummary(analysisId);
      if (!mounted.current) {
        return;
      }
      setGroups(summary.groups);
      setHeaderNeedsReview(summary.status === "completed_needs_review");
    } catch (error: unknown) {
      if (!mounted.current) {
        return;
      }
      setErrorMessage(messageFrom(error, "Could not save the correction."));
    } finally {
      if (mounted.current) {
        setBusy(false);
        setStatusMessage(null);
      }
    }
  }

  async function onMoveScreenshots() {
    if (!analysisId || busy || loans.length < 2) {
      return;
    }
    setBusy(true);
    setErrorMessage(null);
    setStatusMessage("Moving screenshots…");
    const assignment = loans.map((loan) => ({
      loan_internal_id: loan.loan_internal_id,
      image_ids: Object.entries(moves)
        .filter(([, target]) => target === loan.loan_internal_id)
        .map(([imageId]) => imageId),
    }));
    try {
      const analysis = await postGrouping(analysisId, { loans: assignment });
      if (!mounted.current) {
        return;
      }
      const summary = await getAnalysisSummary(analysisId);
      if (!mounted.current) {
        return;
      }
      const nextLoans = loansFrom(analysis, summary.groups);
      setGroups(summary.groups);
      setHeaderNeedsReview(summary.status === "completed_needs_review");
      setLoans(nextLoans);
      setMoves(initialMoves(nextLoans));
    } catch (error: unknown) {
      if (!mounted.current) {
        return;
      }
      setErrorMessage(messageFrom(error, "Could not move the screenshots."));
    } finally {
      if (mounted.current) {
        setBusy(false);
        setStatusMessage(null);
      }
    }
  }

  const images = loans.flatMap((loan) => loan.image_ids.map((imageId) => ({ imageId, loanId: loan.loan_internal_id })));
  const showMoves = canAccept && loans.length >= 2 && images.length > 0;

  return (
    <section className="screen">
      <header className="screen-header">
        <h1 className="screen-title">Preview</h1>
        <ReviewBadge needsReview={headerNeedsReview} />
      </header>
      {errorMessage ? (
        <p role="alert" style={{ whiteSpace: "pre-line" }}>
          {errorMessage}
        </p>
      ) : null}
      {statusMessage ? <p role="status">{statusMessage}</p> : null}
      {groups?.map((group, index) => (
        <SummaryGroup key={`${group.header}:${index}`} group={group} />
      ))}
      {canAccept && loans.length > 0 ? (
        <FieldCorrectionForm
          loans={loans}
          disabled={busy}
          extraText={extraText}
          onSave={(loanId, patch) => {
            void onSaveCorrection(loanId, patch);
          }}
        />
      ) : null}
      {showMoves ? (
        <form
          className="correction-form"
          onSubmit={(event) => {
            event.preventDefault();
            void onMoveScreenshots();
          }}
        >
          <h2>Move screenshots</h2>
          {images.map((image) => (
            <label key={image.imageId}>
              {`Screenshot ${image.imageId}`}
              <select
                aria-label={`Move ${image.imageId}`}
                value={moves[image.imageId] ?? image.loanId}
                disabled={busy}
                onChange={(event) => {
                  const target = event.target.value;
                  setMoves((current) => ({ ...current, [image.imageId]: target }));
                }}
              >
                {loans.map((loan) => (
                  <option key={loan.loan_internal_id} value={loan.loan_internal_id}>
                    {loan.label}
                  </option>
                ))}
              </select>
            </label>
          ))}
          <button className="secondary-button" type="submit" disabled={busy}>
            Move screenshots
          </button>
        </form>
      ) : null}
      {canRetry ? (
        <button
          className="secondary-button"
          type="button"
          disabled={busy}
          onClick={() => {
            void onRetry();
          }}
        >
          Retry
        </button>
      ) : null}
      <footer className="sticky-footer">
        <button
          className="primary-button"
          type="button"
          disabled={!canAccept || busy}
          onClick={() => {
            void onAddToBook();
          }}
        >
          Add to book
        </button>
      </footer>
    </section>
  );
}

function loansFrom(analysis: Analysis, groups: SummaryGroupDto[]): PreviewLoan[] {
  const labels = new Map<string, string>();
  for (const group of groups) {
    for (const line of group.lines) {
      labels.set(line.loan_internal_id, line.text);
    }
  }
  const loans: PreviewLoan[] = [];
  for (const group of analysis.lender_groups ?? []) {
    for (const loan of group.loans ?? []) {
      if (!loan?.loan_internal_id) {
        continue;
      }
      const imageIds = Array.isArray(loan.image_ids) ? loan.image_ids.filter((id) => id !== "") : [];
      loans.push({
        loan_internal_id: loan.loan_internal_id,
        label: labels.get(loan.loan_internal_id) || loan.summary_line || loan.loan_internal_id,
        image_ids: imageIds,
      });
    }
  }
  return loans;
}

function initialMoves(loans: PreviewLoan[]): Record<string, string> {
  const moves: Record<string, string> = {};
  for (const loan of loans) {
    for (const imageId of loan.image_ids) {
      moves[imageId] = loan.loan_internal_id;
    }
  }
  return moves;
}

function messageFrom(error: unknown, fallback: string): string {
  if (error instanceof ApiClientError && error.message.trim() !== "") {
    return error.message;
  }
  return fallback;
}
