# BIBhelper System Reference

## Architecture

- `packages/bib_core/src/bib_core/core.py`
  Main parsing, task construction, template filling, PDF conversion, and pipeline orchestration.
- `apps/service/app/main.py`
  FastAPI app, job storage, template management, authentication, and web/API endpoints.
- `apps/service/templates/`
  Server-rendered HTML pages for login, upload, jobs, and template management.
- `deploy/`
  Dockerfile and compose files for local build and GHCR deployment.

## Business scope

- Supported: `savings`, `critical_illness`
- Not supported: education

## Production output path

- Final documents must be produced as `DOCX -> PDF`.
- Do not route production summaries through HTML rendering unless the user explicitly reopens that project.

## Template management

- Current templates live under `BIBHELPER_DATA_ROOT/templates/current`
- Historical templates live under `BIBHELPER_DATA_ROOT/templates/history`
- Templates are persisted on the host bind mount, normally `/opt/bibhelper-data`
- Template web UI supports:
  - list current templates
  - upload/replace current template
  - download current template
  - view/download historical source templates
  - restore a historical template

## Storage

- Jobs root: `BIBHELPER_DATA_ROOT/jobs`
- Each job keeps:
  - uploaded PDFs
  - work dir
  - output dir
  - `result.zip`
  - `job.json`
  - `process.log`

## Service routes

### API

- `POST /api/v1/process`
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/{job_id}/download`
- `GET /healthz`

### Web

- `GET /login`
- `POST /login`
- `GET /upload`
- `POST /upload`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /templates`
- `GET /templates/{template_id}`
- `POST /templates/{template_id}/upload`
- `POST /templates/{template_id}/restore`

## Important parsing behavior

- Name extraction must preserve spaced English names.
- Savings total premium comes from the summary table, using the last row second column as the primary source.
- Simplified/traditional Chinese variants should be handled in regexes where practical.
- If a new extraction rule is added, add a regression test in `tests/test_aia.py`.

## Secrets and deployment

- Runtime secrets must live in `deploy/.env.runtime`
- Tracked files may include only `deploy/.env.example`
- `deploy/docker-compose.ghcr.yml` is the preferred production deploy file
- GHCR image is built by `.github/workflows/docker-publish.yml`

## Useful commands

### Local service

```bash
uvicorn apps.service.app.main:app --host 0.0.0.0 --port 8000
```

### Full tests

```bash
./aiahelper_pro/bin/python -m unittest discover -s tests -v
```

### Server upgrade

```bash
cd /opt/bibhelper && git pull origin main && docker compose -f deploy/docker-compose.ghcr.yml pull && docker compose -f deploy/docker-compose.ghcr.yml up -d
```

