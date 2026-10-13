import { createElement, type ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BOOK_ID_STORAGE_KEY } from "../src/api/client";
import { BookScreen } from "../src/screens/BookScreen";
import { LoanDetailScreen } from "../src/screens/LoanDetailScreen";
import { PreviewScreen } from "../src/screens/PreviewScreen";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function node(type: unknown, props: Record<string, unknown> | null, ...children: ReactNode[]) {
  return createElement(type as never, props, ...children);
}

describe("review UI", () => {
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

  it("shows Needs review on a completed_needs_review preview and leaves unknown amounts blank", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        const href = String(url);
        if (href.endsWith("/summary")) {
          return Promise.resolve(jsonResponse(previewSummary("completed_needs_review", true)));
        }
        return Promise.resolve(jsonResponse(previewAnalysis("completed_needs_review")));
      }),
    );

    render(
      node(
        MemoryRouter,
        { initialEntries: ["/a/anl_01"] },
        node(Routes, null, node(Route, { path: "/a/:analysisId", element: node(PreviewScreen, null) })),
      ),
    );

    expect(await screen.findByRole("heading", { name: "Dhanrishi" })).toBeTruthy();
    expect(badgeTexts()).toEqual(["Needs review", "Needs review"]);
    const line = summaryLine();
    expect(line.textContent).toBe("amount unknown, pay 16950");
    expect(line.querySelector(".unknown-value")?.textContent).toBe("unknown");
    expect(line.textContent?.replace("16950", "")).not.toContain("0");
    expect((screen.getByLabelText("New value") as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("does not show Needs review when the analysis is completed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        const href = String(url);
        if (href.endsWith("/summary")) {
          return Promise.resolve(
            jsonResponse({
              analysis_id: "anl_01",
              status: "completed",
              groups: [
                {
                  header: "Dhanrishi",
                  lines: [
                    {
                      loan_internal_id: "lon_01",
                      text: "15k, got 13230, pay 16950, extra 3720 (28.1%)",
                      needs_review: false,
                    },
                  ],
                },
              ],
            }),
          );
        }
        return Promise.resolve(jsonResponse(previewAnalysis("completed")));
      }),
    );

    render(
      node(
        MemoryRouter,
        { initialEntries: ["/a/anl_01"] },
        node(Routes, null, node(Route, { path: "/a/:analysisId", element: node(PreviewScreen, null) })),
      ),
    );

    expect(await screen.findByText("15k, got 13230, pay 16950, extra 3720 (28.1%)")).toBeTruthy();
    expect(document.querySelector(".review-badge")).toBeNull();
    expect(screen.queryByText("Needs review")).toBeNull();
  });

  it("shows Needs review on the book header and the saved loan, without filling null totals", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (String(url).endsWith("/summary")) {
          return Promise.resolve(
            jsonResponse({
              book_id: "bok_01",
              totals: { received: null, still_to_pay: null, extra: null, incomplete: true },
              groups: [
                {
                  header: "Dhanrishi",
                  lines: [
                    {
                      loan_internal_id: "lon_01",
                      text: "amount unknown, pay 16950",
                      needs_review: true,
                    },
                  ],
                },
              ],
            }),
          );
        }
        return Promise.resolve(jsonResponse(storedBook()));
      }),
    );

    render(node(MemoryRouter, null, node(BookScreen, null)));

    expect(await screen.findByRole("heading", { name: "Dhanrishi" })).toBeTruthy();
    expect(badgeTexts()).toEqual(["Needs review", "Needs review"]);
    expect(screen.getByText("Received").textContent).toBe("Received");
    expect(screen.getByText("Still to pay").textContent).toBe("Still to pay");
    expect(screen.getByText("Extra").textContent).toBe("Extra");
    expect(screen.getByText("Some loans are missing amounts.")).toBeTruthy();
    const line = screen.getByRole("link", { name: "amount unknown, pay 16950" });
    expect(line.querySelector(".unknown-value")?.textContent).toBe("unknown");
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByText("Received 0")).toBeNull();
  });

  it("hides the badge on a saved loan that does not need review", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(cleanLoanBook()))),
    );

    render(
      node(
        MemoryRouter,
        { initialEntries: ["/loans/lon_01"] },
        node(Routes, null, node(Route, { path: "/loans/:loanInternalId", element: node(LoanDetailScreen, null) })),
      ),
    );

    const gst = await screen.findByText(/GST amount:/);
    expect(gst.textContent).toContain("unknown");
    expect(gst.querySelector(".unknown-value")?.textContent).toBe("unknown");
    expect(gst.textContent).not.toContain("0");
    expect(screen.getByText(/Processing fee:/).textContent).toContain("0.00");
    expect(document.querySelector(".review-badge")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });
});

function badgeTexts(): string[] {
  return [...document.querySelectorAll(".review-badge")].map((badge) => badge.textContent ?? "");
}

function summaryLine(): HTMLElement {
  const line = document.querySelector(".summary-line p, .summary-line a");
  if (!(line instanceof HTMLElement)) {
    throw new Error("summary line was not rendered");
  }
  return line;
}

function previewAnalysis(status: string) {
  return {
    analysis_id: "anl_01",
    status,
    created_at: "2026-09-22T15:00:00Z",
    completed_at: "2026-09-22T15:00:12Z",
    expires_at: "2026-09-23T15:00:00Z",
    error: null,
    images: [],
    lender_groups: [
      {
        lender_group_id: "ldg_01",
        lender_brand: "Dhanrishi",
        legal_lending_entity: null,
        loans: [
          {
            loan_internal_id: "lon_01",
            image_ids: [],
            summary_line: "amount unknown, pay 16950",
          },
        ],
      },
    ],
  };
}

function previewSummary(status: string, needsReview: boolean) {
  return {
    analysis_id: "anl_01",
    status,
    groups: [
      {
        header: "Dhanrishi",
        lines: [
          {
            loan_internal_id: "lon_01",
            text: "amount unknown, pay 16950",
            needs_review: needsReview,
          },
        ],
      },
    ],
  };
}

function storedBook() {
  return {
    book_id: "bok_01",
    created_at: "2026-09-22T15:00:00Z",
    totals: { received: null, still_to_pay: null, extra: null, incomplete: true },
    lender_groups: [],
  };
}

function cleanLoanBook() {
  return {
    book_id: "bok_01",
    created_at: "2026-09-22T15:00:00Z",
    totals: { received: "13230.00", still_to_pay: "16950.00", extra: "3720.00", incomplete: false },
    lender_groups: [
      {
        lender_group_id: "ldg_01",
        lender_brand: "Dhanrishi",
        legal_lending_entity: "Gagandeep",
        loans: [
          {
            loan_internal_id: "lon_01",
            external_loan_id: null,
            needs_review: false,
            review_flags: [],
            image_ids: ["img_01"],
            fields: {
              sanctioned_amount: { value: "15000.00", currency: "INR", source: "extracted", confidence: "high" },
              disbursed_amount: { value: "13230.00", currency: "INR", source: "extracted", confidence: "high" },
              processing_fee: { value: "0.00", currency: "INR", source: "extracted", confidence: "high" },
              gst_amount: { value: null, currency: null, source: "unknown", confidence: "unknown" },
              repayment_amount: { value: "16950.00", currency: "INR", source: "extracted", confidence: "high" },
            },
            calculated: {
              implied_cost: { value: "3720.00", source: "calculated" },
              extra_percent: { value: "28.1", source: "calculated" },
            },
            validations: [],
            validation_summary: "ok",
            summary_line: "15k, got 13230, pay 16950, extra 3720 (28.1%)",
          },
        ],
      },
    ],
  };
}
