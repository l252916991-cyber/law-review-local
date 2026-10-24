import { text, type Row } from "./data";
export function materialTimeline(evidence: Row[], documents: Row[]) {
  const byId = new Map(documents.map((row) => [row.id, row]));
  return evidence
    .map((row) => {
      const dateRange = text(byId.get(row.source_document_id)?.date_range);
      const sortDate =
        dateRange.match(/\b\d{4}-\d{2}(?:-\d{2})?\b/)?.[0] || null;
      return { evidence: row, dateRange, sortDate };
    })
    .sort((a, b) => (a.sortDate || "9999").localeCompare(b.sortDate || "9999"));
}
