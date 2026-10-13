import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BOOK_ID_STORAGE_KEY } from "../src/api/client";
import { BookScreen } from "../src/screens/BookScreen";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("BookScreen", () => {
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

  it("creates a book on first visit and stores loanlens.book_id", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).endsWith("/summary")) {
        return Promise.resolve(
          jsonResponse({
            book_id: "bok_screen",
            totals: { received: "0.00", still_to_pay: "0.00", extra: "0.00", incomplete: false },
            groups: [],
          }),
        );
      }
      return Promise.resolve(jsonResponse({ book_id: "bok_screen", created_at: "2026-09-22T15:00:00Z" }, 201));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <StrictMode>
        <MemoryRouter>
          <BookScreen />
        </MemoryRouter>
      </StrictMode>,
    );

    expect(screen.getByRole("heading", { name: "Book" })).toBeTruthy();
    expect(await screen.findByText("Received 0.00")).toBeTruthy();
    expect(screen.getByText("Still to pay 0.00")).toBeTruthy();
    expect(screen.getByText("Extra 0.00")).toBeTruthy();
    await waitFor(() => {
      expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_screen");
    });
    expect(sessionStorage.getItem(BOOK_ID_STORAGE_KEY)).toBeNull();
    const posts = fetchMock.mock.calls.filter((call) => (call[1] as RequestInit | undefined)?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0]?.[0]).toBe("http://127.0.0.1:8002/api/v1/books");
  });

  it("renders two group headers and the three totals from the summary", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (String(url).endsWith("/summary")) {
          return Promise.resolve(jsonResponse(twoGroupSummary()));
        }
        return Promise.resolve(jsonResponse(storedBook("bok_01")));
      }),
    );

    render(
      <MemoryRouter>
        <BookScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Dhanrishi (Gagandeep)" })).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "15k, got 13230 on jun 17, pay 16950 on jun 30, extra 3720 (28.1%)" }).getAttribute("href"),
    ).toBe("/loans/lon_dhanrishi");
    expect(screen.getByRole("heading", { name: "snappaisa (empire finance)" })).toBeTruthy();
    expect(screen.getByText("Received 28467")).toBeTruthy();
    expect(screen.getByText("Still to pay 39750")).toBeTruthy();
    expect(screen.getByText("Extra 11283")).toBeTruthy();
    expect(
      screen.getByText("15k, got 13230 on jun 17, pay 16950 on jun 30, extra 3720 (28.1%)"),
    ).toBeTruthy();
    expect(screen.getByText("20k, got 17640, pay 22800 on jul 1, extra 5160 (29.3%)")).toBeTruthy();
    expect(screen.queryByText("Some loans are missing amounts.")).toBeNull();
    expect(screen.queryByText("null")).toBeNull();
  });

  it("says when some loans are missing amounts and still shows the summed totals", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (String(url).endsWith("/summary")) {
          return Promise.resolve(
            jsonResponse({
              ...twoGroupSummary(),
              totals: { received: "13230.00", still_to_pay: "16950.00", extra: "3720.00", incomplete: true },
            }),
          );
        }
        return Promise.resolve(jsonResponse(storedBook("bok_01")));
      }),
    );

    render(
      <MemoryRouter>
        <BookScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Some loans are missing amounts.")).toBeTruthy();
    expect(screen.getByText("Received 13230.00")).toBeTruthy();
    expect(screen.getByText("Still to pay 16950.00")).toBeTruthy();
    expect(screen.getByText("Extra 3720.00")).toBeTruthy();
  });

  it("deletes only the chosen loan and keeps loanlens.book_id", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    let removed = false;
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (init?.method === "DELETE") {
        removed = true;
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      if (href.endsWith("/summary")) {
        if (removed) {
          return Promise.resolve(
            jsonResponse({
              book_id: "bok_01",
              totals: { received: "17640.00", still_to_pay: "22800.00", extra: "5160.00", incomplete: false },
              groups: [twoGroupSummary().groups[1]],
            }),
          );
        }
        return Promise.resolve(jsonResponse(twoGroupSummary()));
      }
      return Promise.resolve(jsonResponse(storedBook("bok_01")));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter>
        <BookScreen />
      </MemoryRouter>,
    );

    const removeDhanrishi = await screen.findByRole("button", {
      name: "Remove loan 15k, got 13230 on jun 17, pay 16950 on jun 30, extra 3720 (28.1%)",
    });
    fireEvent.click(removeDhanrishi);

    expect(await screen.findByText("Received 17640.00")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Dhanrishi (Gagandeep)" })).toBeNull();
    expect(screen.getByRole("heading", { name: "snappaisa (empire finance)" })).toBeTruthy();
    expect(screen.queryByText("15k, got 13230 on jun 17, pay 16950 on jun 30, extra 3720 (28.1%)")).toBeNull();
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01");
    const deleteCall = fetchMock.mock.calls.find((call) => (call[1] as RequestInit | undefined)?.method === "DELETE");
    expect(deleteCall?.[0]).toBe("http://127.0.0.1:8002/api/v1/books/bok_01/loans/lon_dhanrishi");
  });
});

function storedBook(bookId: string) {
  return {
    book_id: bookId,
    created_at: "2026-09-22T15:00:00Z",
    totals: { received: null, still_to_pay: null, extra: null, incomplete: false },
    lender_groups: [],
  };
}

function twoGroupSummary() {
  return {
    book_id: "bok_01",
    totals: {
      received: "28467",
      still_to_pay: "39750",
      extra: "11283",
      incomplete: false,
    },
    groups: [
      {
        header: "Dhanrishi (Gagandeep)",
        lines: [
          {
            loan_internal_id: "lon_dhanrishi",
            text: "15k, got 13230 on jun 17, pay 16950 on jun 30, extra 3720 (28.1%)",
            needs_review: false,
          },
        ],
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
  };
}
