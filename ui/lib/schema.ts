import type { InputSchema, SchemaProp } from "./types";

export type Values = Record<string, string | boolean>;

const typesOf = (p: SchemaProp): string[] => (Array.isArray(p.type) ? p.type : p.type ? [p.type] : ["string"]).filter((t) => t !== "null");
export const kindOf = (p: SchemaProp): "enum" | "boolean" | "number" | "text" => p.enum ? "enum" : typesOf(p).includes("boolean") ? "boolean" : typesOf(p).some((t) => t === "number" || t === "integer") ? "number" : "text";

const DATE = /^\^?\[0-9\]\{4\}-\[0-9\]\{2\}-\[0-9\]\{2\}\$?$/;
const TIME = /^\^?\[0-9\]\{2\}:\[0-9\]\{2\}\$?$/;

/** A placeholder that shows the shape an input takes, derived from its pattern, so a person is not shown a regex. */
export function placeholderFor(p: SchemaProp): string | undefined {
  const pat = p.pattern;
  if (!pat) return undefined;
  if (DATE.test(pat)) return "YYYY-MM-DD";
  if (TIME.test(pat)) return "HH:MM";
  let s = pat.replace(/^\^|\$$/g, "");
  s = s.replace(/\(\\\.\[0-9\]\{(\d+)\}\)\?/g, (_, n) => "." + "0".repeat(Number(n)));
  s = s.replace(/\[0-9\]\{(\d+)\}/g, (_, n) => "0".repeat(Number(n))).replace(/\[0-9\]\+/g, "0");
  return /[\\[\](){}|*+?]/.test(s) ? undefined : s;
}

export function validate(schema: InputSchema, values: Values): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const [name, prop] of Object.entries(schema.properties)) {
    const raw = values[name];
    const empty = raw === undefined || raw === "";
    if (empty) { if (schema.required.includes(name)) errors[name] = "Required"; continue; }
    if (typeof raw !== "string") continue;
    if (prop.enum && !prop.enum.includes(raw)) errors[name] = "Choose one of the listed options";
    else if (prop.pattern && !new RegExp(prop.pattern).test(raw)) { const ph = placeholderFor(prop); errors[name] = ph ? `Doesn't match the expected format, e.g. ${ph}` : "Doesn't match the expected format"; }
    else if (prop.minLength !== undefined && raw.length < prop.minLength) errors[name] = `At least ${prop.minLength} characters`;
    else if (prop.maxLength !== undefined && raw.length > prop.maxLength) errors[name] = `At most ${prop.maxLength} characters`;
    else if (kindOf(prop) === "number") {
      const n = Number(raw);
      if (Number.isNaN(n)) errors[name] = "Enter a number";
      else if (prop.minimum !== undefined && n < prop.minimum) errors[name] = `At least ${prop.minimum}`;
      else if (prop.maximum !== undefined && n > prop.maximum) errors[name] = `At most ${prop.maximum}`;
    }
  }
  const group = schema.at_least_one_of ?? [];
  if (group.length && !group.some((n) => values[n] !== undefined && values[n] !== "")) {
    for (const n of group) errors[n] = errors[n] ?? "";
    errors["_group"] = `Fill in at least one of: ${group.map((n) => n.replace(/_/g, " ")).join(", ")}`;
  }
  return errors;
}

/** Empty optional fields are left out entirely: "not supplied" means "leave that field as it is". */
export function buildParams(schema: InputSchema, values: Values): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [name, prop] of Object.entries(schema.properties)) {
    const v = values[name];
    if (v === undefined || v === "") continue;
    out[name] = kindOf(prop) === "number" && typeof v === "string" ? Number(v) : v;
  }
  return out;
}

const ACRONYMS = new Set(["mrn", "id", "url", "ssn", "dob"]);
export const humanize = (name: string) => name.split("_").map((w, i) => (ACRONYMS.has(w) ? w.toUpperCase() : i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w)).join(" ");
export const newKey = () => `ui-${crypto.randomUUID()}`;
