import type { components } from "./schema";
import { object, rows } from "./data";

type Schemas = components["schemas"];

/**
 * Generated response models allow extra keys (the backend never drops a field),
 * which openapi-typescript renders as an open index signature. Removing it here
 * makes a misspelled or undeclared field a compile error instead of `unknown`.
 */
type Known<T> = { [K in keyof T as string extends K ? never : K]: T[K] };

export type CaseRow = Known<Schemas["CaseRow"]>;
export type CaseSummary = Known<Schemas["CaseSummary"]>;
export type CaseMetrics = Known<Schemas["CaseMetrics"]>;
export type CaseDetail = Omit<Known<Schemas["CaseDetail"]>, "metrics"> & { metrics: CaseMetrics };
export type DocumentSummary = Known<Schemas["DocumentSummary"]>;
export type PageView = Known<Schemas["PageView"]>;
export type SearchHit = Known<Schemas["SearchHit"]>;
export type EvidenceItem = Known<Schemas["EvidenceItem"]>;
export type EvidenceRelation = Known<Schemas["EvidenceRelation"]>;
export type EvidenceList = { evidence: EvidenceItem[]; relations: EvidenceRelation[] };
export type ConversationSummary = Known<Schemas["ConversationSummary"]>;
export type AgentStep = Known<Schemas["AgentStep"]>;
export type AgentRunTrace = Omit<Known<Schemas["AgentRunTrace"]>, "steps"> & { steps: AgentStep[] };

/** Keeps the runtime shape guard (an array of objects); the element type is the backend schema. */
export function rowsOf<T>(value: unknown): T[] {
  return value == null ? [] : (rows(value) as unknown as T[]);
}

/** Keeps the runtime shape guard (a plain object); the type is the backend schema. */
export function objectOf<T>(value: unknown): T {
  return object(value) as unknown as T;
}
