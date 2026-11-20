/** Node type -> brand colour. Violet is reserved for synthesised knowledge (summaries, themes). Colour is never the only cue: types are always written out. */
export type NodeTone = "blue" | "teal" | "indigo" | "violet";

const TONES: Record<string, NodeTone> = {
  concept: "blue",
  person: "indigo",
  organization: "indigo",
  project: "indigo",
  document: "teal",
  policy: "teal",
  product: "teal",
  synthesis: "violet",
  summary: "violet",
  theme: "violet",
};

export function nodeTone(type: string): NodeTone {
  return TONES[type.toLowerCase()] ?? "blue";
}

export const TONE_VAR: Record<NodeTone, string> = {
  blue: "var(--brand-blue)",
  teal: "var(--brand-teal)",
  indigo: "var(--brand-indigo)",
  violet: "var(--brand-violet)",
};

export const TONE_DOT_CLASS: Record<NodeTone, string> = {
  blue: "bg-brand-blue",
  teal: "bg-brand-teal",
  indigo: "bg-brand-indigo dark:bg-info",
  violet: "bg-brand-violet",
};
