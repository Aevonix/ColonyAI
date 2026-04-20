import { describe, expect, it } from "vitest";

import { TurnExtractionPipeline } from "../src/extraction/pipeline.js";

describe("TurnExtractionPipeline.extractPendingTasks", () => {
  const pipeline = new TurnExtractionPipeline();

  it("captures imperative-future assistant commitments", () => {
    const result = pipeline.extract(
      "please follow up with Bob",
      "Sure. I'll email Bob tomorrow and I will file the expense report afterward.",
    );
    expect(result.pending_tasks).toContain("email Bob tomorrow");
    expect(
      result.pending_tasks.some((t) => t.includes("file the expense report")),
    ).toBe(true);
  });

  it("captures explicit TODO markers", () => {
    const result = pipeline.extract(
      "anything else?",
      "Done. TODO: retry the build on the release branch",
    );
    expect(result.pending_tasks).toContain(
      "retry the build on the release branch",
    );
  });

  it("captures unchecked markdown checkboxes", () => {
    const result = pipeline.extract(
      "plan?",
      "Plan:\n- [ ] review PR 42\n- [ ] send status update\n- [x] already done",
    );
    expect(result.pending_tasks).toEqual(
      expect.arrayContaining(["review PR 42", "send status update"]),
    );
    expect(result.pending_tasks).not.toContain("already done");
  });

  it("deduplicates tasks case-insensitively and caps at 5", () => {
    const text = Array.from({ length: 8 }, (_, i) => `I'll step ${i + 1}.`).join(" ");
    const result = pipeline.extract("", text);
    expect(result.pending_tasks.length).toBeLessThanOrEqual(5);
  });

  it("returns an empty array when no commitments are present", () => {
    const result = pipeline.extract("hi", "Hello! How can I help?");
    expect(result.pending_tasks).toEqual([]);
  });
});
