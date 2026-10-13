import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { analyzeDraft, ApiClientError, deleteLoan, ensureBook, getBookInbox, getBookSummary, readBookId } from "../api/client";
import type { BookInbox, BookSummary } from "../api/types";
import { emptyInbox, InboxSummary, isBookInbox } from "../components/InboxSummary";
import { ReviewBadge } from "../components/ReviewBadge";
import { SummaryGroup } from "../components/SummaryGroup";

export function BookScreen() {
  const [summary, setSummary] = useState<BookSummary | null>(null);
  const [inbox, setInbox] = useState<BookInbox | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>("Opening book…");
  const [removing, setRemoving] = useState(false);
  const [retryingId, setRetryingId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    ensureBook()
      .then((book) => {
        if (cancelled) {
          return null;
        }
        return Promise.all([getBookSummary(book.book_id), getBookInbox(book.book_id)]).then(([nextSummary, nextInbox]) => ({
          bookId: book.book_id,
          nextSummary,
          nextInbox,
        }));
      })
      .then((loaded) => {
        if (cancelled || loaded == null) {
          return;
        }
        setSummary(loaded.nextSummary);
        setInbox(isBookInbox(loaded.nextInbox) ? loaded.nextInbox : emptyInbox(loaded.bookId));
        setStatusMessage(null);
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        setErrorMessage(messageFrom(error, "Could not open the book."));
        setStatusMessage(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onRemoveLoan(loanId: string) {
    const bookId = summary?.book_id ?? readBookId();
    if (!bookId || removing) {
      return;
    }
    setRemoving(true);
    setErrorMessage(null);
    setStatusMessage("Removing loan…");
    try {
      await deleteLoan(bookId, loanId);
      const next = await getBookSummary(bookId);
      setSummary(next);
    } catch (error: unknown) {
      setErrorMessage(messageFrom(error, "Could not remove the loan."));
    } finally {
      setRemoving(false);
      setStatusMessage(null);
    }
  }

  async function onRetry(analysisId: string) {
    const bookId = summary?.book_id ?? readBookId();
    if (!bookId || retryingId) {
      return;
    }
    setRetryingId(analysisId);
    setErrorMessage(null);
    try {
      await analyzeDraft(analysisId);
      const next = await getBookInbox(bookId);
      setInbox(isBookInbox(next) ? next : emptyInbox(bookId));
    } catch (error: unknown) {
      setErrorMessage(messageFrom(error, "Could not retry this draft."));
    } finally {
      setRetryingId(null);
    }
  }

  const headerNeedsReview = summary ? bookNeedsReview(summary) : false;

  return (
    <section className="screen">
      <header className="screen-header">
        <h1 className="screen-title">Book</h1>
        <ReviewBadge needsReview={headerNeedsReview} />
      </header>
      {errorMessage ? <p role="alert">{errorMessage}</p> : null}
      {statusMessage ? <p role="status">{statusMessage}</p> : null}
      {summary ? (
        <>
          {inbox ? (
            <InboxSummary
              inbox={inbox}
              retryingId={retryingId}
              onRetry={(analysisId) => {
                void onRetry(analysisId);
              }}
            />
          ) : null}
          <ul className="summary-totals">
            <li>{totalText("Received", summary.totals.received)}</li>
            <li>{totalText("Still to pay", summary.totals.still_to_pay)}</li>
            <li>{totalText("Extra", summary.totals.extra)}</li>
          </ul>
          {summary.totals.incomplete ? <p>Some loans are missing amounts.</p> : null}
          {summary.groups.map((group, index) => (
            <SummaryGroup
              key={`${group.header}:${index}`}
              group={group}
              loanHref={(loanId) => `/loans/${loanId}`}
              onRemoveLoan={(loanId) => {
                void onRemoveLoan(loanId);
              }}
              removeDisabled={removing}
            />
          ))}
        </>
      ) : null}
      <footer className="sticky-footer">
        <Link className="primary-button" to="/add">
          Add photos
        </Link>
      </footer>
    </section>
  );
}

function bookNeedsReview(summary: BookSummary): boolean {
  return summary.groups.some((group) => group.lines.some((line) => line.needs_review));
}

function totalText(label: string, value: string | null): string {
  if (value === null || value.trim() === "") {
    return label;
  }
  return `${label} ${value}`;
}

function messageFrom(error: unknown, fallback: string): string {
  if (error instanceof ApiClientError && error.message.trim() !== "") {
    return error.message;
  }
  return fallback;
}
