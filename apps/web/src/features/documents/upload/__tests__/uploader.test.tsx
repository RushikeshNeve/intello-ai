// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DocumentUploader } from "../document-uploader";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { FakeXhrs, fakeHash, file, FILE_RESULT, instantSleep, session, type XhrBehavior } from "./fakes";
import { errorMessage } from "../messages";
import type { UploadErrorCode } from "../api";

afterEach(cleanup);

const WS = "/v1/orgs/o1/workspaces/w1";

type Route = () => Response;
function mount(o: { xhr?: XhrBehavior[]; create?: Route; finalize?: Route; props?: Partial<React.ComponentProps<typeof DocumentUploader>> } = {}) {
  const xhrs = new FakeXhrs(o.xhr ?? ["ok"]);
  let n = 0;
  const { fetcher, calls } = fakeFetcher({
    [`POST ${WS}/uploads`]: o.create ?? (() => jsonResponse(201, session(++n))),
    [`POST ${WS}/uploads/up-1/finalize`]: o.finalize ?? (() => jsonResponse(201, FILE_RESULT())),
    [`POST ${WS}/uploads/up-2/finalize`]: o.finalize ?? (() => jsonResponse(201, FILE_RESULT())),
  });
  const onUploaded = vi.fn();
  const utils = renderWithQuery(
    <DocumentUploader
      orgId="o1"
      workspaceId="w1"
      onUploaded={onUploaded}
      engineOptions={{ hash: fakeHash, xhrFactory: xhrs.factory, sleep: instantSleep }}
      {...o.props}
    />,
    { fetcher },
  );
  const input = () => screen.getByTestId("upload-input") as HTMLInputElement;
  return { ...utils, xhrs, calls, onUploaded, input };
}

describe("DocumentUploader", () => {
  it("uploads a file chosen with the picker and reports the result", async () => {
    const m = mount();
    await userEvent.upload(m.input(), file("notes.txt"));
    await waitFor(() => expect(screen.getByTestId("status-notes.txt").textContent).toContain("Uploaded"));
    expect(m.onUploaded).toHaveBeenCalledWith(FILE_RESULT());
    expect(m.xhrs.instances[0].url).toBe(session(1).upload.url);
    // bytes only went to the store, never to the BFF
    expect(m.calls.every((c) => c.path.startsWith(WS))).toBe(true);
  });

  it("has a keyboard-operable 'Choose files' button that opens the picker", async () => {
    const m = mount();
    const click = vi.spyOn(m.input(), "click");
    const btn = screen.getByRole("button", { name: "Choose files" });
    btn.focus();
    await userEvent.keyboard("{Enter}");
    expect(click).toHaveBeenCalled();
  });

  it("accepts dropped files (multiple)", async () => {
    const m = mount();
    const dt = { files: [file("a.txt"), file("b.txt")], items: [] as unknown[] };
    fireEvent.drop(screen.getByTestId("upload-dropzone"), { dataTransfer: dt });
    await waitFor(() => expect(screen.getByTestId("status-a.txt").textContent).toContain("Uploaded"));
    await waitFor(() => expect(screen.getByTestId("status-b.txt").textContent).toContain("Uploaded"));
    expect(m.onUploaded).toHaveBeenCalledTimes(2);
  });

  it("ignores dropped folders with a message, still takes the files beside them", async () => {
    const m = mount({ xhr: ["hang"] });
    const item = (f: File, dir: boolean) => ({ kind: "file", getAsFile: () => f, webkitGetAsEntry: () => ({ isDirectory: dir }) });
    const dt = { files: [], items: [item(file("dir"), true), item(file("ok.txt"), false)] };
    fireEvent.drop(screen.getByTestId("upload-dropzone"), { dataTransfer: dt });
    expect(await screen.findByText("Folders can't be uploaded. Drop files instead.")).not.toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("ok.txt")).not.toBeNull();
    expect(m.xhrs).toBeDefined();
  });

  it("shows progress with an accessible progressbar and updates the aria-live region on state changes", async () => {
    mount({ xhr: ["hang"] });
    fireEvent.drop(screen.getByTestId("upload-dropzone"), { dataTransfer: { files: [file("p.txt")], items: [] } });
    await waitFor(() => expect(screen.getByRole("progressbar", { name: /p\.txt/ })).not.toBeNull());
    const live = screen.getByTestId("upload-live");
    expect(live.getAttribute("aria-live")).toBe("polite");
    expect(live.textContent).toContain("p.txt: Uploading");
  });

  it("announces completion in the live region", async () => {
    mount();
    await userEvent.upload(screen.getByTestId("upload-input"), file("done.txt"));
    await waitFor(() => expect(screen.getByTestId("upload-live").textContent).toContain("done.txt: Uploaded"));
  });

  it("shows Retry after a failure and a retry succeeds", async () => {
    let first = true;
    const m = mount({
      create: () => {
        if (first) {
          first = false;
          return jsonResponse(409, { detail: "quota_exceeded" });
        }
        return jsonResponse(201, session(1));
      },
    });
    await userEvent.upload(m.input(), file("r.txt"));
    await waitFor(() => expect(screen.getByTestId("status-r.txt").textContent).toContain("Your organization's storage is full."));
    expect(screen.getByTestId("upload-live").textContent).toContain("Your organization's storage is full.");
    await userEvent.click(screen.getByRole("button", { name: "Retry r.txt" }));
    await waitFor(() => expect(screen.getByTestId("status-r.txt").textContent).toContain("Uploaded"));
  });

  it("Cancel aborts an upload and Remove clears the row", async () => {
    const m = mount({ xhr: ["hang"] });
    await userEvent.upload(m.input(), file("c.txt"));
    await userEvent.click(await screen.findByRole("button", { name: "Cancel c.txt" }));
    await waitFor(() => expect(screen.getByTestId("status-c.txt").textContent).toContain("Cancelled"));
    expect(m.xhrs.instances[0].aborted).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Remove c.txt" }));
    expect(screen.queryByText("c.txt")).toBeNull();
  });

  it("ignores a double drop of the same File object", async () => {
    mount({ xhr: ["hang"] });
    const f = file("twice.txt");
    const dz = screen.getByTestId("upload-dropzone");
    fireEvent.drop(dz, { dataTransfer: { files: [f], items: [] } });
    fireEvent.drop(dz, { dataTransfer: { files: [f], items: [] } });
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });

  it("is inert when disabled: no picker, drops ignored, reason shown", async () => {
    const m = mount({ props: { disabled: true } });
    expect((screen.getByRole("button", { name: "Choose files" }) as HTMLButtonElement).disabled).toBe(true);
    expect((m.input() as HTMLButtonElement).disabled).toBe(true);
    fireEvent.drop(screen.getByTestId("upload-dropzone"), { dataTransfer: { files: [file("x.txt")], items: [] } });
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
    expect(screen.getAllByText("You don't have permission to upload here.").length).toBeGreaterThan(0);
  });

  it("uploads a new version when documentId is given (document_id in the request)", async () => {
    const m = mount({ props: { documentId: "doc-7" } });
    await userEvent.upload(m.input(), file("v2.txt"));
    await waitFor(() => expect(screen.getByTestId("status-v2.txt").textContent).toContain("Uploaded"));
    expect(JSON.parse(m.calls[0].body!)).toMatchObject({ document_id: "doc-7" });
  });

  it("hides Retry for terminal errors that retrying cannot fix", async () => {
    const m = mount({ create: () => jsonResponse(413, { detail: "file_too_large" }) });
    await userEvent.upload(m.input(), file("huge.txt"));
    await waitFor(() => expect(screen.getByTestId("status-huge.txt").textContent).toContain("This file is larger than 100 MB."));
    expect(screen.queryByRole("button", { name: /Retry/ })).toBeNull();
  });

  it("refuses an oversize file client-side using the maxBytes prop", async () => {
    const m = mount({ props: { maxBytes: 1024 * 1024 } });
    await userEvent.upload(m.input(), new File([new Uint8Array(2 * 1024 * 1024)], "two.bin"));
    await waitFor(() => expect(screen.getByTestId("status-two.bin").textContent).toContain("This file is larger than 1 MB."));
    expect(m.calls).toHaveLength(0);
  });
});

describe("error copy", () => {
  const cases: [UploadErrorCode, RegExp][] = [
    ["file_too_large", /larger than 100 MB/],
    ["quota_exceeded", /storage is full/],
    ["too_many_uploads", /Too many uploads/],
    ["upload_expired", /timed out, try again/],
    ["object_missing", /didn't arrive intact/],
    ["size_mismatch", /didn't arrive intact/],
    ["checksum_mismatch", /didn't arrive intact/],
    ["storage_unavailable", /temporarily unavailable/],
    ["forbidden", /permission/],
    ["not_found", /no longer available/],
    ["invalid_request", /can't be uploaded/],
    ["network_error", /interrupted/],
    ["upload_rejected", /interrupted/],
    ["unauthenticated", /Sign in again/],
    ["unknown", /Something went wrong/],
  ];
  it.each(cases)("%s has calm copy", (code, re) => {
    expect(errorMessage(code, 104857600)).toMatch(re);
  });
});
