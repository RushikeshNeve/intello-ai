// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FirewallPanel } from "../firewall-panel";
import { jsonResponse, mount, O } from "./helpers";

afterEach(cleanup);
const policy = (extra = {}) => ({ input_user: "off", input_evidence: "off", output: "off", threshold: 0.5, is_default: true, updated_at: null, updated_by: null, ...extra });
const ORG = O.split("/").pop() as string;

describe("FirewallPanel", () => {
  it("admin saves modes; members see it read-only", async () => {
    const put = vi.fn(() => jsonResponse(200, policy({ input_user: "block", is_default: false })));
    mount(<FirewallPanel orgId={ORG} canEdit />, { [`GET ${O}/firewall-policy`]: () => jsonResponse(200, policy()), [`PUT ${O}/firewall-policy`]: put });
    await userEvent.selectOptions(await screen.findByLabelText("Questions"), "block");
    await userEvent.click(screen.getByTestId("firewall-save"));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    cleanup();
    mount(<FirewallPanel orgId={ORG} canEdit={false} />, { [`GET ${O}/firewall-policy`]: () => jsonResponse(200, policy()) });
    expect(await screen.findByTestId("firewall-readonly")).toBeTruthy();
  });
});
