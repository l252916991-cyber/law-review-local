# LexVault — local legal document review

LexVault is a local document review workbench built with **FastAPI/Python** and a **Next.js/React/TypeScript** interface. It connects answers to source pages and keeps case access, audit records, model egress controls, bounded read-only tools, and answer validation on the Python backend. Legal conclusions require qualified human review.

![Case workbench overview (synthetic demo case)](docs/images/workbench-overview.png)

## Results at a glance

| Question | Measured |
|---|---|
| Do structured specialist handoff (B) and cross-review (C) improve answers over plain RAG (A)? | Paired runs on a local Qwen3.8-27B (4-bit): dev set 60 questions A/B/C 86.7% / 76.7% / 86.7%; six-case holdout 24 questions × 3 rounds 90.3% / 88.9% / 87.5%. B/C use 4.9–6.4× the input tokens. **No measurable gain**, so cross-review stays off by default. |
| Does the statute corpus help? | LawBench paired runs (4-bit, 1,000 questions): mixed score 58.81% → 62.49% (+3.68 pp, 95% CI [+2.76, +4.69]); the other 18 tasks are unchanged question by question. |
| Can resume reuse stale analysis? | Checkpoints store a digest of every source page; an edited page makes resume refuse the old analysis without consuming a retry. |
| Engineering | Full offline test suite (network disabled, isolated data): 1,659 passed and 0 failed on both Python 3.11 and 3.14 (8 skipped: private LawBench data and Apple-only `mlx_lm`), about 87.5% combined coverage and 100% on the statute verification module; core endpoints declare response models and the frontend uses the generated types; ruff (E9/F/I/UP/B) and mypy gates; Docker build plus HTTP smoke test in CI. |

Not done yet: independent human labels (a two-reviewer blind package is ready), a recorded real-model demo, cross-model comparison, and law-firm business sign-off. Samples are small and synthetic; read the [project brief](docs/portfolio/project-brief.md) (Chinese) for exact scope.

Development: the maintainer owns direction, experiment design, acceptance criteria and trade-offs; much of the code was written with AI coding assistants (Codex, Claude).

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

Independent human labels and judge/human agreement remain outstanding. The 60-question regression set comes from a single synthetic case and is not lawyer-annotated ground truth. Negative results are retained in the execution records.

## License

Released under the [MIT License](LICENSE). Third-party dependencies and datasets (including LawBench, which is not redistributed here) keep their own licenses; see the [license inventory](docs/runbook/licenses.md).
