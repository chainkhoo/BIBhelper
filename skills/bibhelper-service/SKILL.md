---
name: bibhelper-service
description: Use when working on the BIBhelper insurance-summary system: parsing AIA proposal PDFs, maintaining the FastAPI service, template management, or Docker/GHCR deployment. Applies only to savings and critical illness workflows. Preserve the current production DOCX-to-PDF pipeline, do not reintroduce education logic, and never commit deploy secrets.
---

# BIBhelper Service

Use this skill when the task is about this repository's insurance-summary workflow, including:

- parsing AIA proposal PDFs
- fixing savings or critical illness extraction rules
- updating the FastAPI API or web console
- managing DOCX templates and template history
- debugging Docker, GHCR, or 1Panel deployment

Do not use this skill for unrelated generic Python or frontend work.

## Current invariants

- Supported scope is only `savings` and `critical_illness`.
- Education logic must stay removed.
- Production output is `DOCX -> PDF`.
- The repository still keeps low-level `HTML -> PDF` helper code, but production must not route final documents through HTML.
- Template management only manages current and historical source templates. Do not restore automatic HTML template generation unless explicitly requested.
- Service timestamps are GMT+8.
- Secrets belong in `deploy/.env.runtime`, never in tracked compose files.
- Agent integrations must use API Bearer Token auth, not the web login flow.

## Key files

- Core parsing and document generation: `packages/bib_core/src/bib_core/core.py`
- Service routes and storage: `apps/service/app/main.py`
- Web templates: `apps/service/templates/`
- Deployment files: `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/docker-compose.ghcr.yml`
- Regression tests: `tests/test_aia.py`
- Project usage/deploy reference: `README.md`

Read `references/system.md` when you need file-by-file guidance, routes, or deployment commands.
Read `references/api-auth.md` when you need to call the deployed service from Codex, another agent, or OpenClaw.

## Parsing rules that are easy to regress

- Savings comparison grouping depends on consistent name extraction; English names with spaces must keep the full name.
- Single upload batches are expected to belong to one customer.
- Savings total premium must be extracted from the summary table, using the last row's second column as the primary source.
  If the last row is malformed, fall back to the maximum value in that same column.
  Only if the table cannot be parsed at all should code fall back to `annual premium * payment term`.
- Keep simplified/traditional Chinese compatibility in regexes and labels.

## Working pattern

1. Read the relevant file plus `tests/test_aia.py` before editing.
2. Prefer modifying parsing helpers or service storage logic rather than patching templates around bad data.
3. If touching deployment, preserve `.env.runtime` usage and host bind mounts for `/opt/bibhelper-data`.
4. Validate with the smallest relevant test subset first, then run the full test suite.
5. If dependencies or Docker layers change, mention that the server must rebuild the image.

## Agent authentication

For agent use, call the HTTP API directly. Do not automate the web login unless the user explicitly asks for browser-based testing.

The skill assumes these environment variables are available to the agent runtime:

- `BIBHELPER_BASE_URL`
  Default: `https://bib.obao.me`
- `BIBHELPER_API_TOKEN`
  The same bearer token used by Shortcut and API clients

Preferred runtime behavior:

1. Use `https://bib.obao.me` as the default base URL.
2. If `BIBHELPER_BASE_URL` exists in environment, prefer that value.
3. Read `BIBHELPER_API_TOKEN` from environment.
4. If `BIBHELPER_API_TOKEN` is missing, ask the user for the token before making any API request.
5. If the configured or default base URL does not respond or clearly appears wrong, ask the user for the correct service address before continuing.
6. Send requests to `POST /api/v1/process` with:

```text
Authorization: Bearer <BIBHELPER_API_TOKEN>
```

7. Use `GET /api/v1/jobs/{job_id}` and `GET /api/v1/jobs/{job_id}/download` for follow-up.

If the current runtime cannot prompt the user interactively, stop and report exactly which value is missing or unreachable:

- missing `BIBHELPER_API_TOKEN`
- incorrect `BIBHELPER_BASE_URL`
- unreachable service endpoint

Never store real tokens inside:

- `SKILL.md`
- `agents/openai.yaml`
- tracked repo files
- committed example configs

## Validation commands

Run targeted tests first when possible:

```bash
./aiahelper_pro/bin/python -m unittest tests.test_aia.PremiumExtractionTests -v
```

Then run full regression:

```bash
./aiahelper_pro/bin/python -m unittest discover -s tests -v
```

For service/deploy changes, also validate compose:

```bash
docker compose -f deploy/docker-compose.yml config
docker compose -f deploy/docker-compose.ghcr.yml config
```

## Deployment guardrails

- GHCR image: `ghcr.io/chainkhoo/bibhelper:latest`
- Server data root is `/data/bibhelper`, normally bind-mounted from `/opt/bibhelper-data`
- Upgrade command on server:

```bash
cd /opt/bibhelper && git pull origin main && docker compose -f deploy/docker-compose.ghcr.yml pull && docker compose -f deploy/docker-compose.ghcr.yml up -d
```

- If Python dependencies change, or the Dockerfile changes, the server must rebuild instead of only pulling cached state.
