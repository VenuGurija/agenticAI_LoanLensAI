import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BOOK_ID_STORAGE_KEY } from "../src/api/client";
import { LoanDetailScreen } from "../src/screens/LoanDetailScreen";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("LoanDetailScreen", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:loan-screenshot"),
      revokeObjectURL: vi.fn(),
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("shows extracted, calculated, user-corrected, and unknown values, and hides the screenshot until the button", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const href = String(url);
      if (href.includes("/images/")) {
        return Promise.resolve(new Response(new Uint8Array([1, 2, 3]), { status: 200, headers: { "Content-Type": "image/png" } }));
      }
      return Promise.resolve(jsonResponse(bookWithLoan()));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/loans/lon_01"]}>
        <Routes>
          <Route path="/loans/:loanInternalId" element={<LoanDetailScreen />} />
        </Routes>
      </MemoryRouter>,
    );

    const extracted = within(await screen.findByRole("region", { name: "Extracted" }));
    const calculated = within(screen.getByRole("region", { name: "Calculated" }));
    expect(screen.getByRole("heading", { name: "Checks" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Review" })).toBeTruthy();

    expect(extracted.getByText(/Sanctioned amount:/).textContent).toContain("15000.00");
    expect(extracted.getByText(/Sanctioned amount:/).textContent).toContain("user_corrected");
    expect(extracted.getByText(/Disbursed amount:/).textContent).toContain("13230.00");
    expect(extracted.getByText(/Disbursed amount:/).textContent).toContain("extracted");
    const gst = extracted.getByText(/GST amount:/).textContent ?? "";
    expect(gst).toContain("unknown");
    expect(gst).not.toContain("0");
    const fee = extracted.getByText(/Processing fee:/).textContent ?? "";
    expect(fee).toContain("0.00");
    expect(fee).toContain("extracted");
    expect(extracted.queryByText(/Extra rupees/)).toBeNull();

    expect(calculated.getByText(/Extra rupees:/).textContent).toContain("3720.00");
    expect(calculated.getByText(/Extra rupees:/).textContent).toContain("calculated");
    expect(calculated.getByText(/Extra percent:/).textContent).toContain("28.1");
    const impliedFees = calculated.getByText(/Implied fees:/).textContent ?? "";
    expect(impliedFees).toContain("unknown");
    expect(impliedFees).not.toContain("0");

    expect(screen.getByText("Needs review")).toBeTruthy();
    expect(screen.getByText("LOW_CONFIDENCE_FIELD")).toBeTruthy();
    expect(screen.getByText(/V_POSITIVE_DISBURSED: passed/)).toBeTruthy();
    expect(screen.queryByText("null")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/images/"))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Show screenshot" }));

    const image = await screen.findByRole("img", { name: "Loan screenshot" });
    expect(image.getAttribute("src")).toBe("blob:loan-screenshot");
    const imageCall = fetchMock.mock.calls.find((call) => String(call[0]).includes("/images/"));
    expect(imageCall?.[0]).toBe("http://127.0.0.1:8002/api/v1/books/bok_01/loans/lon_01/images/img_01");
  });

  it("saves a book correction and shows the returned extra rupees and percentage", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return Promise.resolve(
          jsonResponse({
            ...bookWithLoan().lender_groups[0].loans[0],
            calculated: {
              implied_fees_total: { value: null, source: "unknown" },
              fee_plus_gst: { value: null, source: "unknown" },
              implied_cost: { value: "3720.00", currency: "INR", source: "calculated" },
              extra_percent: { value: "28.1", source: "calculated" },
              display_sanctioned_short: { value: "15k", source: "calculated" },
            },
          }),
        );
      }
      return Promise.resolve(jsonResponse(bookWithLoan()));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/loans/lon_01"]}>
        <Routes>
          <Route path="/loans/:loanInternalId" element={<LoanDetailScreen />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Extracted" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("New value"), { target: { value: "13230.00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save correction" }));

    expect(await screen.findByText("extra 3720 (28.1%)")).toBeTruthy();
    const patch = fetchMock.mock.calls.find((call) => (call[1] as RequestInit | undefined)?.method === "PATCH");
    expect(patch?.[0]).toBe("http://127.0.0.1:8002/api/v1/books/bok_01/loans/lon_01");
    expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({ disbursed_amount: "13230.00" });
  });
});

function bookWithLoan() {
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
            needs_review: true,
            review_flags: ["LOW_CONFIDENCE_FIELD"],
            image_ids: ["img_01"],
            fields: {
              lender_brand: { value: "Dhanrishi", source: "extracted", confidence: "high" },
              sanctioned_amount: { value: "15000.00", currency: "INR", source: "user_corrected", confidence: "high" },
              disbursed_amount: { value: "13230.00", currency: "INR", source: "extracted", confidence: "high" },
              processing_fee: { value: "0.00", currency: "INR", source: "extracted", confidence: "high" },
              gst_amount: { value: null, currency: null, source: "unknown", confidence: "unknown" },
              repayment_amount: { value: "16950.00", currency: "INR", source: "extracted", confidence: "high" },
            },
            calculated: {
              implied_fees_total: { value: null, source: "unknown" },
              fee_plus_gst: { value: null, source: "unknown" },
              implied_cost: { value: "3720.00", currency: "INR", source: "calculated" },
              extra_percent: { value: "28.1", source: "calculated" },
              display_sanctioned_short: { value: "15k", source: "calculated" },
            },
            validations: [{ id: "V_POSITIVE_DISBURSED", status: "passed", message: "Disbursed amount is positive." }],
            validation_summary: "incomplete",
            summary_line: "15k, got 13230, pay 16950, extra 3720 (28.1%)",
          },
        ],
      },
    ],
  };
}
