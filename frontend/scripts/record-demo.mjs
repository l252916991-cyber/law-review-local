// Record the actual FastAPI static UI with synthetic data and explicit model doubles.
import { chromium, expect } from "@playwright/test";
import { createHash, randomBytes } from "node:crypto";
import { createWriteStream, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { execFileSync, spawn } from "node:child_process";
import { createServer } from "node:net";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const args = process.argv.slice(2);
const outputFlag = args.indexOf("--output");
const output = resolve(outputFlag >= 0 ? args[outputFlag + 1] : join(root, "output", `demo-${Date.now()}`));
if (existsSync(output)) throw new Error("Use a new output directory to retain previous recordings");
if (!existsSync(join(root, "frontend/out/index.html"))) throw new Error("Build the static UI first");
await new Promise((resolvePort, rejectPort) => {
  const probe = createServer();
  probe.once("error", rejectPort);
  probe.listen(13000, "127.0.0.1", () => probe.close(resolvePort));
});
mkdirSync(output, { recursive: true });
const log = createWriteStream(join(output, "server.log"));
const token = randomBytes(32).toString("hex");
const server = spawn("uv", ["run", "--project", root, "--locked", "python", join(root, "scripts/frontend_test_server.py")], {
  cwd: root, env: { ...process.env, LEXVAULT_E2E_TOKEN: token, LEXVAULT_E2E_MODE: "static" },
  stdio: ["ignore", "pipe", "pipe"],
});
server.stdout.pipe(log); server.stderr.pipe(log);
let browser, context;
let report = { ok: false, synthetic_data: true, model: "fixed_answer_test_double", real_model_verified: false,
  narration: "silent_with_captions", scenes: [], source_head: execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim(),
  dirty: Boolean(execFileSync("git", ["status", "--porcelain"], { cwd: root, encoding: "utf8" }).trim()),
  frontend_input_sha256: JSON.parse(readFileSync(join(root, "frontend/out/build-info.json"), "utf8")).input_sha256,
  runtime_files: Object.fromEntries(["app/main.py", "app/review/services.py", "app/review/tool_loop.py", "scripts/frontend_test_server.py"].map(path =>
    [path, createHash("sha256").update(readFileSync(join(root, path))).digest("hex")])) };
const delay = ms => new Promise(done => setTimeout(done, ms));
try {
  let ready = false;
  for (let i = 0; i < 160; i++) {
    if (server.exitCode !== null) throw new Error("Isolated server exited");
    try { ready = (await fetch("http://127.0.0.1:13000/api/auth/me", { signal: AbortSignal.timeout(500) })).ok; } catch {}
    if (ready) break;
    await delay(250);
  }
  if (!ready) throw new Error("Isolated server startup timeout");
  browser = await chromium.launch({ headless: true,
    ...(process.env.LEXVAULT_BROWSER_PATH ? { executablePath: process.env.LEXVAULT_BROWSER_PATH } : {}), slowMo: 120 });
  context = await browser.newContext({ viewport: { width: 1280, height: 800 },
    recordVideo: { dir: join(output, "video"), size: { width: 1280, height: 800 } } });
  const page = await context.newPage();
  const started = Date.now();
  const until = async seconds => { const remaining = started + seconds * 1000 - Date.now(); if (remaining > 0) await delay(remaining); };
  const scene = async (seconds, title) => {
    await until(seconds);
    await page.evaluate(text => {
      let caption = document.getElementById("synthetic-demo-caption");
      if (!caption) {
        caption = document.createElement("div"); caption.id = "synthetic-demo-caption";
        caption.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:999999;background:#183a32;color:white;padding:10px 20px;font:18px system-ui;pointer-events:none";
        document.body.append(caption); document.body.style.paddingTop = "48px";
      }
      caption.textContent = `${text}｜合成数据 · 模型测试替身`;
    }, title);
    report.scenes.push({ start_seconds: Math.round((Date.now() - started) / 1000), title });
    console.log(title);
  };
  await page.goto("http://127.0.0.1:13000/");
  await expect(page.getByRole("heading", { name: "登录律所工作空间" })).toBeVisible();
  await scene(0, "00 范围：本地工作流演示，输出必须由律师复核");
  await page.getByLabel("访问令牌").fill(token);
  await page.getByRole("button", { name: "登录工作空间" }).click();
  await expect(page.getByRole("navigation", { name: "案件列表" })).toBeVisible();
  await scene(12, "01 建案与导入：所有材料均为虚构文本");
  await page.getByRole("button", { name: "＋ 新建案件", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "新建案件" });
  await dialog.getByLabel("案件名称").fill("合成阅卷演示（非真实案件）");
  await dialog.getByRole("button", { name: "创建案件", exact: true }).click();
  await page.getByRole("button", { name: "卷宗目录", exact: true }).click();
  await page.getByLabel("上传卷宗", { exact: true }).setInputFiles({ name: "合成宣传审批.txt", mimeType: "text/plain",
    buffer: Buffer.from("【完全虚构演示材料】张某批准了年化12%的固定回报宣传。李某邮件写明到期还本付息。材料没有法院判决或已核验法条。") });
  await expect(page.getByRole("status")).toContainText("导入完成：1 个文件");
  await page.screenshot({ path: join(output, "01-import.png") });
  await scene(42, "02 流式提问：显示待校验草稿，再确认答案和引用");
  await page.getByRole("button", { name: "AI 阅卷", exact: true }).click();
  await page.getByLabel("向卷宗提问").fill("张某如何参与年化12%固定回报宣传？");
  await page.getByRole("button", { name: "发送 ↑" }).click();
  await expect(page.locator(".message.assistant .citation").first()).toBeVisible();
  await expect(page.locator(".message.assistant")).toContainText("请律师复核");
  await scene(72, "03 引用溯源：回到本次上传材料的原文");
  await page.locator(".message.assistant .citation").first().click();
  await expect(page.getByRole("complementary", { name: "卷宗原文" })).toContainText("完全虚构");
  await page.screenshot({ path: join(output, "02-citation.png") });
  await scene(92, "04 法条核验节点：缺少语料时不提供可核验法条");
  await page.getByRole("button", { name: "关闭原文" }).click();
  await page.getByRole("button", { name: "阅卷任务", exact: true }).click();
  await page.getByLabel("使用模型", { exact: true }).uncheck();
  await page.getByLabel("审校问题").fill("请核验刑法第999999条的法律规定，缺少依据时不要作法律结论。");
  await page.getByRole("button", { name: "运行审校 →" }).click();
  const legalTrace = page.getByRole("region", { name: "执行轨迹" });
  await expect(legalTrace.locator("header .badge").first()).toHaveText("completed", { timeout: 30000 });
  await legalTrace.locator("details").filter({ hasText: "法条核验 Agent" }).locator("summary").click();
  await expect(legalTrace).toContainText(/无法核验|法条语料不可用/);
  await page.screenshot({ path: join(output, "03-statute-boundary.png") });
  await scene(120, "05 Agent：只读工具、校验失败、测试替身回退与轨迹");
  await page.getByRole("navigation", { name: "案件列表" }).getByRole("button").filter({ hasText: "（演示）" }).first().click();
  await page.getByRole("button", { name: "阅卷任务", exact: true }).click();
  await page.getByLabel("审校问题").fill("回退验证：张某如何参与年化12%固定回报宣传？");
  await page.getByRole("button", { name: "运行审校 →" }).click();
  await expect(page.getByRole("region", { name: "审校结果" })).toContainText("回退模型：fallback-model", { timeout: 30000 });
  const trace = page.getByRole("region", { name: "执行轨迹" });
  await trace.locator("details").last().locator("summary").click();
  await expect(trace).toContainText("get_page");
  await page.screenshot({ path: join(output, "04-trace.png") });
  await scene(145, "06 用量与费用：可用样本分列，未知价格不会显示为免费");
  await page.getByRole("button", { name: "用量与费用", exact: true }).click();
  const usage = page.getByRole("region", { name: "模型用量与估算费用" });
  await expect(usage).toContainText("fallback-model");
  await expect(usage).toContainText("未配置成本");
  await page.screenshot({ path: join(output, "05-metrics.png") });
  await scene(170, "07 已演示工作流；真实模型、人工评分与开源授权仍待验收");
  await until(180);
  const video = page.video();
  await context.close(); context = null;
  await video.saveAs(join(output, "lexvault-synthetic-demo.webm"));
  report = { ...report, ok: true, duration_seconds: Math.round((Date.now() - started) / 1000), video: "lexvault-synthetic-demo.webm" };
} finally {
  if (context) await context.close();
  if (browser) await browser.close();
  server.kill("SIGTERM");
  await Promise.race([new Promise(done => server.once("close", done)), delay(10000).then(() => server.kill("SIGKILL"))]);
  log.end();
  writeFileSync(join(output, "demo-report.json"), JSON.stringify(report, null, 2) + "\n");
}
