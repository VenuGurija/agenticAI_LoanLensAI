import { describe, expect, it } from "vitest";
import { phaseProgress } from "../src/lib/phaseProgress";

describe("phase progress", () => {
  it("maps each processing status to its step of 6", () => {
    expect(phaseProgress("queued")).toBe("1/6 queued");
    expect(phaseProgress("running")).toBe("2/6 preparing photos");
    expect(phaseProgress("preprocessing")).toBe("2/6 preparing photos");
    expect(phaseProgress("classifying")).toBe("3/6 classifying");
    expect(phaseProgress("extracting")).toBe("4/6 extracting");
    expect(phaseProgress("grouping")).toBe("5/6 grouping");
    expect(phaseProgress("validating")).toBe("6/6 checking");
  });

  it("omits the line once the draft is finished or not yet queued", () => {
    expect(phaseProgress("completed")).toBeNull();
    expect(phaseProgress("completed_needs_review")).toBeNull();
    expect(phaseProgress("failed")).toBeNull();
    expect(phaseProgress("accepted")).toBeNull();
    expect(phaseProgress("images_pending")).toBeNull();
  });
});
