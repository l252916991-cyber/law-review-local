export type Row = Record<string, unknown>;
export function object(value: unknown): Row {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("接口返回格式无效");
  return value as Row;
}
export function rows(value: unknown): Row[] {
  if (!Array.isArray(value)) throw new Error("接口列表格式无效");
  return value.map(object);
}
export function text(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}
export function id(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1)
    throw new Error("资源标识无效");
  return value;
}
export function message(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请重试";
}
