# Local development

## API

```powershell
$env:PYTHONPATH = "$(Get-Location);$(Get-Location)\services\api;$(Get-Location)\packages\workflow-engine\src"
python -m pip install -r services/api/requirements.txt
python -m uvicorn app.asgi:app --app-dir services/api --host 127.0.0.1 --port 8000
```

## Web

```powershell
Set-Location apps/web
npm ci
npm run dev
```

The default `NEXT_PUBLIC_API_BASE_URL=/api/v1` uses the Next same-origin proxy and is the recommended value for local and embedded browsers. Set an absolute API URL only when the Web app is intentionally hosted separately from the API service.

## Tests

Run the Python suites from the repository root:

```powershell
python -m pytest -q services/api/tests packages/workflow_core/tests packages/workflow-engine/tests
```

Run the Web checks from `apps/web`:

```powershell
npm run typecheck
npm run lint
npm run build
```

The default configuration writes state below `output/`, which is ignored by Git. Use a separate temporary directory for test artifacts and delete or quarantine it after inspection.
