interface ReviewBadgeProps {
  needsReview: boolean;
}

/** Visible when a loan or a finished analysis still needs a person to check it. */
export function ReviewBadge({ needsReview }: ReviewBadgeProps) {
  if (!needsReview) {
    return null;
  }
  return <p className="review-badge">Needs review</p>;
}
