"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useApiFetcher } from "@/lib/query/provider";
import { isRecord, jsonBody, requestJson, type ApiPath } from "@/features/organizations/api/http";

export const FEEDBACK_REASONS = [
  ["wrong_answer", "Wrong answer"],
  ["wrong_citation", "Wrong citation"],
  ["missing_info", "Missing information"],
  ["unsafe", "Unsafe"],
  ["other", "Other"],
] as const;
export type FeedbackReason = (typeof FEEDBACK_REASONS)[number][0];
export type Rating = "up" | "down";

const feedbackPath = (orgId: string, messageId: string) =>
  `/v1/orgs/${encodeURIComponent(orgId)}/chat/messages/${encodeURIComponent(messageId)}/feedback` as ApiPath;

/** PUT replaces the caller's vote for this message (one per user and message). Reason codes only: no free text. */
export function useSendFeedback(orgId: string, messageId: string) {
  const fetcher = useApiFetcher();
  return useMutation({
    mutationFn: (v: { rating: Rating; reason?: FeedbackReason }) =>
      requestJson(fetcher, feedbackPath(orgId, messageId), (b) => isRecord(b), {
        ...jsonBody(v),
        method: "PUT",
      }),
  });
}

/** Thumbs up/down on an assistant message; a thumbs-down asks for a reason code. */
export function FeedbackButtons({ orgId, messageId }: { orgId: string; messageId: string }) {
  const send = useSendFeedback(orgId, messageId);
  const [vote, setVote] = useState<Rating | null>(null);
  const [askReason, setAskReason] = useState(false);

  function submit(rating: Rating, reason?: FeedbackReason) {
    send.mutate({ rating, ...(reason ? { reason } : {}) }, { onSuccess: () => setVote(rating) });
  }

  return (
    <div className="flex flex-wrap items-center gap-1" data-testid="feedback">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label="Good answer"
        aria-pressed={vote === "up"}
        disabled={send.isPending}
        onClick={() => {
          setAskReason(false);
          submit("up");
        }}
      >
        👍
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label="Bad answer"
        aria-pressed={vote === "down"}
        disabled={send.isPending}
        onClick={() => {
          setAskReason(true);
          submit("down");
        }}
      >
        👎
      </Button>
      {askReason && (
        <select
          aria-label="What was wrong?"
          className="rounded-control border border-border bg-transparent px-2 py-1 text-xs"
          defaultValue=""
          onChange={(e) => e.target.value && submit("down", e.target.value as FeedbackReason)}
        >
          <option value="">What was wrong?</option>
          {FEEDBACK_REASONS.map(([code, label]) => (
            <option key={code} value={code}>
              {label}
            </option>
          ))}
        </select>
      )}
      {send.isError && <span className="text-xs text-red-600">Could not save feedback.</span>}
      {vote && !send.isError && <span className="text-xs text-muted-foreground">Thanks for the feedback.</span>}
    </div>
  );
}
