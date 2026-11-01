# LexVault — local legal document review

LexVault is a local document review workbench built with **FastAPI/Python** and a **Next.js/React/TypeScript** interface. It connects answers to source pages and keeps case access, audit records, model egress controls, bounded read-only tools, and answer validation on the Python backend. Legal conclusions require qualified human review.

## Run locally

Use Python 3.11–3.14, the Node version in `frontend/.node-version` (24.22.1), and pnpm 11.25.0. Install `uv` 0.12.0 and document parsing/OCR tools as described in the [Chinese setup guide](README.md).

```sh
uv sync --locked --group dev
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build:static
./run.sh
```

Open `http://127.0.0.1:8765`. FastAPI serves the static UI and API from the same origin; Node is needed for development/builds. The application does not implicitly load `.env`. Inject configuration explicitly using the safe examples in `.env.example`. Local mode retains its loopback restrictions; shared access requires configured token/OIDC authentication.

Model functions require an explicitly configured compatible local endpoint and an actual model ID. Remote model access is disabled by default. Offline rule summaries and fixed-answer test doubles are distinct from real model results. Scan support requires working parsers/OCR and a separate quality evaluation.

## Capabilities and checks

- Case/document management, source preview, evidence annotations and exports.
- SSE answer drafts, validation/retraction, citations and idempotent retry.
- Native DAG/LangGraph workflows, checkpoints, read-only tools, traces and optional model fallback.
- Usage, latency and estimated costs with unknown/partial observations preserved.
- A frozen 60-question synthetic answer regression set, strict optional judge and a digest-bound 20-item blind review workflow.

```sh
uv run --locked ruff check app tests scripts
uv run --locked mypy
uv run --locked python scripts/build_answer_eval.py
pnpm --dir frontend contract:check
pnpm --dir frontend typecheck
pnpm --dir frontend lint
pnpm --dir frontend test
pnpm --dir frontend build:static
pnpm --dir frontend test:e2e
```

Full offline checks, dependency export parity and coverage follow `.github/workflows/test.yml`. Use isolated synthetic data for tests. M2 integration needs the separately installed, pinned Python 3.12 framework environment; a skipped integration is not a pass.

## Demo and evidence

See the [frontend runbook](docs/runbook/frontend.md), [MCP acceptance](docs/runbook/mcp-acceptance.md), [answer review guide](docs/runbook/answer-evaluation.md), and [execution record](docs/history/migration/ai-application-upgrade-followup-execution-20261025.md).

The recording script `node frontend/scripts/record-demo.mjs --output output/new-demo` starts an isolated FastAPI instance and fixed-answer model test doubles. It records a silent, captioned three-minute workflow. Install the Playwright FFmpeg dependency first (`pnpm --dir frontend exec playwright install ffmpeg`) and set `LEXVAULT_BROWSER_PATH` when using an existing Chromium-family browser. The recording is explicitly labelled synthetic and is not evidence of real model accuracy.

Current real model acceptance is incomplete: local omlx requests were refused by memory admission; Cursor/Claude Desktop were unavailable. Independent human labels and judge/human agreement remain outstanding. A single-case, assistant-authored regression set is not lawyer-annotated ground truth. Negative results are retained in the execution record.

## Distribution status

**No project-wide open-source license has been selected.** This repository and the prepared demo are not a public release. Maintainers must confirm ownership, choose a license and review all third-party source/data notices before public distribution. Third-party metadata is documented in the [license inventory](docs/runbook/licenses.md); metadata alone is not a completed legal review. No public hosting, repository publication or community posting is performed by these scripts.
