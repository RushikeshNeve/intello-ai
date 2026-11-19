/** User-facing copy for chat states. Server `message` text is deliberately not shown: only the stable code is used. */
import type { ChatErrorCode, Verification } from "./api";

export const ABSTAINED_TEXT = "I couldn't find this in your documents.";
export const PARTIAL_TEXT = "Some statements were removed because they weren't supported by your documents.";

/** Safe wording per run `error_code` (contract §8). Unknown codes get the generic line. */
export function runErrorMessage(code: string | null | undefined): string {
  switch (code) {
    case "model_unavailable":
      return "The AI model isn't reachable right now. If you run it locally, check that LM Studio is running with the chat and embedding models loaded, then retry.";
    case "policy_denied":
      return "Your organization's AI policy doesn't allow a model for this request. Ask an administrator if you think it should.";
    case "search_unavailable":
      return "Search isn't available right now, so the question couldn't be answered. Your documents are safe; retry in a moment.";
    case "timeout":
      return "This took too long and was stopped. Try a narrower question or a smaller scope, then retry.";
    default:
      return "Something went wrong while answering. Your documents are safe; retry in a moment.";
  }
}

export function sendErrorMessage(code: ChatErrorCode): string {
  switch (code) {
    case "rate_limited":
      return "You're sending questions very quickly. Wait a few seconds and try again.";
    case "too_many_runs":
      return "Another answer is still being written. Wait for it to finish, or cancel it, then send again.";
    case "not_found":
      return "This conversation isn't available any more.";
    case "invalid":
      return "That message couldn't be sent. Check its length and try again.";
    case "network":
      return "Your message may not have been sent. Check your connection and retry: it won't be sent twice.";
    case "unavailable":
      return "Chat is temporarily unavailable. Try again in a moment.";
    case "unauthenticated":
      return "Your session has ended. Please sign in again.";
    default:
      return "We couldn't send your message. Try again.";
  }
}

export type VerificationNote = { tone: "abstained" | "partial" | "flagged"; text: string };

export function verificationNote(v: Verification | null): VerificationNote | null {
  if (!v) return null;
  if (v.status === "abstained") return { tone: "abstained", text: ABSTAINED_TEXT };
  if (v.status === "partial") {
    const extra = v.withheld_count > 0 ? ` (${v.withheld_count} removed)` : "";
    return { tone: "partial", text: `${PARTIAL_TEXT}${extra}` };
  }
  if (v.flagged_count > 0) {
    return { tone: "flagged", text: `${v.flagged_count} ${v.flagged_count === 1 ? "statement" : "statements"} could not be fully verified against your documents.` };
  }
  return null;
}
