import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  analyzeDraft,
  ApiClientError,
  createDraft,
  deleteImage,
  ensureBook,
  readAnalysisId,
  uploadImages,
  writeAnalysisId,
  clearAnalysisId,
} from "../api/client";
import { ImageThumbList, type ImageThumb } from "../components/ImageThumbList";

export function UploadScreen() {
  const navigate = useNavigate();
  const [thumbs, setThumbs] = useState<ImageThumb[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [bookReady, setBookReady] = useState(false);
  const alive = useRef(true);
  const thumbsRef = useRef<ImageThumb[]>([]);
  thumbsRef.current = thumbs;

  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    ensureBook()
      .then(() => {
        if (!cancelled) {
          setBookReady(true);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        setErrorMessage(messageFrom(error, "Could not open the book."));
      });
    return () => {
      cancelled = true;
      alive.current = false;
      for (const thumb of thumbsRef.current) {
        revokePreview(thumb.previewUrl);
      }
    };
  }, []);

  function onFilesSelected(event: ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files;
    const files = selected ? Array.from(selected) : [];
    event.target.value = "";
    if (files.length === 0) {
      return;
    }
    void addFiles(files);
  }

  async function addFiles(files: File[]) {
    setBusy(true);
    setErrorMessage(null);
    setStatusMessage("Uploading…");
    try {
      const book = await ensureBook();
      const result = await uploadOntoDraft(book.book_id, files);
      const previews = result.images.map((image, index) => {
        const file = files[index];
        return {
          imageId: image.image_id,
          filename: image.filename || file?.name || "photo",
          previewUrl: file ? URL.createObjectURL(file) : "",
        };
      });
      if (!alive.current) {
        for (const preview of previews) {
          revokePreview(preview.previewUrl);
        }
        return;
      }
      setThumbs((current) => [...current, ...previews]);
    } catch (error: unknown) {
      if (!alive.current) {
        return;
      }
      setErrorMessage(messageFrom(error, "Could not upload the photo."));
    } finally {
      if (alive.current) {
        setBusy(false);
        setStatusMessage(null);
      }
    }
  }

  async function onAnalyze() {
    const analysisId = readAnalysisId();
    if (!analysisId || thumbs.length === 0) {
      return;
    }
    setBusy(true);
    setErrorMessage(null);
    setStatusMessage(null);
    try {
      await analyzeDraft(analysisId);
      clearAnalysisId();
      if (!alive.current) {
        return;
      }
      navigate("/");
    } catch (error: unknown) {
      if (!alive.current) {
        return;
      }
      setErrorMessage(messageFrom(error, "Could not analyze the photos."));
    } finally {
      if (alive.current) {
        setBusy(false);
        setStatusMessage(null);
      }
    }
  }

  async function onRemove(imageId: string) {
    const analysisId = readAnalysisId();
    if (!analysisId) {
      setErrorMessage("Could not remove the photo.");
      return;
    }
    setBusy(true);
    setErrorMessage(null);
    try {
      await deleteImage(analysisId, imageId);
      if (!alive.current) {
        return;
      }
      setThumbs((current) => {
        const removed = current.find((item) => item.imageId === imageId);
        if (removed) {
          revokePreview(removed.previewUrl);
        }
        return current.filter((item) => item.imageId !== imageId);
      });
    } catch (error: unknown) {
      if (!alive.current) {
        return;
      }
      setErrorMessage(messageFrom(error, "Could not remove the photo."));
    } finally {
      if (alive.current) {
        setBusy(false);
      }
    }
  }

  return (
    <section className="screen">
      <h1 className="screen-title">Add photos</h1>
      {errorMessage ? <p role="alert">{errorMessage}</p> : null}
      {statusMessage ? <p role="status">{statusMessage}</p> : null}
      <label className="file-picker">
        Add photos
        <input
          type="file"
          accept="image/*"
          multiple
          disabled={!bookReady || busy}
          onChange={onFilesSelected}
        />
      </label>
      <ImageThumbList images={thumbs} onRemove={onRemove} disabled={busy} />
      <footer className="sticky-footer">
        <button
          className="primary-button"
          type="button"
          disabled={!bookReady || busy || thumbs.length === 0}
          onClick={() => {
            void onAnalyze();
          }}
        >
          Analyze
        </button>
      </footer>
    </section>
  );
}

async function uploadOntoDraft(bookId: string, files: File[]) {
  const existing = readAnalysisId();
  const analysisId = existing ?? (await startDraft(bookId));
  try {
    return await uploadImages(analysisId, files);
  } catch (error) {
    if (!isStaleDraft(error)) {
      throw error;
    }
    clearAnalysisId();
    const fresh = await startDraft(bookId);
    return uploadImages(fresh, files);
  }
}

async function startDraft(bookId: string): Promise<string> {
  const draft = await createDraft(bookId);
  writeAnalysisId(draft.analysis_id);
  return draft.analysis_id;
}

function isStaleDraft(error: unknown): boolean {
  return (
    error instanceof ApiClientError &&
    (error.code === "ANALYSIS_NOT_FOUND" || error.code === "INVALID_STATUS")
  );
}

function revokePreview(url: string): void {
  if (url.startsWith("blob:")) {
    URL.revokeObjectURL(url);
  }
}

function messageFrom(error: unknown, fallback: string): string {
  if (error instanceof ApiClientError && error.message.trim() !== "") {
    return error.message;
  }
  return fallback;
}
