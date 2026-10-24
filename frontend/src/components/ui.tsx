"use client";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "@/lib/api";
import { message, rows } from "@/lib/data";

export function useData(path: string | null) {
  const [version, update] = useState(0);
  const [state, set] = useState<{
    path: string;
    data?: unknown;
    error?: string;
  }>({ path: "" });
  const reload = useCallback(() => update((value) => value + 1), []);
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    api(path, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) set({ path, data });
      })
      .catch((error) => {
        if (!controller.signal.aborted) set({ path, error: message(error) });
      });
    return () => controller.abort();
  }, [path, version]);
  return {
    data: state.path === path ? state.data : undefined,
    error: state.path === path ? state.error : undefined,
    reload,
  };
}
export function list(value: unknown) {
  return value === undefined ? [] : rows(value);
}
export function ErrorBox({ error }: { error?: string }) {
  return error ? (
    <p role="alert" className="error">
      {error}
    </p>
  ) : null;
}
export function Empty({ children = "暂无内容" }: { children?: ReactNode }) {
  return <div className="empty">{children}</div>;
}
export function Modal({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog ref={ref} onCancel={close} aria-label={title}>
      <header>
        <h2>{title}</h2>
        <button onClick={close} aria-label="关闭">
          ×
        </button>
      </header>
      {children}
    </dialog>
  );
}
export function Fields({
  names,
  values = {},
}: {
  names: Record<string, string>;
  values?: Record<string, unknown>;
}) {
  return (
    <div className="fields">
      {Object.entries(names).map(([name, label]) => (
        <label key={name}>
          {label}
          <input name={name} defaultValue={String(values[name] ?? "")} />
        </label>
      ))}
    </div>
  );
}
export function values(form: HTMLFormElement) {
  return Object.fromEntries(new FormData(form));
}
