# Furniture Workflow Website

Furniture Workflow Website is a local-first operator console for turning a public product URL into a traceable workflow:

`URL preflight → taxonomy and count evidence → scope selection → candidate discovery → image/identity review → production gate → optional 3D provider → delivery`

This repository contains the Website control plane only. The separate Skills package, private company material, credentials, browser profiles, databases, historical production output, and generated models are intentionally excluded.

## What is included

- Next.js operator console in `apps/web`
- FastAPI control plane in `services/api`
- deterministic workflow state machine in `packages/workflow-engine` and `packages/workflow_core`
- public-site taxonomy and product acquisition workers
- optional Lux3D adapter with idempotency and safety gates
- unit and integration regression tests
- Docker examples for local deployment

The Website does not need an AI provider for its local control-plane checks. The decision boundary supports two deployment modes without hard-coding either one:

1. `MULTIMODAL_SINGLE_MODEL`
2. `TEXT_BRAIN_PLUS_VISION`

`LOCAL_AGENT` is available for local review fixtures. Provider calls are disabled by default.

## Local setup

Requirements: Python 3.11+ and Node.js 20.9+.

```powershell
Copy-Item .env.example .env.local
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r services/api/requirements.txt
python -m playwright install chromium
Set-Location apps/web
npm ci
npm run build
Set-Location ../..
python launch_website.py
```

The launcher starts the API on `http://127.0.0.1:8000` and the Web UI on `http://127.0.0.1:3000`. It reads only non-secret routing values from `.env.local`; provider credentials are read by the API process when explicitly configured.

### Deterministic end-to-end check

The public repository includes an opt-in, network-free workflow check. It exercises Add Site, taxonomy snapshot binding, category scope, candidate review, provider ledger, Blender QA, and 20-per-batch delivery with local deterministic adapters:

```powershell
$env:PYTHONPATH = "$(Get-Location);$(Get-Location)\services\api;$(Get-Location)\packages\workflow-engine\src"
python -m pytest -q services/api/tests/test_website_e2e.py
```

The test itself sets `FURNITURE_WORKFLOW_LOCAL_E2E=1` and uses `test_profile=LOCAL_E2E`. These switches are required together and are never enabled by ordinary jobs. The test does not contact a real AI or Lux3D service.

For development, run the API and Web app separately:

```powershell
$env:PYTHONPATH = "$(Get-Location);$(Get-Location)\services\api;$(Get-Location)\packages\workflow-engine\src"
python -m uvicorn app.asgi:app --app-dir services/api --host 127.0.0.1 --port 8000
```

In another terminal:

```powershell
Set-Location apps/web
npm run dev
```

## Provider configuration boundary

`.env.example` lists variable names only. It contains no live endpoints, API keys, tokens, passwords, cookies, or account identifiers.

- Website Brain uses the `WEBSITE_BRAIN_*` namespace.
- Public-site L2 uses an isolated Playwright browser. The default is `WEBSITE_L2_BROWSER_ENGINE=chromium`; run `python -m playwright install chromium` once. `msedge`/`chrome` are supported for machines that have those browsers installed, but each Website job still receives its own persistent profile.
- Lux3D uses `LUX3D_*` and remains disabled until qualification, an idempotency ledger, an explicit cost ceiling, and the production gate all pass.
- Blender post-processing uses `BLENDER_WORKER_ENABLED=true` plus a resolvable `BLENDER_EXECUTABLE`; if it is not configured, model delivery stops with an explicit QA/configuration state.
- A failed or unknown provider submission is quarantined; the runtime must not blindly resubmit it.
- No provider is contacted during the default local setup.

Keep `.env.local`, databases, browser sessions, captured media, and generated GLBs outside version control.

## Development checks

```powershell
python -m pytest -q services/api/tests packages/workflow_core/tests packages/workflow-engine/tests
Set-Location apps/web
npm run typecheck
npm run lint
npm run build
```

When changing taxonomy, count semantics, identity binding, queue recovery, provider safety, or delivery behavior, add a focused regression test. Exact counts must be backed by direct evidence; an inaccessible or ambiguous count stays `UNKNOWN` or `ESTIMATED`.

See [docs/WEBSITE_E2E_REPAIR_REPORT.md](docs/WEBSITE_E2E_REPAIR_REPORT.md) for the repair rationale, test scope, safety review, and known live-environment boundaries.

## Safe acquisition principles

The acquisition layer is designed for public pages and public APIs. It respects access boundaries, rate limits, robots/terms requirements, and human-verification states. It does not bypass CAPTCHA, WAF, login, paywalls, or other access controls, and it preserves an explicit blocked/incomplete state when evidence is insufficient.
