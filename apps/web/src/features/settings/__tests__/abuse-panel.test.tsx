// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AbusePanel } from "../abuse-panel";
import { jsonResponse, mount, O } from "./helpers";

afterEach(cleanup);
const ORG = O.split("/").pop() as string;
const SEC = `${O}/security`;
const event = { id: "e1", kind: "canary_hit", severity: "high", user_id: null, created_at: "2026-11-22T10:00:00Z", acknowledged_at: null };
const cfg = { action: "flag", window_seconds: 300 };

describe("AbusePanel", () => {
  it("lists events, acknowledges one and switches the action", async () => {
    const ack = vi.fn(() => jsonResponse(200, { ...event, acknowledged_at: "2026-11-22T11:00:00Z" }));
    const put = vi.fn(() => jsonResponse(200, { ...cfg, action: "throttle" }));
    mount(<AbusePanel orgId={ORG} />, {
      [`GET ${SEC}/events`]: () => jsonResponse(200, { events: [event] }),
      [`GET ${SEC}/abuse-settings`]: () => jsonResponse(200, cfg),
      [`POST ${SEC}/events/e1/acknowledge`]: ack,
      [`PUT ${SEC}/abuse-settings`]: put,
    });
    expect(await screen.findByText("Canary document opened")).toBeTruthy();
    await userEvent.click(screen.getByTestId("ack-e1"));
    await waitFor(() => expect(ack).toHaveBeenCalledTimes(1));
    await userEvent.selectOptions(await screen.findByLabelText("When a limit is crossed"), "throttle");
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
  });

  it("shows a calm error for a forbidden member", async () => {
    mount(<AbusePanel orgId={ORG} />, {
      [`GET ${SEC}/events`]: () => jsonResponse(403, { detail: "forbidden" }),
      [`GET ${SEC}/abuse-settings`]: () => jsonResponse(403, { detail: "forbidden" }),
    });
    expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
  });
});
