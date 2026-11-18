"use client";

import { useId, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/features/organizations/api/http";
import { isValidSlug, SLUG_PATTERN, slugify } from "@/features/organizations/slug";

export type SlugNameInput = { name: string; slug: string };

type FieldErrors = { name?: string; slug?: string; form?: string };

export function describeError(err: unknown, noun: string): FieldErrors {
  if (err instanceof ApiError) {
    if (err.code === "slug_taken") return { slug: `That slug is already in use. Choose another ${noun} slug.` };
    if (err.status === 403) return { form: `You don't have permission to create a ${noun} here.` };
    if (err.status === 422 || err.status === 400) return { form: "Please check the name and slug and try again." };
    if (err.status === 401) return { form: "Your session has ended. Redirecting you to sign in…" };
    if (err.isUnavailable) return { form: "The service is unavailable right now. Please try again shortly." };
  }
  return { form: `The ${noun} could not be created. Please try again.` };
}

/** Name + slug create form (orgs and workspaces). Errors are announced inline next to their field. */
export function SlugNameForm({
  noun,
  submitLabel,
  onCreate,
}: {
  noun: string;
  submitLabel: string;
  onCreate: (input: SlugNameInput) => Promise<unknown>;
}) {
  const id = useId();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const next: FieldErrors = {};
    const trimmed = name.trim();
    if (!trimmed) next.name = "Enter a name.";
    if (!isValidSlug(slug)) {
      next.slug = "Use 2–63 lowercase letters, digits or hyphens, starting with a letter or digit.";
    }
    setErrors(next);
    if (next.name || next.slug) {
      document.getElementById(next.name ? `${id}-name` : `${id}-slug`)?.focus();
      return;
    }
    setBusy(true);
    try {
      await onCreate({ name: trimmed, slug });
      setName("");
      setSlug("");
      setSlugTouched(false);
    } catch (err) {
      const mapped = describeError(err, noun);
      setErrors(mapped);
      if (mapped.slug) document.getElementById(`${id}-slug`)?.focus();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} noValidate className="grid gap-3 sm:max-w-md" aria-label={`Create ${noun}`}>
      <div className="grid gap-1.5">
        <Label htmlFor={`${id}-name`}>Name</Label>
        <Input
          id={`${id}-name`}
          name="name"
          value={name}
          autoComplete="off"
          aria-invalid={errors.name ? true : undefined}
          aria-describedby={errors.name ? `${id}-name-error` : undefined}
          onChange={(e) => {
            setName(e.target.value);
            if (!slugTouched) setSlug(slugify(e.target.value));
          }}
        />
        {errors.name && (
          <p id={`${id}-name-error`} className="text-sm text-destructive">
            {errors.name}
          </p>
        )}
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor={`${id}-slug`}>Slug</Label>
        <Input
          id={`${id}-slug`}
          name="slug"
          value={slug}
          autoComplete="off"
          spellCheck={false}
          pattern={SLUG_PATTERN}
          aria-invalid={errors.slug ? true : undefined}
          aria-describedby={`${id}-slug-hint${errors.slug ? ` ${id}-slug-error` : ""}`}
          onChange={(e) => {
            setSlugTouched(true);
            setSlug(e.target.value);
          }}
        />
        <p id={`${id}-slug-hint`} className="text-xs text-muted-foreground">
          Used in links. Lowercase letters, digits and hyphens.
        </p>
        {errors.slug && (
          <p id={`${id}-slug-error`} role="alert" className="text-sm text-destructive">
            {errors.slug}
          </p>
        )}
      </div>
      {errors.form && (
        <p role="alert" className="text-sm text-destructive">
          {errors.form}
        </p>
      )}
      <div>
        <Button type="submit" disabled={busy}>
          {busy ? "Creating…" : submitLabel}
        </Button>
      </div>
    </form>
  );
}
