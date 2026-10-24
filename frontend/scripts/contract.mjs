import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import openapiTS, { astToString } from 'openapi-typescript';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const temp = mkdtempSync(join(tmpdir(), 'lexvault-contract-'));
try {
  const result = spawnSync('uv', ['run', '--locked', 'python', '-c',
    'import json; from app.main import app; print(json.dumps(app.openapi(), ensure_ascii=False))'], {
    cwd: root, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024,
    env: { ...process.env, LAW_REVIEW_DATA_DIR: temp, LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB: join(temp, 'checkpoint.sqlite'), LAW_REVIEW_LLM_URL: 'http://127.0.0.1:1/v1' },
  });
  if (result.status !== 0) throw new Error(result.stderr || 'OpenAPI export failed');
  const output = '// Generated from FastAPI OpenAPI. Run pnpm contract:generate.\n' + astToString(await openapiTS(JSON.parse(result.stdout)));
  const destination = new URL('../src/lib/schema.d.ts', import.meta.url);
  if (process.argv.includes('--check')) {
    if (readFileSync(destination, 'utf8') !== output) throw new Error('FastAPI contract changed; run pnpm contract:generate');
  } else writeFileSync(destination, output);
} finally { rmSync(temp, { recursive: true, force: true }); }
