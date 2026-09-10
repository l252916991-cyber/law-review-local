"""Mobile entry, shared helpers and desktop race regressions without a browser."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import db_scope, init_db
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


class MobileRouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lexvault-mobile-")
        self.addCleanup(directory.cleanup)
        scope = db_scope(Path(directory.name) / "test.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=False)
        env = patch.dict(os.environ, {
            "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_ALLOWED_HOSTS": "testserver",
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({"m" * 40: {"name": "测试", "admin": True}}),
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_public_shells_and_assets_have_same_security_policy(self):
        desktop = self.client.get("/")
        for path in ("/mobile", "/assets/entry.js", "/assets/shared.js", "/assets/mobile.js", "/assets/mobile.css"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-cache")
                self.assertEqual(response.headers["content-security-policy"], desktop.headers["content-security-policy"])
                self.assertEqual(response.headers["x-frame-options"], "DENY")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        mobile = self.client.get("/mobile?auth_error=expired")
        self.assertIn("text/html", mobile.headers["content-type"])
        self.assertIn('/assets/mobile.js', mobile.text)
        self.assertNotIn('/assets/app.js', mobile.text)
        for html, app_script in ((desktop.text, '/assets/app.js'), (mobile.text, '/assets/mobile.js')):
            self.assertLess(html.index('/assets/entry.js'), html.index('</head>'))
            self.assertLess(html.index('/assets/shared.js'), html.index(app_script))
        self.assertGreaterEqual(desktop.text.count('/mobile?ui=mobile'), 2)
        for asset in ("entry.js", "shared.js", "app.js"):
            self.assertIn(f'/assets/{asset}?v=20260910-mobile-workspace', desktop.text)
        self.assertIn('?ui=desktop', mobile.text + (ROOT / "app/static/mobile.js").read_text())

    def test_mobile_shell_does_not_bypass_api_or_host_security(self):
        self.assertEqual(self.client.get("/mobile").status_code, 200)
        denied = self.client.get("/api/cases")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/mobile", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/session", json={"token": "m" * 40}, headers={"Origin": "https://evil.example"}).status_code, 403)


class MobileJavaScriptTests(unittest.TestCase):
    def run_node(self, source):
        result = subprocess.run(["node", "-e", source], cwd=ROOT, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_scripts_share_global_scope_without_duplicate_declarations(self):
        self.run_node(r'''
const vm = require('node:vm');
const fs = require('node:fs');
for (const page of ['app', 'mobile']) {
  new vm.Script(['entry', 'shared', page].map(name =>
    fs.readFileSync(`app/static/${name}.js`, 'utf8')).join('\n;\n'));
}
''')

    def test_entry_preferences_boundaries_and_no_redirect_loop(self):
        self.run_node(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync('app/static/entry.js', 'utf8');
function visit(path, width, saved, blocked = false) {
  const redirects = [];
  const store = new Map(saved ? [['lexvault-ui', saved]] : []);
  const window = {location: {href: 'http://localhost' + path, replace: v => redirects.push(v)},
    matchMedia: () => ({matches: width <= 900})};
  vm.runInNewContext(source, {URL, window, localStorage: {
    getItem: k => {if (blocked) throw Error(); return store.get(k);},
    setItem: (k,v) => {if (blocked) throw Error(); store.set(k,v);}
  }});
  assert.ok(redirects.length <= 1);
  if (redirects.length) {
    window.location.href = 'http://localhost' + redirects[0];
    vm.runInNewContext(source, {URL, window, localStorage: {getItem: k => store.get(k), setItem: (k,v) => store.set(k,v)}});
    assert.equal(redirects.length, 1, 'destination must not bounce');
  }
  return {target: redirects[0], saved: store.get('lexvault-ui')};
}
assert.equal(visit('/', 900).target, '/mobile');
assert.equal(visit('/', 901).target, undefined);
assert.equal(visit('/?auth_error=expired#login', 390).target, '/mobile?auth_error=expired#login');
assert.equal(visit('/', 390, 'desktop').target, undefined);
assert.equal(visit('/', 1400, 'mobile').target, '/mobile');
assert.equal(visit('/mobile', 1400, 'desktop').target, undefined);
assert.equal(visit('/mobile', 1400, 'desktop').saved, 'mobile');
assert.equal(visit('/', 1400, visit('/mobile', 1400).saved).target, '/mobile');
assert.equal(visit('/mobile?ui=desktop&auth_error=denied', 390).target, '/?ui=desktop&auth_error=denied');
assert.equal(visit('/?ui=mobile', 1400).saved, 'mobile');
assert.equal(visit('/?ui=auto', 390, 'desktop').saved, 'auto');
assert.equal(visit('/mobile?ui=auto', 1400, 'mobile').target, '/?ui=auto');
assert.equal(visit('/?ui=invalid', 390, 'desktop').target, undefined);
assert.equal(visit('/?ui=desktop', 390, null, true).target, undefined);
assert.equal(visit('/', 390, null, true).target, '/mobile');
''')

    def test_shared_api_unauthorized_and_safe_rendering(self):
        self.run_node(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const events = [];
let response;
const window = {dispatchEvent: e => events.push(e.type)};
vm.runInNewContext(require('node:fs').readFileSync('app/static/shared.js', 'utf8'), {
  window, CustomEvent: class {constructor(type) {this.type = type;}}, Event: class {constructor(type) {this.type = type;}},
  fetch: async () => response, console
});
(async () => {
  const {api, escapeHtml, markdown, formatDate} = window.LexVault;
  for (const [path, method, count] of [
    ['/api/auth/me', 'GET', 0], ['/api/auth/session', 'POST', 0],
    ['/api/cases', 'GET', 1], ['/api/auth/session', 'DELETE', 2]
  ]) {
    response = {status: 401, ok: false, json: async () => ({detail: 'denied'})};
    await assert.rejects(api(path, {method}), e => e.status === 401);
    assert.equal(events.length, count);
  }
  response = {status: 409, ok: false, json: async () => ({detail: {message: 'resume', run_id: 7, resumable: true}})};
  await assert.rejects(api('/api/cases'), e => e.message === 'resume' && e.runId === 7 && e.resumable);
  response = {ok: true, headers: {get: () => 'application/json'}, json: async () => ({id: 3})};
  assert.equal((await api('/api/cases')).id, 3);
  response = {ok: true, headers: {get: () => 'application/octet-stream'}};
  assert.equal(await api('/api/file'), response);
  assert.equal(events.length, 2);
  assert.equal(escapeHtml('<script>'), '&lt;script&gt;');
  assert.ok(!markdown('<img src=x onerror=alert(1)>').includes('<img'));
  assert.ok(markdown('**重点**\n- 条目').includes('<strong>重点</strong>'));
  assert.equal(formatDate(''), '刚刚');
  assert.equal(formatDate('invalid'), 'invalid');
})().catch(e => {console.error(e); process.exitCode = 1;});
''')

    def test_desktop_case_requests_ignore_stale_success_failure_and_metrics(self):
        self.run_node(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync('app/static/app.js', 'utf8');
const context = vm.createContext({console});
vm.runInContext(`
const state = {caseRequestId: 0};
const nodes = new Map();
const $ = key => {if (!nodes.has(key)) nodes.set(key, {}); return nodes.get(key);};
const $$ = () => [];
const pending = [];
const errors = [];
const api = path => new Promise((resolve, reject) => pending.push({path, resolve, reject}));
function toast(message) {errors.push(message);}
function showView() {}
function showLoadingState() {}
function hideLoadingState() {}
function renderCaseList() {}
function renderOverview() {}
function renderDirectory() {}
function renderEvidence() {}
function renderConversations() {}
function resetChat() {}
` + source.slice(source.indexOf('async function selectCase('), source.indexOf('/**\n * 显示加载状态')) +
source.slice(source.indexOf('async function loadLabMetrics('), source.indexOf('function renderAgentResult(')) + `
function finishCase(offset, title) {
  [{title}, [], {evidence: [], relations: []}, [], []].forEach((value,i) => pending[offset+i].resolve(value));
}
`, context);
(async () => {
  const run = code => vm.runInContext(code, context);
  const first = run('selectCase(1)');
  const second = run('selectCase(2)');
  run('finishCase(5, "new")');
  await new Promise(resolve => setImmediate(resolve));
  run('finishCase(0, "old")');
  await first;
  assert.equal(run('state.case.title'), 'new');
  const third = run('selectCase(3)');
  run('pending[10].resolve({agent_runs: {total: 999}}); pending[11].resolve({})');
  await second;
  assert.equal(run('$("#lab-run-count").textContent'), undefined);
  const fourth = run('selectCase(4)');
  run('pending[12].reject(new Error("old failure"))');
  await third;
  assert.equal(run('errors.length'), 0);
  run('finishCase(17, "latest")');
  await new Promise(resolve => setImmediate(resolve));
  run('pending[22].resolve({agent_runs: {total: 4}}); pending[23].resolve({total_questions: 0})');
  await fourth;
  assert.equal(run('state.case.title'), 'latest');
  assert.equal(run('$("#lab-run-count").textContent'), 4);
})().catch(e => {console.error(e); process.exitCode = 1;});
''')
