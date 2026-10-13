const PHASE_LINE: Record<string, string> = {
  queued: "1/6 queued",
  running: "2/6 preparing photos",
  preprocessing: "2/6 preparing photos",
  classifying: "3/6 classifying",
  extracting: "4/6 extracting",
  grouping: "5/6 grouping",
  validating: "6/6 checking",
};

/** SPEC §4.6. The step is the processing phase, not a screenshot count. */
export function phaseProgress(status: string): string | null {
  return PHASE_LINE[status] ?? null;
}
