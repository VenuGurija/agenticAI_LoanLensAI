import type {
  AcceptDraftResponse,
  Analysis,
  AnalysisSummary,
  Book,
  BookCreated,
  BookInbox,
  BookSummary,
  DraftCreated,
  GroupingRequest,
  HealthResponse,
  ImageUploadResult,
  Loan,
  LoanCorrection,
} from "./types";

export const BOOK_ID_STORAGE_KEY = "loanlens.book_id";
export const ANALYSIS_ID_STORAGE_KEY = "loanlens.analysis_id";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8002";

export class ApiClientError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly details: Record<string, string | number | boolean | null>;

  constructor(
    status: number,
    code: string,
    message: string,
    retryable: boolean,
    details: Record<string, string | number | boolean | null>,
  ) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
    this.details = details;
  }
}

export function apiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL;
  if (typeof configured === "string" && configured.trim() !== "") {
    return configured.replace(/\/+$/, "");
  }
  return DEFAULT_API_BASE_URL;
}

export function readBookId(): string | null {
  const value = localStorage.getItem(BOOK_ID_STORAGE_KEY);
  if (value === null || value.trim() === "") {
    return null;
  }
  return value;
}

export function writeBookId(bookId: string): void {
  localStorage.setItem(BOOK_ID_STORAGE_KEY, bookId);
  sessionStorage.removeItem(BOOK_ID_STORAGE_KEY);
}

export function readAnalysisId(): string | null {
  const value = sessionStorage.getItem(ANALYSIS_ID_STORAGE_KEY);
  if (value === null || value.trim() === "") {
    return null;
  }
  return value;
}

export function writeAnalysisId(analysisId: string): void {
  sessionStorage.setItem(ANALYSIS_ID_STORAGE_KEY, analysisId);
}

export function clearAnalysisId(): void {
  sessionStorage.removeItem(ANALYSIS_ID_STORAGE_KEY);
}

export function health(): Promise<HealthResponse> {
  return request<HealthResponse>("/api/v1/health");
}

export function createBook(): Promise<BookCreated> {
  return request<BookCreated>("/api/v1/books", {
    method: "POST",
    body: "{}",
  });
}

export function getBook(bookId: string): Promise<Book> {
  return request<Book>(`/api/v1/books/${encodeURIComponent(bookId)}`);
}

export function createDraft(bookId: string): Promise<DraftCreated> {
  return request<DraftCreated>(`/api/v1/books/${encodeURIComponent(bookId)}/drafts`, {
    method: "POST",
    body: "{}",
  });
}

export function uploadImages(analysisId: string, files: File[]): Promise<ImageUploadResult> {
  const body = new FormData();
  for (const file of files) {
    body.append("files", file, file.name);
  }
  return request<ImageUploadResult>(`/api/v1/analyses/${encodeURIComponent(analysisId)}/images`, {
    method: "POST",
    body,
  });
}

export function deleteImage(analysisId: string, imageId: string): Promise<void> {
  return request<void>(
    `/api/v1/analyses/${encodeURIComponent(analysisId)}/images/${encodeURIComponent(imageId)}`,
    { method: "DELETE" },
  );
}

export function analyzeDraft(analysisId: string): Promise<Analysis> {
  return request<Analysis>(`/api/v1/analyses/${encodeURIComponent(analysisId)}/analyze`, {
    method: "POST",
    body: "{}",
  });
}

export function getAnalysis(analysisId: string): Promise<Analysis> {
  return request<Analysis>(`/api/v1/analyses/${encodeURIComponent(analysisId)}`);
}

export function getAnalysisSummary(analysisId: string): Promise<AnalysisSummary> {
  return request<AnalysisSummary>(`/api/v1/analyses/${encodeURIComponent(analysisId)}/summary`);
}

export function getBookSummary(bookId: string): Promise<BookSummary> {
  return request<BookSummary>(`/api/v1/books/${encodeURIComponent(bookId)}/summary`);
}

export function getBookInbox(bookId: string): Promise<BookInbox> {
  return request<BookInbox>(`/api/v1/books/${encodeURIComponent(bookId)}/inbox`);
}

export function acceptDraft(bookId: string, analysisId: string): Promise<AcceptDraftResponse> {
  return request<AcceptDraftResponse>(
    `/api/v1/books/${encodeURIComponent(bookId)}/drafts/${encodeURIComponent(analysisId)}/accept`,
    { method: "POST", body: "{}" },
  );
}

export function deleteLoan(bookId: string, loanId: string): Promise<void> {
  return request<void>(
    `/api/v1/books/${encodeURIComponent(bookId)}/loans/${encodeURIComponent(loanId)}`,
    { method: "DELETE" },
  );
}

export function patchPreviewLoan(analysisId: string, loanId: string, patch: LoanCorrection): Promise<Loan> {
  return request<Loan>(
    `/api/v1/analyses/${encodeURIComponent(analysisId)}/loans/${encodeURIComponent(loanId)}`,
    { method: "PATCH", body: JSON.stringify(patch) },
  );
}

export function patchBookLoan(bookId: string, loanId: string, patch: LoanCorrection): Promise<Loan> {
  return request<Loan>(
    `/api/v1/books/${encodeURIComponent(bookId)}/loans/${encodeURIComponent(loanId)}`,
    { method: "PATCH", body: JSON.stringify(patch) },
  );
}

export function postGrouping(analysisId: string, body: GroupingRequest): Promise<Analysis> {
  return request<Analysis>(`/api/v1/analyses/${encodeURIComponent(analysisId)}/grouping`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function fetchLoanImage(bookId: string, loanId: string, imageId: string): Promise<Blob> {
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/books/${encodeURIComponent(bookId)}/loans/${encodeURIComponent(loanId)}/images/${encodeURIComponent(imageId)}`,
    { headers: { Accept: "image/*,*/*" } },
  );
  if (!response.ok) {
    throw await readError(response);
  }
  return response.blob();
}

let pendingEnsure: Promise<BookCreated | Book> | null = null;

/** Load the stored book, or create one when that id is absent or no longer found. */
export function ensureBook(): Promise<BookCreated | Book> {
  if (!pendingEnsure) {
    pendingEnsure = loadOrCreateBook().finally(() => {
      pendingEnsure = null;
    });
  }
  return pendingEnsure;
}

async function loadOrCreateBook(): Promise<BookCreated | Book> {
  const existing = readBookId();
  if (existing) {
    try {
      return await getBook(existing);
    } catch (error) {
      if (!(error instanceof ApiClientError) || error.code !== "BOOK_NOT_FOUND") {
        throw error;
      }
    }
  }
  const created = await createBook();
  writeBookId(created.book_id);
  return created;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData;
  if (init?.body !== undefined && !isFormData && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${apiBaseUrl()}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    throw await readError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiClientError(response.status, "INTERNAL_ERROR", "The response was not valid JSON.", true, {});
  }
  return payload as T;
}

async function readError(response: Response): Promise<ApiClientError> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (isEnvelope(payload)) {
    return new ApiClientError(
      response.status,
      payload.error.code,
      payload.error.message,
      payload.error.retryable,
      readDetails(payload.error.details),
    );
  }
  return new ApiClientError(
    response.status,
    "INTERNAL_ERROR",
    "The request failed.",
    response.status >= 500,
    {},
  );
}

function isEnvelope(
  value: unknown,
): value is { error: { code: string; message: string; retryable: boolean; details?: unknown } } {
  if (!isRecord(value) || !isRecord(value.error)) {
    return false;
  }
  const error = value.error;
  return typeof error.code === "string" && typeof error.message === "string" && typeof error.retryable === "boolean";
}

function readDetails(value: unknown): Record<string, string | number | boolean | null> {
  if (!isRecord(value)) {
    return {};
  }
  const details: Record<string, string | number | boolean | null> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === "string" || typeof item === "number" || typeof item === "boolean" || item === null) {
      details[key] = item;
    }
  }
  return details;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
