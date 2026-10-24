"use client";
import { useState } from "react";
import { api, jsonBody } from "@/lib/api";
import { message, object, text } from "@/lib/data";
import { ErrorBox, Fields, useData, values } from "./ui";

export function Settings() {
  const settings = useData("/api/model-config");
  const [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  return (
    <section>
      <header className="section-header">
        <div>
          <span className="eyebrow">本机私有模式</span>
          <h2>模型设置</h2>
        </div>
      </header>
      <ErrorBox error={error || settings.error} />
      {settings.data ? (
        <form
          className="card"
          key={JSON.stringify(settings.data)}
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await api(
                "/api/model-config",
                jsonBody(values(e.currentTarget), "PUT"),
              );
              setNotice("模型配置已保存");
              setError("");
              settings.reload();
            } catch (e) {
              setError(message(e));
            }
          }}
        >
          <Fields
            names={{ base_url: "模型服务地址", model: "模型名称" }}
            values={object(settings.data)}
          />
          <p>地址与模型仍须满足服务端出口规则；保存后可用于本案问答。</p>
          <button className="primary">保存模型设置</button>
          <button
            type="button"
            onClick={async (e) => {
              const form = e.currentTarget.form;
              if (!form) return;
              try {
                const result = object(
                  await api(
                    "/api/model-config/test",
                    jsonBody({ base_url: new FormData(form).get("base_url") }),
                  ),
                );
                setNotice(
                  `连接成功：${Array.isArray(result.models) ? result.models.map(text).join("、") : ""}`,
                );
                setError("");
              } catch (e) {
                setError(message(e));
              }
            }}
          >
            测试连接
          </button>
          <button
            type="button"
            onClick={async () => {
              try {
                await api("/api/model-config", { method: "DELETE" });
                settings.reload();
                setNotice("已恢复部署默认配置");
                setError("");
              } catch (e) {
                setError(message(e));
              }
            }}
          >
            恢复默认
          </button>
          <p role="status">{notice}</p>
        </form>
      ) : (
        <p>正在加载配置…</p>
      )}
    </section>
  );
}
