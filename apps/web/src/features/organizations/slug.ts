/** Org slug rule from contract §2; the same rule is assumed for workspace slugs (unique per org). */
export const SLUG_PATTERN = "^[a-z0-9][a-z0-9-]{1,62}$";
const SLUG_RE = new RegExp(SLUG_PATTERN);

export function isValidSlug(value: string): boolean {
  return SLUG_RE.test(value);
}

/** Suggests a slug from a display name ("Acme Research Lab" → "acme-research-lab"). */
export function slugify(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63)
    .replace(/-+$/g, "");
}
