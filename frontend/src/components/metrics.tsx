"use client";
import { object, text, type Row } from "@/lib/data";
import { costText, milliseconds, tokenText } from "@/lib/metrics";
import { Empty, ErrorBox, list, useData } from "./ui";

export function Usage({ summary }: { summary: Row }) {
  const cost = summary.cost ? object(summary.cost) : {};
  return (
    <section aria-label="模型用量与估算费用">
      <div className="metrics">
        {[
          [
            "模型调用",
            text(summary.calls) || "0",
            `失败 ${text(summary.failed) || "0"} 次`,
          ],
          [
            "输入 tokens",
            tokenText(
              summary.input_tokens,
              summary.input_tokens_known_calls,
              summary.calls,
            ),
            `可用样本 ${text(summary.input_tokens_known_calls) || "0"}/${text(summary.calls) || "0"}`,
          ],
          [
            "输出 tokens",
            tokenText(
              summary.output_tokens,
              summary.output_tokens_known_calls,
              summary.calls,
            ),
            `可用样本 ${text(summary.output_tokens_known_calls) || "0"}/${text(summary.calls) || "0"}`,
          ],
          [
            "首 token P50 / P95",
            `${milliseconds(summary.ttft_p50_ms)} / ${milliseconds(summary.ttft_p95_ms)}`,
            `可用样本 ${text(summary.ttft_known_calls) || "0"}/${text(summary.calls) || "0"}`,
          ],
          [
            "调用延迟 P50 / P95",
            `${milliseconds(summary.latency_p50_ms)} / ${milliseconds(summary.latency_p95_ms)}`,
            "每次模型调用，非整轮问答延迟",
          ],
          [
            "估算费用",
            costText(cost),
            `可计价 ${text(cost.known_calls) || "0"}/${text(cost.calls) || "0"} 次`,
          ],
          [
            "未知价格 / 用量",
            `${text(cost.no_price_calls) || "0"} / ${text(cost.usage_missing_calls) || "0"}`,
            "缺失不计为零",
          ],
          [
            "问答数量",
            text(summary.turn_count) || "0",
            `定价版本 ${text(cost.version) || "未配置或多个版本"}`,
          ],
        ].map(([label, value, note]) => (
          <article className="card" key={label}>
            <span>{label}</span>
            <strong className="metric-value">{value}</strong>
            <small>{note}</small>
          </article>
        ))}
      </div>
      <p>
        金额为按已记录定价与用量计算的估算费用，不是账单；未配置本地资源成本时不显示为免费。首
        token 指服务端调用时间，非浏览器首字可见时间。
      </p>
      <ErrorBox error={text(cost.config_error)} />
      {(["by_kind", "by_model"] as const).map((key) => (
        <div className="card table-wrap" key={key}>
          <table>
            <caption>{key === "by_kind" ? "按调用类型" : "按模型"}</caption>
            <thead>
              <tr>
                <th>分组</th>
                <th>调用 / 失败</th>
                <th>输入 / 输出 tokens</th>
                <th>首 token P50 / P95</th>
                <th>延迟 P50 / P95</th>
              </tr>
            </thead>
            <tbody>
              {list(summary[key]).map((row, index) => (
                <tr key={index}>
                  <td>{text(row.kind || row.model)}</td>
                  <td>
                    {text(row.calls)} / {text(row.failed)}
                  </td>
                  <td>
                    {tokenText(
                      row.input_tokens,
                      row.input_tokens_known_calls,
                      row.calls,
                    )}{" "}
                    /{" "}
                    {tokenText(
                      row.output_tokens,
                      row.output_tokens_known_calls,
                      row.calls,
                    )}
                  </td>
                  <td>
                    {milliseconds(row.ttft_p50_ms)} /{" "}
                    {milliseconds(row.ttft_p95_ms)}
                  </td>
                  <td>
                    {milliseconds(row.latency_p50_ms)} /{" "}
                    {milliseconds(row.latency_p95_ms)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!list(summary[key]).length && <Empty>尚无调用记录</Empty>}
        </div>
      ))}
      <div className="card table-wrap">
        <table>
          <caption>问答级费用（包含工具循环及回退调用）</caption>
          <thead>
            <tr>
              <th>问答</th>
              <th>模型</th>
              <th>可计价 / 调用</th>
              <th>估算费用</th>
            </tr>
          </thead>
          <tbody>
            {list(summary.turns).map((row, index) => (
              <tr key={index}>
                <td>{text(row.turn_key) || "未归属"}</td>
                <td>
                  {Array.isArray(row.models)
                    ? row.models.map(text).join("、")
                    : "未知"}
                </td>
                <td>
                  {text(row.known_calls)}/{text(row.calls)}
                </td>
                <td>
                  {row.mixed_currency
                    ? "多币种，不求和"
                    : row.cost != null
                      ? text(row.cost)
                      : row.partial_cost != null
                        ? `≥${text(row.partial_cost)}`
                        : "未知"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
export function Metrics({ caseId }: { caseId: number }) {
  const data = useData(`/api/cases/${caseId}/platform-metrics`);
  const summary = data.data ? object(data.data) : {};
  return (
    <section>
      <header className="section-header">
        <div>
          <span className="eyebrow">运行观测</span>
          <h2>用量、延迟与费用</h2>
        </div>
        <button onClick={data.reload}>刷新指标</button>
      </header>
      <ErrorBox error={data.error} />
      {data.data ? (
        <Usage summary={object(summary.llm_usage)} />
      ) : (
        <Empty>{data.error ? "指标不可用" : "正在加载指标…"}</Empty>
      )}
    </section>
  );
}
