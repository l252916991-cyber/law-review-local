"""Desktop chat client: one request id per submit, protocol events, one retry."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Minimal page globals. The client module is a classic script, so slicing the
# functions and running them in a vm keeps the test free of a browser.
HARNESS = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const sliced = source.slice(
  source.indexOf('async function streamChat('),
  source.indexOf('async function uploadFiles(')
);
const sse = (frames) => frames.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('');
function streamResponse(frames) {
  const payload = Buffer.from(sse(frames), 'utf8');
  let sent = false;
  return {
    ok: true,
    headers: {get: () => 'text/event-stream'},
    body: {getReader: () => ({read: async () => sent ? {done: true} : (sent = true, {value: payload, done: false})})},
  };
}
const nodes = new Map();
const meta = {children: [], querySelector: () => null, append(child) { this.children.push(child); }};
let retracted = false;
const messages = [{querySelector: () => meta, classList: {contains: (name) => name === 'retracted' && retracted}}];
const document = {createElement: () => ({dataset: {}, className: '', textContent: ''})};
const $ = (key) => {
  if (!nodes.has(key)) {
    nodes.set(key, {
      value: key === '#chat-input' ? '问题' : '律师', checked: true, disabled: false,
      innerHTML: '', scrollTop: 0, textContent: '',
      remove() {}, append() {},
    });
  }
  return nodes.get(key);
};
const calls = {fetch: [], appended: [], notes: [], failures: [], removed: 0, api: []};
const provisional = {
  note(value) { calls.notes.push(value); },
  append(value) { calls.notes.push(`append:${value}`); },
  fail(value) { calls.failures.push(value); retracted = true; },
  remove() { calls.removed += 1; },
};
const entries = [];
let sequence = 0;
const context = vm.createContext({
  console, JSON, Number, String, Array, Math, TextDecoder, Uint8Array,
  window: {crypto: {randomUUID: () => `uuid-${++sequence}-aaaaaaaa-bbbb-cccc-dddddddd`}},
  document, $, $$: () => messages,
  state: {caseId: 7, conversationId: null, conversations: []},
  appendMessage: (...args) => calls.appended.push(args),
  appendTyping: () => {},
  appendStreamingMessage: () => provisional,
  renderConversations: () => {},
  toast: () => {},
  api: async (path) => { calls.api.push(path); return []; },
  fetch: async (url, init) => {
    calls.fetch.push({url, body: JSON.parse(init.body)});
    const next = entries.shift();
    if (typeof next === 'function') return next();
    if (next instanceof Error) throw next;
    return next;
  },
});
vm.runInContext(sliced + '\n;globalThis.__sendChat = sendChat; globalThis.__newId = newChatRequestId;', context, {filename: 'app.js'});
const sendChat = context.__sendChat;
const newChatRequestId = context.__newId;
// The sandbox is contextified in place, so its properties are observable here.
const state = context.state;
'''


class ChatClientTests(unittest.TestCase):
    def run_node(self, body: str) -> None:
        script = HARNESS + "(async () => {\n" + body + "\n})().catch(e => {console.error(e); process.exitCode = 1;});\n"
        result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_request_id_matches_the_server_pattern(self):
        self.run_node(r'''
  const pattern = /^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$/;
  const first = newChatRequestId();
  const second = newChatRequestId();
  assert.match(first, pattern);
  assert.match(second, pattern);
  assert.notEqual(first, second);
''')

    def test_success_path_reports_phases_citations_and_first_token(self):
        self.run_node(r'''
  entries.push(streamResponse([
    ['status', {phase: 'retrieval'}],
    ['status', {phase: 'generating'}],
    ['token', {text: '第一段', provisional: true}],
    ['status', {phase: 'validating'}],
    ['citations', {citations: [{index: 1}], count: 1}],
    ['final', {answer: '第一段[资料1]', conversation_id: 42, citations: [{index: 1}], route: '事实检索'}],
    ['done', {status: 'ok', conversation_id: 42, message_id: 9, ttft_ms: 1200}],
  ]));
  await sendChat();
  const body = calls.fetch[0].body;
  assert.match(body.request_id, /^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$/);
  assert.equal(body.question, '问题');
  assert.deepEqual(calls.notes, [
    '检索中…', '生成中', '生成中（草稿待校验）', 'append:第一段', '校验引用…', '引用已校验 1 条',
  ]);
  assert.equal(calls.appended.length, 2);
  assert.deepEqual(calls.appended[1].slice(0, 2), ['assistant', '第一段[资料1]']);
  assert.equal(state.conversationId, 42);
  assert.equal(calls.failures.length, 0);
  assert.equal(meta.children.length, 1);
  assert.equal(meta.children[0].textContent, '首字 1.2s');
  assert.deepEqual(calls.api, ['/api/cases/7/conversations']);
''')

    def test_retract_clears_the_draft_and_reports_the_server_message(self):
        self.run_node(r'''
  entries.push(streamResponse([
    ['status', {phase: 'retrieval'}],
    ['token', {text: 'SECRET', provisional: true}],
    ['retract', {code: 'model_error', message: '已撤回未校验草稿'}],
    ['done', {status: 'failed', retracted: true, ttft_ms: 900}],
  ]));
  await sendChat();
  assert.deepEqual(calls.failures, ['已撤回未校验草稿']);
  assert.equal(calls.appended.length, 1);
  // No first-token badge on a retracted draft.
  assert.equal(meta.children.length, 0);
  assert.equal(calls.api.length, 0);
''')

    def test_a_failed_done_without_retract_still_fails_the_bubble(self):
        self.run_node(r'''
  entries.push(streamResponse([
    ['status', {phase: 'retrieval'}],
    ['done', {status: 'failed', message: '模型不可用'}],
  ]));
  await sendChat();
  assert.deepEqual(calls.failures, ['模型不可用']);
  // The draft stays on screen but is marked failed; no answer is appended.
  assert.equal(calls.appended.length, 1);
  assert.equal(meta.children.length, 0);
''')

    def test_network_drop_retries_once_with_the_same_request_id(self):
        self.run_node(r'''
  entries.push(new TypeError('network down'));
  entries.push(streamResponse([
    ['status', {phase: 'replayed'}],
    ['citations', {citations: [], count: 0}],
    ['final', {answer: '已提交的回答', conversation_id: 43, citations: []}],
    ['done', {status: 'ok', conversation_id: 43, message_id: 10, replayed: true}],
  ]));
  await sendChat();
  assert.equal(calls.fetch.length, 2);
  assert.equal(calls.fetch[0].body.request_id, calls.fetch[1].body.request_id);
  assert.ok(calls.notes.includes('连接中断，正在重试…'));
  assert.equal(calls.appended[1][1], '已提交的回答');
  assert.equal(state.conversationId, 43);
''')

    def test_http_error_is_not_retried(self):
        self.run_node(r'''
  entries.push(async () => {
    const error = new Error('同一请求正在处理，请勿重复提交');
    error.status = 409;
    throw error;
  });
  await sendChat();
  assert.equal(calls.fetch.length, 1);
  assert.equal(calls.appended[1][1], '处理失败：同一请求正在处理，请勿重复提交');
''')


if __name__ == "__main__":
    unittest.main()
