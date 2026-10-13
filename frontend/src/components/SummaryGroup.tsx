import { Link } from "react-router-dom";
import type { SummaryGroup as SummaryGroupDto } from "../api/types";
import { ReviewBadge } from "./ReviewBadge";

interface SummaryGroupProps {
  group: SummaryGroupDto;
  onRemoveLoan?: (loanInternalId: string) => void;
  removeDisabled?: boolean;
  loanHref?: (loanInternalId: string) => string;
}

export function SummaryGroup({ group, onRemoveLoan, removeDisabled = false, loanHref }: SummaryGroupProps) {
  const header = group.header.trim();

  return (
    <section className="summary-group">
      {header !== "" ? <h2 className="summary-header">{group.header}</h2> : null}
      <ul className="summary-lines">
        {group.lines.map((line) => (
          <li key={line.loan_internal_id} className="summary-line">
            {loanHref ? (
              <Link to={loanHref(line.loan_internal_id)} aria-label={lineLabel(line.text)}>
                <SummaryLineText text={line.text} />
              </Link>
            ) : (
              <p>
                <SummaryLineText text={line.text} />
              </p>
            )}
            <ReviewBadge needsReview={line.needs_review} />
            {onRemoveLoan ? (
              <button
                className="thumb-remove"
                type="button"
                disabled={removeDisabled}
                aria-label={`Remove loan ${line.text}`}
                onClick={() => onRemoveLoan(line.loan_internal_id)}
              >
                Remove loan
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

function lineLabel(text: string): string {
  return text.trim() === "" ? "unknown" : text;
}

/** Show the summary text as returned. A missing piece stays the word unknown, never a filled-in number. */
export function SummaryLineText({ text }: { text: string }) {
  const shown = text.trim();
  if (shown === "") {
    return <span className="unknown-value">unknown</span>;
  }
  const parts = shown.split(/(unknown)/i).filter((part) => part !== "");
  return parts.map((part, index) =>
    part.toLowerCase() === "unknown" ? (
      <span key={index} className="unknown-value">
        {part}
      </span>
    ) : (
      <span key={index}>{part}</span>
    ),
  );
}
