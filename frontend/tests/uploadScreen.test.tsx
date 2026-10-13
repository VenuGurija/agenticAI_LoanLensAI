import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ANALYSIS_ID_STORAGE_KEY, BOOK_ID_STORAGE_KEY } from "../src/api/client";
import { UploadScreen } from "../src/screens/UploadScreen";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function bookResponse() {
  return jsonResponse({
    book_id: "bok_01",
    created_at: "2026-09-22T15:00:00Z",
    totals: { received: null, still_to_pay: null, extra: null, incomplete: false },
    lender_groups: [],
  });
}

function draftResponse() {
  return jsonResponse(
    {
      book_id: "bok_01",
      analysis_id: "anl_01",
      status: "created",
      created_at: "2026-09-22T15:00:00Z",
      expires_at: "2026-09-23T15:00:00Z",
    },
    201,
  );
}

function setInputFiles(input: HTMLInputElement, files: File[]) {
  Object.defineProperty(input, "files", {
    configurable: true,
    value: files,
  });
  fireEvent.change(input);
}

describe("UploadScreen", () => {
  let previewCount = 0;

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    localStorage.setItem(BOOK_ID_STORAGE_KEY, "bok_01");
    previewCount = 0;
    if (typeof URL.createObjectURL !== "function") {
      URL.createObjectURL = () => "";
    }
    if (typeof URL.revokeObjectURL !== "function") {
      URL.revokeObjectURL = () => undefined;
    }
    vi.spyOn(URL, "createObjectURL").mockImplementation(() => `blob:preview-${++previewCount}`);
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("renders two thumbnails and enables Analyze", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/drafts")) {
        return Promise.resolve(draftResponse());
      }
      if (String(url).endsWith("/images") && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              analysis_id: "anl_01",
              status: "images_pending",
              images: [
                {
                  image_id: "img_one",
                  filename: "one.png",
                  content_type: "image/png",
                  byte_size: 1,
                  status: "uploaded",
                },
                {
                  image_id: "img_two",
                  filename: "two.png",
                  content_type: "image/png",
                  byte_size: 1,
                  status: "uploaded",
                },
              ],
            },
            201,
          ),
        );
      }
      return Promise.resolve(bookResponse());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <StrictMode>
        <MemoryRouter>
          <UploadScreen />
        </MemoryRouter>
      </StrictMode>,
    );

    const input = await screen.findByLabelText("Add photos");
    expect(input.getAttribute("multiple")).not.toBeNull();
    expect(input.hasAttribute("capture")).toBe(false);
    expect((screen.getByRole("button", { name: "Analyze" }) as HTMLButtonElement).disabled).toBe(true);

    await waitFor(() => {
      expect((input as HTMLInputElement).disabled).toBe(false);
    });

    setInputFiles(input as HTMLInputElement, [
      new File(["a"], "one.png", { type: "image/png" }),
      new File(["b"], "two.png", { type: "image/png" }),
    ]);

    expect(await screen.findByRole("img", { name: "one.png" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "two.png" })).toBeTruthy();
    expect((screen.getByRole("button", { name: "Analyze" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText("Analyze arrives in a later session")).toBeNull();
    expect(sessionStorage.getItem(ANALYSIS_ID_STORAGE_KEY)).toBe("anl_01");
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01");
    expect(JSON.stringify(localStorage)).not.toContain("blob:");
    expect(JSON.stringify(localStorage)).not.toContain("one.png");

    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes("/analyze") || url.includes("vision"))).toBe(false);
    const uploadCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/images"));
    expect(uploadCall?.[1]).toEqual(expect.objectContaining({ method: "POST" }));
    expect((uploadCall?.[1] as RequestInit).body).toBeInstanceOf(FormData);
  });

  it("shows the server error message and does not add thumbnails", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/drafts")) {
        return Promise.resolve(draftResponse());
      }
      if (String(url).endsWith("/images") && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: "IMAGE_TOO_LARGE",
                message: "Each image must be 10 MiB or smaller.",
                retryable: false,
                details: { filename: "big.png" },
              },
            },
            400,
          ),
        );
      }
      return Promise.resolve(bookResponse());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter>
        <UploadScreen />
      </MemoryRouter>,
    );

    const input = await screen.findByLabelText("Add photos");
    await waitFor(() => {
      expect((input as HTMLInputElement).disabled).toBe(false);
    });
    setInputFiles(input as HTMLInputElement, [new File(["a"], "big.png", { type: "image/png" })]);

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Each image must be 10 MiB or smaller.");
    expect(screen.queryByRole("img")).toBeNull();
    expect((screen.getByRole("button", { name: "Analyze" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("removes a thumbnail and revokes its preview", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/drafts")) {
        return Promise.resolve(draftResponse());
      }
      if (String(url).endsWith("/images") && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              analysis_id: "anl_01",
              status: "images_pending",
              images: [
                {
                  image_id: "img_one",
                  filename: "one.png",
                  content_type: "image/png",
                  byte_size: 1,
                  status: "uploaded",
                },
                {
                  image_id: "img_two",
                  filename: "two.png",
                  content_type: "image/png",
                  byte_size: 1,
                  status: "uploaded",
                },
              ],
            },
            201,
          ),
        );
      }
      if (init?.method === "DELETE") {
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      return Promise.resolve(bookResponse());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter>
        <UploadScreen />
      </MemoryRouter>,
    );

    const input = await screen.findByLabelText("Add photos");
    await waitFor(() => {
      expect((input as HTMLInputElement).disabled).toBe(false);
    });
    setInputFiles(input as HTMLInputElement, [
      new File(["a"], "one.png", { type: "image/png" }),
      new File(["b"], "two.png", { type: "image/png" }),
    ]);

    expect(await screen.findByRole("img", { name: "one.png" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove one.png" }));

    await waitFor(() => {
      expect(screen.queryByRole("img", { name: "one.png" })).toBeNull();
    });
    expect(screen.getByRole("img", { name: "two.png" })).toBeTruthy();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:preview-1");
    const deleteCall = fetchMock.mock.calls.find((call) => (call[1] as RequestInit | undefined)?.method === "DELETE");
    expect(deleteCall?.[0]).toBe("http://127.0.0.1:8002/api/v1/analyses/anl_01/images/img_one");
  });

  it("returns home after Analyze accepts the photos and does not keep a spinner", async () => {
    let releaseAnalyze: (response: Response) => void = () => undefined;
    const analyzeGate = new Promise<Response>((resolve) => {
      releaseAnalyze = resolve;
    });
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith("/analyze")) {
        return analyzeGate;
      }
      if (href.endsWith("/drafts")) {
        return Promise.resolve(draftResponse());
      }
      if (href.endsWith("/images") && init?.method === "POST") {
        return Promise.resolve(uploadedImagesResponse());
      }
      return Promise.resolve(bookResponse());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/add"]}>
        <Routes>
          <Route path="/" element={<p>Book home</p>} />
          <Route path="/add" element={<UploadScreen />} />
          <Route path="/a/:analysisId" element={<p>Preview open</p>} />
        </Routes>
      </MemoryRouter>,
    );

    const input = await screen.findByLabelText("Add photos");
    await waitFor(() => {
      expect((input as HTMLInputElement).disabled).toBe(false);
    });
    setInputFiles(input as HTMLInputElement, [new File(["a"], "one.png", { type: "image/png" })]);
    const analyze = await screen.findByRole("button", { name: "Analyze" });
    await waitFor(() => {
      expect((analyze as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(analyze);

    await waitFor(() => {
      expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/analyze"))).toBe(true);
    });
    expect(screen.getByRole("heading", { name: "Add photos" })).toBeTruthy();
    expect(screen.queryByText("Analyzing…")).toBeNull();
    expect(screen.queryByText("Preview open")).toBeNull();

    releaseAnalyze(
      jsonResponse(
        {
          analysis_id: "anl_01",
          status: "queued",
          created_at: "2026-09-22T15:00:00Z",
          completed_at: null,
          expires_at: "2026-09-23T15:00:00Z",
          error: null,
          pages_done: 0,
          pages_total: 1,
          queued_at: "2026-09-22T15:00:01Z",
          images: [],
          lender_groups: [],
        },
        202,
      ),
    );

    expect(await screen.findByText("Book home")).toBeTruthy();
    expect(screen.queryByText("Preview open")).toBeNull();
    expect(screen.queryByText("Analyzing…")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01");
    expect(sessionStorage.getItem(ANALYSIS_ID_STORAGE_KEY)).toBeNull();
    const analyzeCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/analyze"));
    expect(analyzeCall?.[0]).toBe("http://127.0.0.1:8002/api/v1/analyses/anl_01/analyze");
    expect((analyzeCall?.[1] as RequestInit).method).toBe("POST");
    expect((analyzeCall?.[1] as RequestInit).body).toBe("{}");
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes("/accept") || url.includes("/loans/"))).toBe(false);
  });

  it("shows the analyze error and leaves the book in place", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith("/analyze")) {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: "NO_IMAGES",
                message: "At least one image is required.",
                retryable: false,
                details: {},
              },
            },
            400,
          ),
        );
      }
      if (href.endsWith("/drafts")) {
        return Promise.resolve(draftResponse());
      }
      if (href.endsWith("/images") && init?.method === "POST") {
        return Promise.resolve(uploadedImagesResponse());
      }
      return Promise.resolve(bookResponse());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/add"]}>
        <Routes>
          <Route path="/add" element={<UploadScreen />} />
          <Route path="/a/:analysisId" element={<p>Preview open</p>} />
        </Routes>
      </MemoryRouter>,
    );

    const input = await screen.findByLabelText("Add photos");
    await waitFor(() => {
      expect((input as HTMLInputElement).disabled).toBe(false);
    });
    setInputFiles(input as HTMLInputElement, [new File(["a"], "one.png", { type: "image/png" })]);
    const analyze = await screen.findByRole("button", { name: "Analyze" });
    await waitFor(() => {
      expect((analyze as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(analyze);

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "At least one image is required.");
    expect(screen.queryByText("Preview open")).toBeNull();
    expect(screen.getByRole("heading", { name: "Add photos" })).toBeTruthy();
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01");
    expect(sessionStorage.getItem(ANALYSIS_ID_STORAGE_KEY)).toBe("anl_01");
  });

  it("starts a new draft on the same book when the previous draft can no longer take photos", async () => {
    sessionStorage.setItem(ANALYSIS_ID_STORAGE_KEY, "anl_done");
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const href = String(url);
      if (href.endsWith("/anl_done/images") && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              error: {
                code: "INVALID_STATUS",
                message: "This draft can no longer be changed.",
                retryable: false,
                details: {},
              },
            },
            409,
          ),
        );
      }
      if (href.endsWith("/drafts")) {
        return Promise.resolve(
          jsonResponse(
            {
              book_id: "bok_01",
              analysis_id: "anl_new",
              status: "created",
              created_at: "2026-09-22T15:00:00Z",
              expires_at: "2026-09-23T15:00:00Z",
            },
            201,
          ),
        );
      }
      if (href.endsWith("/anl_new/images") && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              analysis_id: "anl_new",
              status: "images_pending",
              images: [
                {
                  image_id: "img_new",
                  filename: "new.png",
                  content_type: "image/png",
                  byte_size: 1,
                  status: "uploaded",
                },
              ],
            },
            201,
          ),
        );
      }
      return Promise.resolve(bookResponse());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter>
        <UploadScreen />
      </MemoryRouter>,
    );

    const input = await screen.findByLabelText("Add photos");
    await waitFor(() => {
      expect((input as HTMLInputElement).disabled).toBe(false);
    });
    setInputFiles(input as HTMLInputElement, [new File(["a"], "new.png", { type: "image/png" })]);

    expect(await screen.findByRole("img", { name: "new.png" })).toBeTruthy();
    expect(sessionStorage.getItem(ANALYSIS_ID_STORAGE_KEY)).toBe("anl_new");
    expect(localStorage.getItem(BOOK_ID_STORAGE_KEY)).toBe("bok_01");
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.includes("/books/bok_01/drafts"))).toBe(true);
    expect(urls.some((url) => url.includes("/loans/"))).toBe(false);
  });
});

function uploadedImagesResponse() {
  return jsonResponse(
    {
      analysis_id: "anl_01",
      status: "images_pending",
      images: [
        {
          image_id: "img_one",
          filename: "one.png",
          content_type: "image/png",
          byte_size: 1,
          status: "uploaded",
        },
      ],
    },
    201,
  );
}
