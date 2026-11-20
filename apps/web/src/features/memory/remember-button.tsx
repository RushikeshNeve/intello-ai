"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useApiFetcher } from "@/lib/query/provider";
import { createSuggestion, errorText, evidenceFromCitations, MAX_STATEMENT } from "./api";

type Cite = { chunk_id: string; document_id: string; version_id: string; workspace_id: string };

/** "Remember this" on a chat answer: proposes a memory suggestion (never approved automatically) with the answer's citations as evidence. */
export function RememberButton({ orgId, orgSlug, answer, citations }: { orgId: string; orgSlug: string; answer: string; citations: readonly Cite[] }) {
  const fetcher = useApiFetcher();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(() => answer.slice(0, MAX_STATEMENT));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const evidence = evidenceFromCitations(citations);
  if (evidence.length === 0) return null; // a memory needs sources

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const ws = new Set(citations.map((c) => c.workspace_id));
      await createSuggestion(fetcher, orgId, { statement: text.trim(), kind: "fact", workspace_id: ws.size === 1 ? [...ws][0] : null, evidence });
      setSaved(true);
      setOpen(false);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  if (saved) {
    return (
      <p role="status" className="text-xs text-muted-foreground" data-testid="remember-saved">
        Saved as a suggestion.{" "}
        <a href={`/o/${encodeURIComponent(orgSlug)}/memory`} className="underline">
          Review it in Memory
        </a>
      </p>
    );
  }
  return (
    <div className="grid gap-2" data-testid="remember">
      {!open ? (
        <Button variant="ghost" size="sm" className="w-fit" onClick={() => setOpen(true)} data-testid="remember-open">
          Remember this
        </Button>
      ) : (
        <div className="grid gap-2 rounded-md border border-border p-2">
          <label className="grid gap-1 text-xs text-muted-foreground">
            What should be remembered? It stays a suggestion until you approve it.
            <textarea
              className="min-h-20 w-full rounded-md border border-input bg-transparent p-2 text-sm text-foreground"
              value={text}
              maxLength={MAX_STATEMENT}
              onChange={(e) => setText(e.target.value)}
              data-testid="remember-text"
            />
          </label>
          <div className="flex gap-2">
            <Button size="sm" disabled={busy || text.trim() === ""} onClick={() => void save()} data-testid="remember-save">
              Save suggestion
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="text-xs text-destructive" data-testid="remember-error">
          {error}
        </p>
      )}
    </div>
  );
}
