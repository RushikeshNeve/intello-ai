"use client";

import { useId, type FormEvent, type KeyboardEvent } from "react";
import { SendIcon, SquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import type { Workspace } from "@/features/workspaces/api";
import { MAX_MESSAGE_LENGTH } from "./api";

export type ComposerProps = {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  /** A run is active (or a send is in flight): sending is disabled. */
  busy: boolean;
  onCancel?: () => void;
  cancelling?: boolean;
  workspaces: readonly Workspace[];
  /** Workspace id, or null for every workspace the caller may read. */
  scope: string | null;
  onScope: (id: string | null) => void;
};

/** Enter sends, Shift+Enter inserts a newline; the textarea stays editable while an answer is written. */
export function Composer({ value, onChange, onSend, busy, onCancel, cancelling, workspaces, scope, onScope }: ComposerProps) {
  const id = useId();
  const length = value.length;
  const trimmed = value.trim();
  const tooLong = length > MAX_MESSAGE_LENGTH;
  const canSend = !busy && trimmed.length > 0 && !tooLong;

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
    e.preventDefault();
    if (canSend) onSend();
  }
  function submit(e: FormEvent) {
    e.preventDefault();
    if (canSend) onSend();
  }

  return (
    <form onSubmit={submit} className="grid gap-2 rounded-lg border border-border p-3" data-testid="composer">
      <Label htmlFor={`${id}-input`} className="sr-only">
        Your question
      </Label>
      <textarea
        id={`${id}-input`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        rows={3}
        placeholder="Ask about your documents…  (Enter to send, Shift+Enter for a new line)"
        aria-describedby={`${id}-count`}
        aria-invalid={tooLong || undefined}
        className="min-h-20 w-full resize-y rounded-lg border border-input bg-transparent px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
        data-testid="composer-input"
      />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Label htmlFor={`${id}-scope`} className="text-xs text-muted-foreground">
            Search in
          </Label>
          <Select id={`${id}-scope`} value={scope ?? ""} onChange={(e) => onScope(e.target.value || null)} data-testid="chat-scope">
            <option value="">All my workspaces</option>
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex items-center gap-3">
          <span id={`${id}-count`} className={tooLong ? "text-xs text-destructive" : "text-xs text-muted-foreground"} data-testid="char-count">
            {length}/{MAX_MESSAGE_LENGTH}
          </span>
          {busy && onCancel && (
            <Button type="button" variant="outline" onClick={onCancel} disabled={cancelling} data-testid="cancel-run">
              <SquareIcon aria-hidden="true" />
              {cancelling ? "Cancelling…" : "Cancel"}
            </Button>
          )}
          <Button type="submit" disabled={!canSend} data-testid="send">
            <SendIcon aria-hidden="true" />
            Send
          </Button>
        </div>
      </div>
    </form>
  );
}
