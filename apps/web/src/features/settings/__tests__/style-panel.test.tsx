// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StylePanel } from "../style-panel";
import { jsonResponse, mount, O, W } from "./helpers";

afterEach(cleanup);
const style = (extra = {}) => ({ source: "default", tone: "neutral", answer_format: "concise", language: null, citation_style: "numeric", custom_instructions: "", updated_at: null, updated_by: null, ...extra });
const ORG = O.split("/").pop() as string;
const WS = W.split("/").pop() as string;

describe("StylePanel", () => {
  it("admin saves a style; marker characters block the save", async () => {
    const put = vi.fn(() => jsonResponse(200, style({ source: "workspace", answer_format: "bullets" })));
    mount(<StylePanel orgId={ORG} workspaceId={WS} canEdit />, { [`GET ${W}/style`]: () => jsonResponse(200, style()), [`PUT ${W}/style`]: put });
    await userEvent.selectOptions(await screen.findByLabelText("Answer format"), "bullets");
    await userEvent.type(screen.getByLabelText("Extra preferences"), "a <<< b");
    expect((screen.getByTestId("style-save") as HTMLButtonElement).disabled).toBe(true);
    await userEvent.clear(screen.getByLabelText("Extra preferences"));
    await userEvent.click(screen.getByTestId("style-save"));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
  });

  it("members see it read-only", async () => {
    mount(<StylePanel orgId={ORG} workspaceId={WS} canEdit={false} />, { [`GET ${W}/style`]: () => jsonResponse(200, style({ tone: "formal" })) });
    expect(await screen.findByTestId("style-readonly")).toBeTruthy();
    expect((screen.getByLabelText("Tone") as HTMLSelectElement).disabled).toBe(true);
  });
});
