// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EvalsApp } from "../evals-app";
import { parseIds, parseRun } from "../api";
import { jsonResponse, mount, orgWith, W, WS_ADMIN } from "@/features/settings/__tests__/helpers";

afterEach(cleanup);

const SET = { id: "s1", name: "Core", version: 2, case_count: 1, updated_at: "2026-11-21T10:00:00Z" };
const CASE = { id: "c1", question: "Where is the kestrel guide?", expected_document_ids: ["d1"], expected_chunk_ids: [], should_abstain: false, notes: null };
const RUN = {
  id: "r1",
  set_id: "s1",
  set_version: 2,
  state: "completed",
  started_at: "2026-11-21T10:00:00Z",
  summary: { total: 1, errors: 0, top_k: 5, hit_at_k: 1, mrr: 1, abstention_accuracy: null, pass_rate: 1 },
  results: [{ id: "x1", case_id: "c1", question: CASE.question, retrieved: [], answer_status: "answered", metrics: { hit: true, rank: 1, should_abstain: false, abstain_correct: true, passed: true } }],
};
const E = `${W}/evals`;

describe("evals api", () => {
  it("parses a run and rejects a malformed one", () => {
    expect(parseRun(RUN).summary.hit_at_k).toBe(1);
    expect(() => parseRun({ id: "r" })).toThrow();
    expect(parseIds("3f2b1c0e-0000-4000-8000-000000000001, nope")).toHaveLength(1);
  });
});

describe("EvalsApp", () => {
  it("lists sets, runs one and shows the metrics table", async () => {
    mount(<EvalsApp org={orgWith("owner")} workspace={WS_ADMIN} />, {
      [`GET ${E}/sets`]: () => jsonResponse(200, { items: [SET] }),
      [`GET ${E}/sets/s1/cases`]: () => jsonResponse(200, { items: [CASE] }),
      [`GET ${E}/sets/s1/runs`]: () => jsonResponse(200, { items: [] }),
      [`POST ${E}/sets/s1/runs`]: () => jsonResponse(201, RUN),
    });
    await userEvent.click(await screen.findByRole("button", { name: /core \(1\)/i }));
    expect(await screen.findByText("Where is the kestrel guide?")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Run set" }));
    expect(await screen.findByTestId("evals-run")).toBeTruthy();
    expect(screen.getByTestId("evals-run").textContent).toMatch(/Hit@5.*100%/);
    expect(screen.getByText("Pass")).toBeTruthy();
  });

  it("shows an error state when the sets cannot be loaded (403 for non-admins)", async () => {
    mount(<EvalsApp org={orgWith("member")} workspace={WS_ADMIN} />, { [`GET ${E}/sets`]: () => jsonResponse(403, { detail: "forbidden" }) });
    await waitFor(() => expect(screen.getByTestId("evals-error")).toBeTruthy());
  });
});
