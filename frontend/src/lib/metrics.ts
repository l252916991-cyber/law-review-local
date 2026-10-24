import { text, type Row } from "./data";
export function tokenText(
  value: unknown,
  known: unknown,
  calls: unknown,
): string {
  if (value == null) return "未知";
  return `${Number(known) < Number(calls) ? "≥" : ""}${Number(value).toLocaleString("zh-CN")}`;
}
export function milliseconds(value: unknown): string {
  return typeof value === "number" ? `${value}ms` : "未知";
}
export function costText(cost: Row): string {
  if (cost.mixed_currency) return "多币种，不求和";
  if (cost.total != null) return text(cost.total);
  if (cost.partial_total != null)
    return `≥${text(cost.partial_total)}（已知部分）`;
  if (!cost.configured) return "未配置成本";
  return "未知";
}
