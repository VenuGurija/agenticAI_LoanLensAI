import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { BookInbox, InboxBucket, InboxCounts, InboxDraft } from "../api/types";
import { phaseProgress } from "../lib/phaseProgress";

const COUNT_ORDER: Array<{ bucket: InboxBucket; label: string; section: string }> = [
  { bucket: "ready", label: "ready", section: "Ready" },
  { bucket: "needs_review", label: "needs review", section: "Needs review" },
  { bucket: "queued", label: "queued", section: "Queued" },
  { bucket: "running", label: "running", section: "Running" },
  { bucket: "failed", label: "failed", section: "Failed" },
];

const OPEN_PREVIEW = new Set<InboxBucket>(["ready", "needs_review"]);

interface InboxSummaryProps {
  inbox: BookInbox;
  retryingId: string | null;
  onRetry: (analysisId: string) => void;
}

export function emptyInbox(bookId: string): BookInbox {
  return {
    book_id: bookId,
    counts: { queued: 0, running: 0, ready: 0, needs_review: 0, failed: 0 },
    drafts: [],
  };
}

export function isBookInbox(value: BookInbox | null | undefined): value is BookInbox {
  if (value == null) {
    return false;
  }
  const counts = value.counts;
  return (
    counts != null &&
    typeof counts.queued === "number" &&
    typeof counts.running === "number" &&
    typeof counts.ready === "number" &&
    typeof counts.needs_review === "number" &&
    typeof counts.failed === "number" &&
    Array.isArray(value.drafts)
  );
}

export function inboxCountLine(counts: InboxCounts): string {
  return COUNT_ORDER.filter((item) => counts[item.bucket] > 0)
    .map((item) => `${counts[item.bucket]} ${item.label}`)
    .join(" · ");
}

/** Count line and bucket list for this book. Rows never include amounts or borrower names. */
export function InboxSummary({ inbox, retryingId, onRetry }: InboxSummaryProps) {
  const [selected, setSelected] = useState<InboxBucket | null>(null);
  const line = inboxCountLine(inbox.counts);

  useEffect(() => {
    if (selected && inbox.counts[selected] === 0) {
      setSelected(null);
    }
  }, [inbox, selected]);

  if (line === "") {
    return null;
  }

  const section = COUNT_ORDER.find((item) => item.bucket === selected);
  const rows = selected ? inbox.drafts.filter((draft) => draft.inbox_bucket === selected) : [];

  return (
    <div className="inbox-summary">
      <p className="inbox-counts">
        {COUNT_ORDER.filter((item) => inbox.counts[item.bucket] > 0).map((item, index) => (
          <span key={item.bucket}>
            {index > 0 ? <span className="inbox-separator"> · </span> : null}
            <button
              type="button"
              className="inbox-count"
              aria-pressed={selected === item.bucket}
              onClick={() => {
                setSelected((current) => (current === item.bucket ? null : item.bucket));
              }}
            >
              {`${inbox.counts[item.bucket]} ${item.label}`}
            </button>
          </span>
        ))}
      </p>
      {section ? (
        <ul className="inbox-rows" aria-label={section.section}>
          {rows.map((draft) => (
            <InboxRow
              key={draft.analysis_id}
              draft={draft}
              retrying={retryingId === draft.analysis_id}
              onRetry={onRetry}
            />
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function InboxRow({
  draft,
  retrying,
  onRetry,
}: {
  draft: InboxDraft;
  retrying: boolean;
  onRetry: (analysisId: string) => void;
}) {
  const progress = phaseProgress(draft.status);
  const openPreview = OPEN_PREVIEW.has(draft.inbox_bucket);

  return (
    <li className="inbox-row">
      {openPreview ? (
        <Link to={`/a/${draft.analysis_id}`}>{draft.analysis_id}</Link>
      ) : (
        <span>{draft.analysis_id}</span>
      )}
      {progress ? <span>{progress}</span> : null}
      <time dateTime={draft.created_at}>{draft.created_at}</time>
      {draft.inbox_bucket === "failed" && draft.error?.message ? (
        <p style={{ whiteSpace: "pre-line" }}>{draft.error.message}</p>
      ) : null}
      {draft.inbox_bucket === "failed" && draft.retryable ? (
        <button
          className="secondary-button"
          type="button"
          disabled={retrying}
          onClick={() => {
            onRetry(draft.analysis_id);
          }}
        >
          Retry
        </button>
      ) : null}
    </li>
  );
}
