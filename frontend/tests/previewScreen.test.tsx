import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ANALYSIS_ID_STORAGE_KEY, BOOK_ID_STORAGE_KEY } from "../src/api/client";
import { PreviewScreen } from "../src/screens/PreviewScreen";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const DHANRISHI_LINE = "15k, got 13230 on jun 17, pay 16950 on jun 30, extra 3720 (28.1%)";
const FAKE_LINE = "99k, got 1, pay 2, extra 1 (1.0%)";

describe("PreviewScreen", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    sessionStorage.setItem(ANALYSIS_ID_STORAGE_KEY, "anl_01");
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("renders summary lines and Add to book returns home without dropping the book id", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith("/summary")) {
        return Promise.resolve(
          jsonResponse({
            analysis_id: "anl_01",
            status: "completed",
            groups: [
              {
                header: "Dhanrishi (Gagandeep)",
                lines: [{ loan_internal_id: "lon_dhanrishi", text: DHANRISHI_LINE, needs_review: false }],
              },
              {
                header: "snappaisa (empire finance)",
                lines: [
                  {
                    loan_internal_id: "lon_snappaisa",
                    text: "20k, got 17640, pay 22800 on jul 1, extra 5160 (29.3%)",
                    needs_review: false,
                  },
                ],
              },
            ],
          }),
        );
      }
      if (href.endsWith("/accept") && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ book_id: "bok_01", loan_internal_ids: ["lon_dhanrishi", "lon_snappaisa"] }));
      }
      return Promise.resolve(jsonResponse(analysisBody("completed", null)));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/a/anl_01"]}>
        <Routes>
          <Route path="/a/:analysisId" element={<PreviewScreen />} />
          <Route path="/" element={<p>Book home</p>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Dhanrishi (Gagandeep)" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "snappaisa (empire finance)" })).toBeTruthy();
    expect(screen.getByText(DHANRISHI_LINE)).toBeTruthy();
    expect(screen.queryByText(/^Received/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Remove loan/ })).toBeNull();

    const add = screen.getByRole("button", { name: "Add to book" }) as HTMLButtonElement;
    expect(add.disabled).toBe(false);
    fireEvent.click(add);

    expect(await screen.findByText("Book home")).toBeTruthy();
    expect(sessionStorage.getItem(ANALYSIS_ID_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01");
    const acceptCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/accept"));
    expect(acceptCall?.[0]).toBe("http://127.0.0.1:8002/api/v1/books/bok_01/drafts/anl_01/accept");
    expect((acceptCall?.[1] as RequestInit).method).toBe("POST");
    expect((acceptCall?.[1] as RequestInit).body).toBe("{}");
  });

  it("shows the failed analysis message and does not change the book", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const href = String(url);
      if (href.endsWith("/summary")) {
        return Promise.resolve(
          jsonResponse({
            analysis_id: "anl_01",
            status: "failed",
            groups: [
              {
                header: "Dhanrishi (Gagandeep)",
                lines: [{ loan_internal_id: "lon_fake", text: FAKE_LINE, needs_review: false }],
              },
            ],
          }),
        );
      }
      return Promise.resolve(
        jsonResponse(
          analysisBody("failed", {
            code: "NO_LOAN_DOCUMENTS",
            message: "No loan documents were found.",
          }),
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/a/anl_01"]}>
        <Routes>
          <Route path="/a/:analysisId" element={<PreviewScreen />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "No loan documents were found.");
    expect(screen.queryByText(FAKE_LINE)).toBeNull();
    expect(screen.queryByText(DHANRISHI_LINE)).toBeNull();
    expect(screen.queryByRole("heading", { name: "Dhanrishi (Gagandeep)" })).toBeNull();
    expect((screen.getByRole("button", { name: "Add to book" }) as HTMLButtonElement).disabled).toBe(true);
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01");
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.endsWith("/summary") || url.includes("/accept") || url.includes("/loans/"))).toBe(
      false,
    );
    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
    });
  });

  it("saves a preview correction and shows the returned extra rupees and percentage", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (init?.method === "PATCH") {
        return Promise.resolve(
          jsonResponse({
            loan_internal_id: "lon_01",
            fields: {},
            calculated: {
              implied_cost: { value: "3720.00", source: "calculated" },
              extra_percent: { value: "28.1", source: "calculated" },
            },
            validations: [],
            review_flags: [],
            image_ids: ["img_01"],
            summary_line: DHANRISHI_LINE,
          }),
        );
      }
      if (href.endsWith("/summary")) {
        return Promise.resolve(
          jsonResponse({
            analysis_id: "anl_01",
            status: "completed",
            groups: [
              {
                header: "Dhanrishi (Gagandeep)",
                lines: [{ loan_internal_id: "lon_01", text: "15k, pay 16950", needs_review: false }],
              },
            ],
          }),
        );
      }
      return Promise.resolve(
        jsonResponse({
          ...analysisBody("completed", null),
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
                  fields: {},
                  calculated: {},
                  validations: [],
                  validation_summary: "incomplete",
                  summary_line: "15k, pay 16950",
                },
              ],
            },
          ],
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/a/anl_01"]}>
        <Routes>
          <Route path="/a/:analysisId" element={<PreviewScreen />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Correct a value" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("New value"), { target: { value: "13230.00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save correction" }));

    expect(await screen.findByText("extra 3720 (28.1%)")).toBeTruthy();
    const patch = fetchMock.mock.calls.find((call) => (call[1] as RequestInit | undefined)?.method === "PATCH");
    expect(patch?.[0]).toBe("http://127.0.0.1:8002/api/v1/analyses/anl_01/loans/lon_01");
    expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({ disbursed_amount: "13230.00" });
  });

  it("moves a draft screenshot by posting the grouping assignment", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith("/grouping") && init?.method === "POST") {
        return Promise.resolve(jsonResponse(twoLoanAnalysis()));
      }
      if (href.endsWith("/summary")) {
        return Promise.resolve(
          jsonResponse({
            analysis_id: "anl_01",
            status: "completed",
            groups: [
              {
                header: "Dhanrishi (Gagandeep)",
                lines: [
                  { loan_internal_id: "lon_a", text: "15k", needs_review: false },
                  { loan_internal_id: "lon_b", text: "20k", needs_review: false },
                ],
              },
            ],
          }),
        );
      }
      return Promise.resolve(jsonResponse(twoLoanAnalysis()));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/a/anl_01"]}>
        <Routes>
          <Route path="/a/:analysisId" element={<PreviewScreen />} />
        </Routes>
      </MemoryRouter>,
    );

    const moveFirst = await screen.findByLabelText("Move img_a");
    fireEvent.change(moveFirst, { target: { value: "lon_b" } });
    fireEvent.click(screen.getByRole("button", { name: "Move screenshots" }));

    await waitFor(() => {
      const posted = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/grouping"));
      expect(posted).toBeTruthy();
    });
    const posted = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/grouping"));
    expect(posted?.[0]).toBe("http://127.0.0.1:8002/api/v1/analyses/anl_01/grouping");
    expect(JSON.parse(String((posted?.[1] as RequestInit).body))).toEqual({
      loans: [
        { loan_internal_id: "lon_a", image_ids: [] },
        { loan_internal_id: "lon_b", image_ids: ["img_a", "img_b"] },
      ],
    });
  });
});

function twoLoanAnalysis() {
  return {
    analysis_id: "anl_01",
    status: "completed",
    created_at: "2026-09-22T15:00:00Z",
    completed_at: "2026-09-22T15:00:12Z",
    expires_at: "2026-09-23T15:00:00Z",
    error: null,
    images: [],
    lender_groups: [
      {
        lender_group_id: "ldg_01",
        lender_brand: "Dhanrishi",
        legal_lending_entity: "Gagandeep",
        loans: [
          {
            loan_internal_id: "lon_a",
            summary_line: "15k",
            image_ids: ["img_a"],
          },
          {
            loan_internal_id: "lon_b",
            summary_line: "20k",
            image_ids: ["img_b"],
          },
        ],
      },
    ],
  };
}

function analysisBody(status: string, error: { code: string; message: string } | null) {
  return {
    analysis_id: "anl_01",
    status,
    created_at: "2026-09-22T15:00:00Z",
    completed_at: status === "failed" || status === "completed" ? "2026-09-22T15:00:12Z" : null,
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
            loan_internal_id: "lon_hidden",
            summary_line: DHANRISHI_LINE,
          },
        ],
      },
    ],
  };
}
