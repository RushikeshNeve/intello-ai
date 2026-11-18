"use client";

import { useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toDocumentErrorCode } from "../api";

export function mutationErrorMessage(err: unknown): string {
  switch (toDocumentErrorCode(err)) {
    case "forbidden":
      return "You don't have permission to do that.";
    case "not_found":
      return "This document is no longer available.";
    case "network_error":
    case "storage_unavailable":
      return "We couldn't reach the server. Try again in a moment.";
    default:
      return "Something went wrong. Try again.";
  }
}

export const MAX_TITLE_LENGTH = 255;

type RenameProps = {
  title: string;
  onClose: () => void;
  /** Resolves when the rename has been applied; a rejection is shown in the dialog. */
  onRename: (title: string) => Promise<void>;
};

/** Mounted only while open (so its state resets each time). */
export function RenameDialog({ title, onClose, onRename }: RenameProps) {
  const [value, setValue] = useState(title);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const trimmed = value.trim();
  const invalid = trimmed.length === 0 || trimmed.length > MAX_TITLE_LENGTH;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (invalid || pending) return;
    if (trimmed === title) {
      onClose();
      return;
    }
    setPending(true);
    setError("");
    try {
      await onRename(trimmed);
    } catch (err) {
      setError(mutationErrorMessage(err));
      setPending(false);
    }
  }

  return (
    <Dialog open onClose={onClose} title="Rename document" initialFocusRef={inputRef}>
      <form onSubmit={submit} className="grid gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="rename-title">Title</Label>
          <Input
            id="rename-title"
            ref={inputRef}
            value={value}
            maxLength={MAX_TITLE_LENGTH}
            onChange={(e) => setValue(e.target.value)}
            aria-invalid={invalid || undefined}
            aria-describedby={error ? "rename-error" : undefined}
            onFocus={(e) => e.currentTarget.select()}
          />
        </div>
        <p id="rename-error" role="alert" className="min-h-5 text-sm text-destructive">
          {error}
        </p>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={invalid || pending}>
            {pending ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

type DeleteProps = {
  title: string;
  onClose: () => void;
  onDelete: () => Promise<void>;
};

export function DeleteDialog({ title, onClose, onDelete }: DeleteProps) {
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);

  async function confirm() {
    if (pending) return;
    setPending(true);
    setError("");
    try {
      await onDelete();
    } catch (err) {
      setError(mutationErrorMessage(err));
      setPending(false);
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="Delete this document?"
      description={`“${title}” and all of its versions will no longer be available.`}
      initialFocusRef={cancelRef}
    >
      <p role="alert" className="min-h-5 text-sm text-destructive">
        {error}
      </p>
      <div className="flex justify-end gap-2">
        <Button ref={cancelRef} type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button type="button" variant="destructive" disabled={pending} onClick={confirm}>
          {pending ? "Deleting…" : "Delete"}
        </Button>
      </div>
    </Dialog>
  );
}
