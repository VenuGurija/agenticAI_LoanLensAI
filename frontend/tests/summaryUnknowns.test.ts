import { createElement, type ReactNode } from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BOOK_ID_STORAGE_KEY } from "../src/api/client";
import { BookScreen } from "../src/screens/BookScreen";
import { LoanDetailScreen } from "../src/screens/LoanDetailScreen";
import { PreviewScreen } from "../src/screens/PreviewScreen";

const KNOWN_LINE = "15k, got 13230 on jun 17, pay 16950 on jun 30, extra 3720 (28.1%)";
const UNKNOWN_LINE = "amount unknown, pay 16950";
const PARTIAL_LINE = "99k, got 1, pay 2, extra 1 (1.0%)";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function node(type: unknown, props: Record<string, unknown> | null, ...children: ReactNode[]) {
  return createElement(type as never, props, ...children);
}

describe("summary unknowns, grouping, and failures", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("groups the book by lender and leaves unknown totals and lines blank", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (String(url).endsWith("/summary")) {
          return Promise.resolve(jsonResponse(groupedBookSummary()));
        }
        return Promise.resolve(jsonResponse(storedBook()));
      }),
    );

    render(node(MemoryRouter, null, node(BookScreen, null)));

    expect(await screen.findByRole("heading", { name: "Dhanrishi (Gagandeep)" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "snappaisa" })).toBeTruthy();
    expect(screen.getByRole("link", { name: KNOWN_LINE }).getAttribute("href")).toBe("/loans/lon_dhanrishi");
    expect(screen.getByText("Received 13230.00")).toBeTruthy();
    expect(screen.getByText("Still to pay").textContent).toBe("Still to pay");
    expect(screen.getByText("Extra").textContent).toBe("Extra");
    expect(screen.getByText("Some loans are missing amounts.")).toBeTruthy();
    expect(screen.queryByText("Received 0")).toBeNull();
    expect(screen.queryByText("Still to pay 0")).toBeNull();
    expect(screen.queryByText("Extra 0")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
    expect(document.body.textContent).not.toContain("NaN");

    const unknown = screen.getByRole("link", { name: UNKNOWN_LINE });
    expect(unknown.querySelector(".unknown-value")?.textContent).toBe("unknown");
    expect(withoutKnownAmount(unknown.textContent)).not.toContain("0");
    expect(unknown.textContent).not.toContain("NaN");
    expect(unknown.closest(".summary-line")?.querySelector(".review-badge")?.textContent).toBe("Needs review");

    const blank = screen.getByRole("link", { name: "unknown" });
    expect(blank.querySelector(".unknown-value")?.textContent).toBe("unknown");
    expect(blank.textContent).not.toMatch(/0|NaN/);
    expect(screen.getByRole("link", { name: "Needs review" })).toBeTruthy();
  });

  it("renders proposed groups and keeps unknown preview lines blank", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (String(url).endsWith("/summary")) {
          return Promise.resolve(
            jsonResponse({
              analysis_id: "anl_01",
              status: "completed_needs_review",
              groups: [
                {
                  header: "Dhanrishi (Gagandeep)",
                  lines: [{ loan_internal_id: "lon_dhanrishi", text: KNOWN_LINE, needs_review: false }],
                },
                {
                  header: "snappaisa",
                  lines: [{ loan_internal_id: "lon_snappaisa", text: UNKNOWN_LINE, needs_review: true }],
                },
              ],
            }),
          );
        }
        return Promise.resolve(jsonResponse(previewAnalysis("completed_needs_review", null)));
      }),
    );

    render(
      node(
        MemoryRouter,
        { initialEntries: ["/a/anl_01"] },
        node(Routes, null, node(Route, { path: "/a/:analysisId", element: node(PreviewScreen, null) })),
      ),
    );

    expect(await screen.findByRole("heading", { name: "Dhanrishi (Gagandeep)" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "snappaisa" })).toBeTruthy();
    expect(summaryLine(KNOWN_LINE).textContent).toBe(KNOWN_LINE);
    const unknown = summaryLine(UNKNOWN_LINE);
    expect(unknown.querySelector(".unknown-value")?.textContent).toBe("unknown");
    expect(withoutKnownAmount(unknown.textContent)).not.toContain("0");
    expect(unknown.textContent).not.toContain("NaN");
    expect(unknown.closest(".summary-line")?.querySelector(".review-badge")?.textContent).toBe("Needs review");
    expect(document.querySelector(".screen-header .review-badge")?.textContent).toBe("Needs review");
    expect(screen.queryByText(/^Received/)).toBeNull();
    expect(screen.queryByText("Received 0")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
    expect(document.body.textContent).not.toContain("NaN");
    expect((screen.getByLabelText("New value") as HTMLInputElement).value).toBe("");
  });

  it("shows unknown loan fields as unknown and keeps a stored zero", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(unknownLoanBook()))),
    );

    render(
      node(
        MemoryRouter,
        { initialEntries: ["/loans/lon_01"] },
        node(Routes, null, node(Route, { path: "/loans/:loanInternalId", element: node(LoanDetailScreen, null) })),
      ),
    );

    const extracted = within(await screen.findByRole("region", { name: "Extracted" }));
    const calculated = within(screen.getByRole("region", { name: "Calculated" }));

    expectUnknownField(extracted.getByText(/Sanctioned amount:/));
    expectUnknownField(extracted.getByText(/Disbursed amount:/));
    expectUnknownField(extracted.getByText(/Repayment amount:/));
    expectUnknownField(extracted.getByText(/GST amount:/));
    expectUnknownField(calculated.getByText(/Extra rupees:/));
    expectUnknownField(calculated.getByText(/Extra percent:/));

    const fee = extracted.getByText(/Processing fee:/);
    expect(fee.textContent).toContain("0.00");
    expect(fee.textContent).toContain("extracted");
    expect(fee.querySelector(".unknown-value")).toBeNull();

    expect(screen.queryByText(/extra NaN/)).toBeNull();
    expect(screen.queryByText("extra 0 (0%)")).toBeNull();
    expect(document.body.textContent).not.toContain("NaN");
    expect((screen.getByLabelText("New value") as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows the failed analysis message and hides partial groups", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).endsWith("/summary")) {
        return Promise.resolve(
          jsonResponse({
            analysis_id: "anl_01",
            status: "failed",
            groups: [
              {
                header: "Dhanrishi (Gagandeep)",
                lines: [{ loan_internal_id: "lon_fake", text: PARTIAL_LINE, needs_review: false }],
              },
            ],
          }),
        );
      }
      return Promise.resolve(
        jsonResponse(
          previewAnalysis("failed", {
            code: "NO_LOAN_DOCUMENTS",
            message: "No loan documents were found.",
          }),
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      node(
        MemoryRouter,
        { initialEntries: ["/a/anl_01"] },
        node(Routes, null, node(Route, { path: "/a/:analysisId", element: node(PreviewScreen, null) })),
      ),
    );

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "No loan documents were found.");
    expect(screen.queryByText(PARTIAL_LINE)).toBeNull();
    expect(screen.queryByRole("heading", { name: "Dhanrishi (Gagandeep)" })).toBeNull();
    expect(screen.queryByText("Received 0")).toBeNull();
    expect(document.body.textContent).not.toContain("NaN");
    expect((screen.getByRole("button", { name: "Add to book" }) as HTMLButtonElement).disabled).toBe(true);
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01");
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.endsWith("/summary") || url.includes("/accept"))).toBe(false);
    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
    });
  });

  it("shows the error envelope message instead of a summary", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        jsonResponse(
          {
            error: {
              code: "ANALYSIS_NOT_FOUND",
              message: "That analysis was not found.",
              retryable: false,
              details: {},
            },
          },
          404,
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      node(
        MemoryRouter,
        { initialEntries: ["/a/anl_01"] },
        node(Routes, null, node(Route, { path: "/a/:analysisId", element: node(PreviewScreen, null) })),
      ),
    );

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "That analysis was not found.");
    expect(screen.queryByRole("heading", { name: "Dhanrishi (Gagandeep)" })).toBeNull();
    expect(screen.queryByText(KNOWN_LINE)).toBeNull();
    expect(screen.queryByText("Received 0")).toBeNull();
    expect(document.body.textContent).not.toContain("NaN");
    expect((screen.getByRole("button", { name: "Add to book" }) as HTMLButtonElement).disabled).toBe(true);
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01");
    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
    });
  });
});

function withoutKnownAmount(text: string | null): string {
  return (text ?? "").replace("16950", "");
}

function expectUnknownField(line: HTMLElement) {
  expect(line.querySelector(".unknown-value")?.textContent).toBe("unknown");
  expect(line.textContent).not.toContain("NaN");
  expect(line.textContent).not.toMatch(/:\s*0/);
}

function summaryLine(text: string): HTMLElement {
  const line = [...document.querySelectorAll(".summary-line p, .summary-line a")].find(
    (item) => item.textContent === text,
  );
  if (!(line instanceof HTMLElement)) {
    throw new Error("summary line was not rendered");
  }
  return line;
}

function storedBook() {
  return {
    book_id: "bok_01",
    created_at: "2026-09-22T15:00:00Z",
    totals: { received: null, still_to_pay: null, extra: null, incomplete: true },
    lender_groups: [],
  };
}

function groupedBookSummary() {
  return {
    book_id: "bok_01",
    totals: { received: "13230.00", still_to_pay: null, extra: null, incomplete: true },
    groups: [
      {
        header: "Dhanrishi (Gagandeep)",
        lines: [
          { loan_internal_id: "lon_dhanrishi", text: KNOWN_LINE, needs_review: false },
          { loan_internal_id: "lon_unknown", text: UNKNOWN_LINE, needs_review: true },
        ],
      },
      {
        header: "snappaisa",
        lines: [
          { loan_internal_id: "lon_blank", text: "   ", needs_review: true },
          { loan_internal_id: "lon_unsummarized", text: "Needs review", needs_review: true },
        ],
      },
    ],
  };
}

function previewAnalysis(status: string, error: { code: string; message: string } | null) {
  return {
    analysis_id: "anl_01",
    status,
    created_at: "2026-09-22T15:00:00Z",
    completed_at: status === "failed" ? null : "2026-09-22T15:00:12Z",
    expires_at: "2026-09-23T15:00:00Z",
    error,
    images: [],
    lender_groups: [
      {
        lender_group_id: "ldg_01",
        lender_brand: "Dhanrishi",
        legal_lending_entity: "Gagandeep",
        loans: [
          {
            loan_internal_id: "lon_dhanrishi",
            image_ids: [],
            summary_line: KNOWN_LINE,
          },
          {
            loan_internal_id: "lon_snappaisa",
            image_ids: [],
            summary_line: UNKNOWN_LINE,
          },
        ],
      },
    ],
  };
}

function unknownLoanBook() {
  return {
    book_id: "bok_01",
    created_at: "2026-09-22T15:00:00Z",
    totals: { received: null, still_to_pay: null, extra: null, incomplete: true },
    lender_groups: [
      {
        lender_group_id: "ldg_01",
        lender_brand: null,
        legal_lending_entity: null,
        loans: [
          {
            loan_internal_id: "lon_01",
            external_loan_id: null,
            needs_review: true,
            review_flags: ["LOW_CONFIDENCE_FIELD"],
            image_ids: ["img_01"],
            fields: {
              sanctioned_amount: { value: null, currency: null, source: "unknown", confidence: "unknown" },
              disbursed_amount: { value: null, currency: null, source: "unknown", confidence: "unknown" },
              processing_fee: { value: "0.00", currency: "INR", source: "extracted", confidence: "high" },
              gst_amount: { value: null, currency: null, source: "unknown", confidence: "unknown" },
              repayment_amount: { value: null, currency: null, source: "unknown", confidence: "unknown" },
            },
            calculated: {
              implied_cost: { value: null, source: "unknown" },
              extra_percent: { value: null, source: "unknown" },
            },
            validations: [],
            validation_summary: "incomplete",
            summary_line: UNKNOWN_LINE,
          },
        ],
      },
    ],
  };
}
