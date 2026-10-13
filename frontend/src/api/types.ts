/** API DTOs from SPEC §14 and the field shapes those resources reference. */

export type AnalysisStatus =
  | "created"
  | "images_pending"
  | "preprocessing"
  | "classifying"
  | "extracting"
  | "grouping"
  | "validating"
  | "completed"
  | "completed_needs_review"
  | "accepted"
  | "failed"
  | "expired";

export type ImageStatus = "uploaded" | "rejected" | "preprocessed" | "classified" | "failed";

export type ErrorCode =
  | "ANALYSIS_NOT_FOUND"
  | "BOOK_NOT_FOUND"
  | "LOAN_NOT_FOUND"
  | "INVALID_STATUS"
  | "NO_IMAGES"
  | "TOO_MANY_IMAGES"
  | "IMAGE_TOO_LARGE"
  | "IMAGE_UNSUPPORTED_TYPE"
  | "IMAGE_UNREADABLE"
  | "IMAGE_TOO_SMALL"
  | "NO_LOAN_DOCUMENTS"
  | "VISION_UNAVAILABLE"
  | "VISION_INVALID_RESPONSE"
  | "VISION_TIMEOUT"
  | "INTERNAL_ERROR";

export type FieldSource = "extracted" | "calculated" | "user_corrected" | "unknown";

export type Confidence = "high" | "medium" | "low" | "unknown";

export type RepaymentFrequency = "one_time" | "weekly" | "biweekly" | "monthly" | "unknown";

export type CheckStatus = "passed" | "failed" | "skipped";

export type ValidationSummary = "ok" | "failed" | "incomplete";

export interface FieldEvidence {
  image_id: string;
  label_text: string;
}

export interface StringField {
  value: string | null;
  source: FieldSource;
  confidence: Confidence;
  evidence?: FieldEvidence;
}

export interface MoneyField {
  value: string | null;
  currency: string | null;
  source: FieldSource;
  confidence: Confidence;
  evidence?: FieldEvidence;
}

export interface DateField {
  value: string | null;
  source: FieldSource;
  confidence: Confidence;
  evidence?: FieldEvidence;
}

export interface RateField {
  value: string | null;
  unit: "percent" | null;
  source: FieldSource;
  confidence: Confidence;
  evidence?: FieldEvidence;
}

export interface TenureField {
  value: number | null;
  unit: "days" | "months" | null;
  source: FieldSource;
  confidence: Confidence;
  evidence?: FieldEvidence;
}

export interface EnumField {
  value: RepaymentFrequency | null;
  source: FieldSource;
  confidence: Confidence;
  evidence?: FieldEvidence;
}

export interface PenaltyField {
  name: string;
  amount: MoneyField | null;
  rate: RateField | null;
  description: string | null;
}

export interface LoanFields {
  lender_brand?: StringField;
  legal_lending_entity?: StringField;
  borrower_name?: StringField;
  loan_id?: StringField;
  sanctioned_amount?: MoneyField;
  disbursed_amount?: MoneyField;
  processing_fee?: MoneyField;
  gst_amount?: MoneyField;
  other_fees_total?: MoneyField;
  interest_rate?: RateField;
  apr?: RateField;
  tenure?: TenureField;
  sanction_date?: DateField;
  disbursement_date?: DateField;
  repayment_amount?: MoneyField;
  repayment_date?: DateField;
  repayment_frequency?: EnumField;
  penalties?: PenaltyField[];
}

export interface CalculatedValue {
  value: string | null;
  source: "calculated" | "unknown";
}

export interface CalculatedFields {
  implied_fees_total?: CalculatedValue;
  fee_plus_gst?: CalculatedValue;
  implied_cost?: CalculatedValue;
  extra_percent?: CalculatedValue;
  display_sanctioned_short?: CalculatedValue;
}

export interface ValidationCheck {
  id: string;
  status: CheckStatus;
  message: string;
}

export interface Loan {
  loan_internal_id: string;
  external_loan_id: string | null;
  needs_review: boolean;
  review_flags: string[];
  image_ids: string[];
  fields: LoanFields;
  calculated: CalculatedFields;
  validations: ValidationCheck[];
  validation_summary: ValidationSummary;
  summary_line: string;
}

export interface LenderGroup {
  lender_group_id: string;
  lender_brand: string | null;
  legal_lending_entity: string | null;
  loans: Loan[];
}

export interface AnalysisImage {
  image_id: string;
  filename: string;
  content_type: string;
  byte_size: number;
  status: ImageStatus;
}

export interface ImageUploadResult {
  analysis_id: string;
  status: AnalysisStatus;
  images: AnalysisImage[];
}

export interface AnalysisResourceError {
  code: string;
  message: string;
}

export interface Analysis {
  analysis_id: string;
  status: AnalysisStatus;
  created_at: string;
  completed_at: string | null;
  expires_at: string | null;
  error: AnalysisResourceError | null;
  pages_done?: number;
  pages_total?: number;
  queued_at?: string | null;
  images: AnalysisImage[];
  lender_groups: LenderGroup[];
}

export interface ApiErrorBody {
  code: ErrorCode | string;
  message: string;
  retryable: boolean;
  details: Record<string, string | number | boolean | null>;
}

export interface ApiErrorEnvelope {
  error: ApiErrorBody;
}

export interface HealthResponse {
  status: "ok";
}

export interface BookCreated {
  book_id: string;
  created_at: string;
}

export interface DraftCreated {
  book_id: string;
  analysis_id: string;
  status: AnalysisStatus;
  created_at: string;
  expires_at: string;
}

export interface BookTotals {
  received: string | null;
  still_to_pay: string | null;
  extra: string | null;
  incomplete: boolean;
}

export interface Book {
  book_id: string;
  created_at: string;
  totals: BookTotals;
  lender_groups: LenderGroup[];
}

export interface SummaryLine {
  loan_internal_id: string;
  text: string;
  needs_review: boolean;
}

export interface SummaryGroup {
  header: string;
  lines: SummaryLine[];
}

export interface AnalysisSummary {
  analysis_id: string;
  status: AnalysisStatus;
  groups: SummaryGroup[];
}

export interface BookSummary {
  book_id: string;
  totals: BookTotals;
  groups: SummaryGroup[];
}

export type InboxBucket = "queued" | "running" | "ready" | "needs_review" | "failed";

export interface InboxCounts {
  queued: number;
  running: number;
  ready: number;
  needs_review: number;
  failed: number;
}

/** Inbox list row. Amounts, borrower names, and loan ids are not part of this DTO. */
export interface InboxDraft {
  analysis_id: string;
  status: AnalysisStatus;
  inbox_bucket: InboxBucket;
  pages_done: number;
  pages_total: number;
  image_count: number;
  created_at: string;
  queued_at: string | null;
  retryable: boolean;
  error: AnalysisResourceError | null;
}

export interface BookInbox {
  book_id: string;
  counts: InboxCounts;
  drafts: InboxDraft[];
}

export interface AcceptDraftResponse {
  book_id: string;
  loan_internal_ids: string[];
}

export interface GroupingRequest {
  loans: Array<{
    loan_internal_id: string;
    image_ids: string[];
  }>;
}

export type CorrectableFieldName =
  | "sanctioned_amount"
  | "disbursed_amount"
  | "repayment_amount"
  | "processing_fee"
  | "gst_amount"
  | "other_fees_total"
  | "sanction_date"
  | "disbursement_date"
  | "repayment_date"
  | "lender_brand"
  | "legal_lending_entity";

export type LoanCorrection = Partial<Record<CorrectableFieldName, string | null>>;
