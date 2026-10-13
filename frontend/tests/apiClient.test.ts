import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ANALYSIS_ID_STORAGE_KEY,
  BOOK_ID_STORAGE_KEY,
  clearAnalysisId,
  createBook,
  deleteImage,
  ensureBook,
  getBook,
  uploadImages,
  health,
  readAnalysisId,
  writeAnalysisId,
} from "../src/api/client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("loads health from the default API base URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(health()).resolves.toEqual({ status: "ok" });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8002/api/v1/health",
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });

  it("creates a book and stores book_id in localStorage", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ book_id: "bok_01J", created_at: "2026-09-22T15:00:00Z" }, 201),
    );
    vi.stubGlobal("fetch", fetchMock);

    const [first, second] = await Promise.all([ensureBook(), ensureBook()]);

    expect(first.book_id).toBe("bok_01J");
    expect(second.book_id).toBe("bok_01J");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8002/api/v1/books");
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01J");
    expect(sessionStorage.getItem(BOOK_ID_STORAGE_KEY)).toBeNull();
    expect(localStorage.length).toBe(1);
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).not.toMatch(/^data:/);
  });

  it("loads an existing book without creating another", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_existing");
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        jsonResponse({
          book_id: "bok_existing",
          created_at: "2026-09-22T15:00:00Z",
          totals: { received: null, still_to_pay: null, extra: null, incomplete: false },
          lender_groups: [],
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const book = await getBook("bok_existing");
    expect(book.book_id).toBe("bok_existing");

    await ensureBook();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1]?.[0]).toBe("http://127.0.0.1:8002/api/v1/books/bok_existing");
    expect(fetchMock.mock.calls.every((call) => (call[1] as RequestInit | undefined)?.method !== "POST")).toBe(true);
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_existing");
  });

  it("creates a replacement book when the stored id is not found", async () => {
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_missing");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          {
            error: {
              code: "BOOK_NOT_FOUND",
              message: "Book not found.",
              retryable: false,
              details: {},
            },
          },
          404,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ book_id: "bok_new", created_at: "2026-09-22T15:00:00Z" }, 201));
    vi.stubGlobal("fetch", fetchMock);

    const book = await ensureBook();
    expect(book.book_id).toBe("bok_new");
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_new");
    expect(sessionStorage.getItem(BOOK_ID_STORAGE_KEY)).toBeNull();
  });

  it("surfaces the error envelope message", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "IMAGE_TOO_LARGE",
            message: "Each image must be 10 MiB or smaller.",
            retryable: false,
            details: { filename: "big.jpg" },
          },
        },
        400,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(createBook()).rejects.toMatchObject({
      code: "IMAGE_TOO_LARGE",
      message: "Each image must be 10 MiB or smaller.",
      retryable: false,
      details: { filename: "big.jpg" },
    });
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBeNull();
  });

  it("uploads files as multipart field files and deletes with an empty response", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          {
            analysis_id: "anl_01J",
            status: "images_pending",
            images: [
              {
                image_id: "img_01",
                filename: "one.png",
                content_type: "image/png",
                byte_size: 1,
                status: "uploaded",
              },
            ],
          },
          201,
        ),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["x"], "one.png", { type: "image/png" });
    const uploaded = await uploadImages("anl_01J", [file]);
    expect(uploaded.images[0]?.image_id).toBe("img_01");

    const [uploadUrl, uploadInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(uploadUrl).toBe("http://127.0.0.1:8002/api/v1/analyses/anl_01J/images");
    expect(uploadInit.method).toBe("POST");
    expect(uploadInit.body).toBeInstanceOf(FormData);
    expect((uploadInit.body as FormData).getAll("files")).toHaveLength(1);
    const uploadHeaders = uploadInit.headers as Headers;
    expect(uploadHeaders.get("Content-Type")).toBeNull();

    await expect(deleteImage("anl_01J", "img_01")).resolves.toBeUndefined();
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "http://127.0.0.1:8002/api/v1/analyses/anl_01J/images/img_01",
    );
    expect((fetchMock.mock.calls[1]?.[1] as RequestInit).method).toBe("DELETE");
    expect(localStorage.length).toBe(0);
  });

  it("keeps the in-progress draft id in sessionStorage", () => {
    writeAnalysisId("anl_01J");
    expect(readAnalysisId()).toBe("anl_01J");
    expect(sessionStorage.getItem(ANALYSIS_ID_STORAGE_KEY)).toBe("anl_01J");
    expect(localStorage.getItem(ANALYSIS_ID_STORAGE_KEY)).toBeNull();
    clearAnalysisId();
    expect(readAnalysisId()).toBeNull();
  });
});
