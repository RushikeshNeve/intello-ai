// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeFetcher, jsonResponse, renderWithQuery } from "@/features/organizations/__tests__/helpers";
import { makeQueryClient } from "@/lib/query/provider";
import { nav } from "@/features/documents/__tests__/helpers";
import type { Workspace } from "@/features/workspaces/api";
import { Conversation } from "../conversation";
import { chatKeys } from "../keys";
import { END, cit, detail, frame, msg, scripted, type Conn } from "./fixtures";

vi.mock("next/navigation", async () => (await import("@/features/documents/__tests__/helpers")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const CHAT = "/o/acme/chat";
const T = "/v1/orgs/o1/chat/threads/t-1";
const workspaces: Workspace[] = [
  { id: "w1", org_id: "o1", slug: "notes", name: "Acme Notes", created_at: "", role: "admin" },
  { id: "w2", org_id: "o1", slug: "papers", name: "Acme Papers", created_at: "", role: "admin" },
];

beforeEach(() => nav.reset(`${CHAT}/t-1`));
afterEach(cleanup);

type Routes = Parameters<typeof fakeFetcher>[0];

function mount(o: { threadId?: string | null; routes?: Routes; conns?: Conn[]; thread?: ReturnType<typeof detail> } = {}) {
  const threadId = o.threadId === undefined ? "t-1" : o.threadId;
  const api = fakeFetcher({
    ...(threadId ? { [`GET ${T}`]: () => jsonResponse(200, o.thread ?? detail()) } : {}),
    "GET /v1/orgs/o1/chat/threads": () => jsonResponse(200, { items: [], next_cursor: null }),
    ...o.routes,
  });
  const { stream, calls } = scripted(o.conns ?? []);
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: 30_000 }, mutations: { retry: false } });
  const utils = renderWithQuery(<Conversation orgId="o1" orgSlug="acme" workspaces={workspaces} threadId={threadId} stream={stream} />, { fetcher: api.fetcher, client });
  return { ...utils, api, calls, client };
}

const accepted = (extra: Record<string, unknown> = {}) => jsonResponse(202, { run_id: "r-1", message_id: "m-1", state: "queued", ...extra });

describe("Conversation: loading and history", () => {
  it("renders history with citation chips that link to exactly that chunk and version", async () => {
    mount({
      thread: detail({
        messages: [
          msg({ id: "m1", role: "user", content: "What grew?" }),
          msg({
            id: "m2",
            role: "assistant",
            content: "Revenue grew by 10% [1].\n\n- Also costs [2]",
            citations: [cit(1, { chunk_id: "c-9", version_id: "v-old", document_id: "d-5" }), cit(2)],
            verification: { status: "grounded", withheld_count: 0, flagged_count: 0 },
            run_id: "r-0",
          }),
        ],
      }),
    });
    await screen.findByTestId("assistant-message");
    const chips = screen.getAllByTestId("citation-chip");
    expect(chips).toHaveLength(2);
    expect(chips[0].getAttribute("href")).toBe("/o/acme/w/notes/documents/d-5/evidence?v=v-old&chunk=c-9&page=3");
    expect(chips[0].textContent).toBe("[1]");
    expect(chips[0].getAttribute("aria-label")).toContain("Source 1");
    expect(screen.getAllByTestId("source-item")).toHaveLength(2);
    expect(screen.getByTestId("conversation-title").textContent).toBe("Quarterly questions");
  });

  it("never renders model output as HTML", async () => {
    mount({
      thread: detail({
        messages: [msg({ id: "m2", role: "assistant", content: '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script> `<b>code</b>`', run_id: "r-0" })],
      }),
    });
    const body = await screen.findByTestId("answer-body");
    expect(body.querySelector("img, script, b")).toBeNull();
    expect(body.textContent).toContain('<img src=x onerror="window.__pwned=1">');
    expect(body.querySelector("code")?.textContent).toBe("<b>code</b>");
  });

  it("an unknown marker stays plain text, not a chip", async () => {
    mount({ thread: detail({ messages: [msg({ id: "m2", role: "assistant", content: "Claim [3].", citations: [cit(1)], run_id: "r-0" })] }) });
    await screen.findByTestId("answer-body");
    expect(screen.queryAllByTestId("citation-chip")).toHaveLength(0);
  });

  it("shows the abstained and partial banners plainly", async () => {
    mount({
      thread: detail({
        messages: [
          msg({ id: "m2", role: "assistant", content: "Nothing found.", verification: { status: "abstained", withheld_count: 0, flagged_count: 0 }, run_id: "r-0" }),
          msg({ id: "m3", role: "assistant", content: "Part [1].", citations: [cit(1)], verification: { status: "partial", withheld_count: 2, flagged_count: 0 }, run_id: "r-1" }),
        ],
      }),
    });
    const banners = await screen.findAllByTestId("verification-banner");
    expect(banners[0].textContent).toBe("I couldn't find this in your documents.");
    expect(banners[1].textContent).toContain("Some statements were removed because they weren't supported");
  });

  it("a thread that is not yours is a calm 404 state", async () => {
    mount({ routes: { [`GET ${T}`]: () => jsonResponse(404, { detail: "not_found" }) } });
    const err = await screen.findByTestId("conversation-error");
    expect(err.getAttribute("data-code")).toBe("not_found");
    expect(err.textContent).toContain("isn't available");
    expect(within(err).getByRole("link").getAttribute("href")).toBe(CHAT);
  });

  it("a 503 offers a retry", async () => {
    let n = 0;
    mount({ routes: { [`GET ${T}`]: () => (++n === 1 ? jsonResponse(503, { detail: "x" }) : jsonResponse(200, detail())) } });
    await screen.findByTestId("conversation-error");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByTestId("conversation-title");
  });

  it("resumes an active run after a reload", async () => {
    const { calls } = mount({
      thread: detail({ messages: [msg({ id: "m1", role: "user", content: "Q", run_id: "r-7" })], active_run: { run_id: "r-7", state: "running" } }),
      conns: [{ frames: [frame(1, "started"), frame(2, "delta", { text: "Partial answer" })], then: "hang" }],
    });
    await waitFor(() => expect(screen.getByTestId("live-answer").textContent).toContain("Partial answer"));
    expect(calls[0].url).toBe("/api/v1/orgs/o1/chat/runs/r-7/events");
    expect(screen.getByTestId("send").hasAttribute("disabled")).toBe(true);
    expect(screen.getByTestId("cancel-run")).toBeTruthy();
  });
});

describe("Conversation: composer", () => {
  it("Enter sends, Shift+Enter inserts a newline, and the button is disabled while a run is active", async () => {
    const posts: Record<string, unknown>[] = [];
    mount({
      routes: {
        [`POST ${T}/runs`]: (init) => {
          posts.push(JSON.parse(String(init?.body)));
          return accepted();
        },
      },
      conns: [{ frames: [frame(1, "started")], then: "hang" }],
    });
    const input = (await screen.findByTestId("composer-input")) as HTMLTextAreaElement;
    expect(screen.getByTestId("send").hasAttribute("disabled")).toBe(true); // empty
    await userEvent.type(input, "line one{Shift>}{Enter}{/Shift}line two");
    expect(input.value).toBe("line one\nline two");
    expect(screen.getByTestId("char-count").textContent).toBe("17/4000");
    await userEvent.type(input, "{Enter}");
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].content).toBe("line one\nline two");
    expect(typeof posts[0].idempotency_key).toBe("string");
    expect((posts[0].idempotency_key as string).length).toBeGreaterThanOrEqual(8);
    expect(posts[0].workspace_ids).toBeUndefined();
    await waitFor(() => expect(screen.getByTestId("send").hasAttribute("disabled")).toBe(true));
    expect(input.value).toBe(""); // cleared once accepted
    await screen.findByTestId("live-answer");
    // typing more during the run does not send
    await userEvent.type(input, "next{Enter}");
    expect(posts).toHaveLength(1);
  });

  it("disables sending over 4000 characters", async () => {
    mount();
    const input = (await screen.findByTestId("composer-input")) as HTMLTextAreaElement;
    await userEvent.click(input);
    await userEvent.paste("x".repeat(4001));
    expect(screen.getByTestId("send").hasAttribute("disabled")).toBe(true);
    expect(screen.getByTestId("char-count").textContent).toBe("4001/4000");
  });

  it("narrows the scope: the chosen workspace is sent as workspace_ids", async () => {
    const posts: Record<string, unknown>[] = [];
    mount({
      routes: {
        [`POST ${T}/runs`]: (init) => {
          posts.push(JSON.parse(String(init?.body)));
          return accepted();
        },
      },
      conns: [{ frames: [], then: "hang" }],
    });
    await screen.findByTestId("composer-input");
    await userEvent.selectOptions(screen.getByTestId("chat-scope"), "w2");
    await userEvent.type(screen.getByTestId("composer-input"), "hello{Enter}");
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].workspace_ids).toEqual(["w2"]);
  });

  it("a network error keeps the draft and Retry reuses the SAME idempotency key", async () => {
    const keys: string[] = [];
    let attempt = 0;
    mount({
      routes: {
        [`POST ${T}/runs`]: (init) => {
          keys.push(JSON.parse(String(init?.body)).idempotency_key);
          if (++attempt === 1) throw new TypeError("fetch failed");
          return accepted();
        },
      },
      conns: [{ frames: [], then: "hang" }],
    });
    await screen.findByTestId("composer-input");
    await userEvent.type(screen.getByTestId("composer-input"), "question{Enter}");
    const err = await screen.findByTestId("send-error");
    expect(err.textContent).toContain("won't be sent twice");
    expect((screen.getByTestId("composer-input") as HTMLTextAreaElement).value).toBe("question");
    await userEvent.click(screen.getByTestId("resend"));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[1]).toBe(keys[0]);
    await waitFor(() => expect(screen.queryByTestId("send-error")).toBeNull());
  });

  it("429 and 409 show calm messages and do not retry with the same key", async () => {
    mount({
      routes: { [`POST ${T}/runs`]: () => jsonResponse(429, { detail: "rate_limited" }) },
    });
    await screen.findByTestId("composer-input");
    await userEvent.type(screen.getByTestId("composer-input"), "q{Enter}");
    expect((await screen.findByTestId("send-error")).textContent).toContain("very quickly");
    expect(screen.queryByTestId("resend")).toBeNull();
  });
});

describe("Conversation: a run end to end", () => {
  it("streams, replaces with answer_text, shows chips and the partial banner, then the persisted message takes over", async () => {
    let refetched = false;
    const persisted = detail({
      messages: [
        msg({ id: "m-1", role: "user", content: "What grew?", run_id: "r-1" }),
        msg({ id: "m-2", role: "assistant", content: "Revenue grew [1].", citations: [cit(1)], verification: { status: "partial", withheld_count: 1, flagged_count: 0 }, run_id: "r-1" }),
      ],
    });
    mount({
      routes: {
        [`GET ${T}`]: () => {
          refetched = true;
          return jsonResponse(200, persisted);
        },
        [`POST ${T}/runs`]: () => accepted(),
      },
      thread: detail(),
      conns: [
        {
          frames: [
            frame(1, "started"),
            frame(2, "delta", { text: "Revenue grew [1]. Moon is cheese." }),
            frame(3, "citations", { citations: [cit(1)] }),
            frame(4, "verification", { status: "partial", withheld_count: 1, flagged_count: 0 }),
            frame(5, "completed", { message_id: "m-2", answer_text: "Revenue grew [1]." }),
            END(),
          ],
        },
      ],
    });
    await screen.findByTestId("composer-input");
    await userEvent.type(screen.getByTestId("composer-input"), "What grew?{Enter}");
    await waitFor(() => expect(refetched).toBe(true));
    await waitFor(() => expect(screen.getByTestId("assistant-message")).toBeTruthy());
    expect(screen.queryByTestId("live-answer")).toBeNull(); // no duplicate bubble
    expect(screen.getAllByTestId("assistant-message")).toHaveLength(1);
    expect(screen.getByTestId("answer-body").textContent).toBe("Revenue grew [1].");
    expect(screen.getByTestId("verification-banner").getAttribute("data-tone")).toBe("partial");
    expect(screen.getByTestId("citation-chip").getAttribute("data-chunk-id")).toBe("c-1");
    expect(screen.getByTestId("send").hasAttribute("disabled")).toBe(true); // empty draft, not running
  });

  it("a failed run shows the safe message for its code; Retry sends the question again with a NEW key", async () => {
    const keys: string[] = [];
    mount({
      thread: detail({ messages: [msg({ id: "m1", role: "user", content: "Q?" })] }),
      routes: {
        [`POST ${T}/runs`]: (init) => {
          keys.push(JSON.parse(String(init?.body)).idempotency_key);
          return accepted({ run_id: `r-${keys.length}`, message_id: `m-${keys.length}` });
        },
      },
      conns: [
        { frames: [frame(1, "started"), frame(2, "failed", { error_code: "model_unavailable", message: "raw upstream text" }), END()] },
        { frames: [frame(1, "started")], then: "hang" },
      ],
    });
    await screen.findByTestId("composer-input");
    await userEvent.type(screen.getByTestId("composer-input"), "Q?{Enter}");
    const failed = await screen.findByTestId("run-failed");
    expect(failed.getAttribute("data-code")).toBe("model_unavailable");
    expect(failed.textContent).toContain("LM Studio");
    expect(failed.textContent).not.toContain("raw upstream text");
    await userEvent.click(screen.getByTestId("retry-run"));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("cancel posts to the cancel endpoint and shows the cancelled state", async () => {
    let cancelPosts = 0;
    mount({
      thread: detail({ messages: [msg({ id: "m1", role: "user", content: "Q", run_id: "r-7" })], active_run: { run_id: "r-7", state: "running" } }),
      routes: {
        "POST /v1/orgs/o1/chat/runs/r-7/cancel": () => {
          cancelPosts++;
          return jsonResponse(202, { state: "cancelled" });
        },
      },
      conns: [{ frames: [frame(1, "started"), frame(2, "delta", { text: "abc" })], then: "hang" }],
    });
    await screen.findByTestId("live-answer");
    await userEvent.click(screen.getByTestId("cancel-run"));
    await waitFor(() => expect(cancelPosts).toBe(1));
    expect(screen.getByTestId("cancel-run").textContent).toContain("Cancelling");
  });

  it("shows a reconnecting note and does not duplicate text across the reconnect", async () => {
    mount({
      thread: detail({ messages: [msg({ id: "m1", role: "user", content: "Q", run_id: "r-7" })], active_run: { run_id: "r-7", state: "running" } }),
      conns: [
        { frames: [frame(1, "started"), frame(2, "delta", { text: "Hello" })], then: "error" },
        { frames: [frame(2, "delta", { text: "Hello" }), frame(3, "delta", { text: " world" })], then: "hang" },
      ],
    });
    await waitFor(() => expect(screen.getByTestId("live-answer").textContent).toContain("Hello world"), { timeout: 4000 }); // default 1 s back-off
    expect(screen.getByTestId("live-answer").textContent).not.toContain("HelloHello");
  });
});

describe("Conversation: a new chat", () => {
  it("creates the thread, starts the run, seeds the cache and moves to the thread's URL", async () => {
    const created: Record<string, unknown>[] = [];
    const { client } = mount({
      threadId: null,
      routes: {
        "POST /v1/orgs/o1/chat/threads": (init) => {
          created.push(JSON.parse(String(init?.body)));
          return jsonResponse(201, { id: "t-new", workspace_id: "w1", title: "", created_at: "a", updated_at: "a" });
        },
        "POST /v1/orgs/o1/chat/threads/t-new/runs": () => accepted({ run_id: "r-n", message_id: "m-n" }),
      },
    });
    await screen.findByTestId("chat-empty");
    await userEvent.selectOptions(screen.getByTestId("chat-scope"), "w1");
    await userEvent.type(screen.getByTestId("composer-input"), "First question{Enter}");
    await waitFor(() => expect(nav.pushes).toEqual([`${CHAT}/t-new`]));
    expect(created).toEqual([{ workspace_id: "w1" }]);
    const seeded = client.getQueryData<ReturnType<typeof detail>>(chatKeys.thread("o1", "t-new"));
    expect(seeded?.active_run).toEqual({ run_id: "r-n", state: "queued" });
    expect(seeded?.messages[0]).toMatchObject({ role: "user", content: "First question", id: "m-n" });
  });
});
