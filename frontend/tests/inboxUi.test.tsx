import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BOOK_ID_STORAGE_KEY } from "../src/api/client";
import type { BookInbox, InboxDraft } from "../src/api/types";
import { InboxSummary } from "../src/components/InboxSummary";
import { BookScreen } from "../src/screens/BookScreen";
import { PreviewScreen } from "../src/screens/PreviewScreen";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function draft(overrides: Partial<InboxDraft> & Pick<InboxDraft, "analysis_id" | "status" | "inbox_bucket">): InboxDraft {
  return {
    pages_done: 0,
    pages_total: 1,
    image_count: 1,
    created_at: "2026-09-25T10:00:00Z",
    queued_at: null,
    retryable: false,
    error: null,
    ...overrides,
  };
}

function specInbox(): BookInbox {
  const review = draft({
    analysis_id: "anl_review",
    status: "completed_needs_review",
    inbox_bucket: "needs_review",
    pages_done: 2,
    pages_total: 2,
    image_count: 2,
    created_at: "2026-09-25T08:00:00Z",
  });
  return {
    book_id: "bok_01",
    counts: { queued: 3, running: 0, ready: 2, needs_review: 1, failed: 1 },
    drafts: [
      Object.assign(review, {
        borrower_name: "Asha Private",
        sanctioned_amount: "15000.00",
        loan_id: "LN-999",
        summary_line: "15k, got 13230",
      }),
      draft({
        analysis_id: "anl_ready_a",
        status: "completed",
        inbox_bucket: "ready",
        created_at: "2026-09-25T09:00:00Z",
      }),
      draft({
        analysis_id: "anl_ready_b",
        status: "completed",
        inbox_bucket: "ready",
        created_at: "2026-09-25T09:30:00Z",
      }),
      draft({
        analysis_id: "anl_q1",
        status: "queued",
        inbox_bucket: "queued",
        pages_done: 3,
        pages_total: 8,
        image_count: 8,
        created_at: "2026-09-25T11:00:00Z",
        queued_at: "2026-09-25T11:01:00Z",
      }),
      draft({
        analysis_id: "anl_q2",
        status: "queued",
        inbox_bucket: "queued",
        created_at: "2026-09-25T11:05:00Z",
      }),
      draft({
        analysis_id: "anl_q3",
        status: "queued",
        inbox_bucket: "queued",
        created_at: "2026-09-25T11:10:00Z",
      }),
      draft({
        analysis_id: "anl_failed",
        status: "failed",
        inbox_bucket: "failed",
        pages_done: 1,
        pages_total: 2,
        created_at: "2026-09-25T07:00:00Z",
        retryable: true,
        error: { code: "VISION_TIMEOUT", message: "The vision service timed out." },
      }),
    ],
  };
}

function countLine(): string {
  return (document.querySelector(".inbox-counts")?.textContent ?? "").replace(/\s+/g, " ").trim();
}

describe("inbox UI", () => {
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

  it("renders inbox counts and does not label completed_needs_review as Ready", () => {
    render(
      <MemoryRouter>
        <InboxSummary inbox={specInbox()} retryingId={null} onRetry={() => undefined} />
      </MemoryRouter>,
    );

    expect(countLine()).toBe("2 ready · 1 needs review · 3 queued · 1 failed");
    expect(screen.queryByRole("button", { name: "0 running" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "2 ready" }));
    const readyList = screen.getByRole("list", { name: "Ready" });
    expect(within(readyList).getByRole("link", { name: "anl_ready_a" }).getAttribute("href")).toBe("/a/anl_ready_a");
    expect(within(readyList).queryByText("anl_review")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "1 needs review" }));
    const reviewList = screen.getByRole("list", { name: "Needs review" });
    expect(within(reviewList).getByRole("link", { name: "anl_review" }).getAttribute("href")).toBe("/a/anl_review");
    expect(within(reviewList).queryByText(/ready/i)).toBeNull();
    expect(reviewList.textContent).not.toMatch(/15000|Asha Private|LN-999|13230/);
    expect(document.body.textContent).not.toMatch(/15000|Asha Private|LN-999|13230/);

    fireEvent.click(screen.getByRole("button", { name: "3 queued" }));
    const queuedList = screen.getByRole("list", { name: "Queued" });
    expect(within(queuedList).getAllByText("1/6 queued")).toHaveLength(3);
    expect(within(queuedList).queryByText("3/8 pages")).toBeNull();
    expect(within(queuedList).getByText("2026-09-25T11:00:00Z")).toBeTruthy();
    expect(within(queuedList).queryByRole("link")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "1 failed" }));
    expect(screen.getByText("The vision service timed out.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("hides Retry when the failed draft is not retryable", () => {
    const inbox: BookInbox = {
      book_id: "bok_01",
      counts: { queued: 0, running: 0, ready: 0, needs_review: 0, failed: 1 },
      drafts: [
        draft({
          analysis_id: "anl_stop",
          status: "failed",
          inbox_bucket: "failed",
          retryable: false,
          error: { code: "NO_LOAN_DOCUMENTS", message: "No loan documents were found." },
        }),
      ],
    };
    render(
      <MemoryRouter>
        <InboxSummary inbox={inbox} retryingId={null} onRetry={() => undefined} />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "1 failed" }));
    expect(screen.getByText("No loan documents were found.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("shows the count line on the book and retries a failed draft without accepting it", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    let inbox = specInbox();
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith("/analyze")) {
        inbox = {
          ...specInbox(),
          counts: { queued: 4, running: 0, ready: 2, needs_review: 1, failed: 0 },
          drafts: specInbox().drafts.filter((row) => row.analysis_id !== "anl_failed").concat(
            draft({
              analysis_id: "anl_failed",
              status: "queued",
              inbox_bucket: "queued",
              pages_done: 1,
              pages_total: 2,
              created_at: "2026-09-25T07:00:00Z",
              queued_at: "2026-09-25T12:00:00Z",
            }),
          ),
        };
        return Promise.resolve(
          jsonResponse(
            {
              analysis_id: "anl_failed",
              status: "queued",
              created_at: "2026-09-25T07:00:00Z",
              completed_at: null,
              expires_at: null,
              error: null,
              pages_done: 1,
              pages_total: 2,
              images: [],
              lender_groups: [],
            },
            202,
          ),
        );
      }
      if (href.endsWith("/inbox")) {
        return Promise.resolve(jsonResponse(inbox));
      }
      if (href.endsWith("/summary")) {
        return Promise.resolve(
          jsonResponse({
            book_id: "bok_01",
            totals: { received: "1.00", still_to_pay: null, extra: null, incomplete: false },
            groups: [],
          }),
        );
      }
      return Promise.resolve(
        jsonResponse({
          book_id: "bok_01",
          created_at: "2026-09-22T15:00:00Z",
          totals: { received: null, still_to_pay: null, extra: null, incomplete: false },
          lender_groups: [],
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<BookScreen />} />
          <Route path="/a/:analysisId" element={<p>Preview open</p>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Received 1.00")).toBeTruthy();
    expect(countLine()).toBe("2 ready · 1 needs review · 3 queued · 1 failed");
    const counts = document.querySelector(".inbox-counts");
    const received = screen.getByText("Received 1.00");
    expect(counts).toBeTruthy();
    expect(counts!.compareDocumentPosition(received) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "1 needs review" }));
    expect(screen.getByRole("link", { name: "anl_review" }).getAttribute("href")).toBe("/a/anl_review");
    expect(document.body.textContent).not.toMatch(/15000|Asha Private|LN-999|13230/);

    fireEvent.click(screen.getByRole("button", { name: "1 failed" }));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => {
      expect(countLine()).toBe("2 ready · 1 needs review · 4 queued");
    });
    expect(screen.queryByText("Preview open")).toBeNull();
    expect(screen.queryByText("Analyzing…")).toBeNull();
    const analyzeCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/analyze"));
    expect(analyzeCall?.[0]).toBe("http://127.0.0.1:8002/api/v1/analyses/anl_failed/analyze");
    expect((analyzeCall?.[1] as RequestInit).method).toBe("POST");
    expect((analyzeCall?.[1] as RequestInit).body).toBe("{}");
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes("/accept") || url.includes("/images"))).toBe(false);
  });

  it("retries a failed preview onto the same draft and returns home without accepting", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith("/analyze") && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              analysis_id: "anl_failed",
              status: "queued",
              created_at: "2026-09-25T07:00:00Z",
              completed_at: null,
              expires_at: null,
              error: null,
              pages_done: 1,
              pages_total: 2,
              images: [],
              lender_groups: [],
            },
            202,
          ),
        );
      }
      return Promise.resolve(
        jsonResponse({
          analysis_id: "anl_failed",
          status: "failed",
          created_at: "2026-09-25T07:00:00Z",
          completed_at: null,
          expires_at: null,
          error: { code: "VISION_TIMEOUT", message: "The vision service timed out." },
          pages_done: 1,
          pages_total: 2,
          images: [],
          lender_groups: [],
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/a/anl_failed"]}>
        <Routes>
          <Route path="/a/:analysisId" element={<PreviewScreen />} />
          <Route path="/" element={<p>Book home</p>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "The vision service timed out.");
    expect((screen.getByRole("button", { name: "Add to book" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Book home")).toBeTruthy();
    expect(screen.queryByText("Analyzing…")).toBeNull();
    const analyzeCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/analyze"));
    expect(analyzeCall?.[0]).toBe("http://127.0.0.1:8002/api/v1/analyses/anl_failed/analyze");
    expect((analyzeCall?.[1] as RequestInit).body).toBe("{}");
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes("/accept") || url.includes("/images"))).toBe(false);
  });
});
