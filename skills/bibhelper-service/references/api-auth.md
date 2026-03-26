# BIBhelper API Auth

## Recommended auth mode

For agents, use the HTTP API with Bearer Token authentication.

Do not use the web login session flow unless you are explicitly validating the browser UI.

## Required runtime variables

- `BIBHELPER_BASE_URL`
- `BIBHELPER_API_TOKEN`

Example:

```bash
export BIBHELPER_BASE_URL="https://bib.obao.me"
export BIBHELPER_API_TOKEN="replace-with-real-token"
```

## Default behavior

- Default base URL is `https://bib.obao.me`
- If `BIBHELPER_BASE_URL` is set, use the environment value instead
- If `BIBHELPER_API_TOKEN` is missing, ask the user for it before making API calls
- If the default or configured base URL cannot be reached, ask the user for the correct address before continuing
- If the runtime cannot ask the user, stop and return a clear configuration error

## Request pattern

### Process PDFs

```bash
curl -X POST "$BIBHELPER_BASE_URL/api/v1/process" \
  -H "Authorization: Bearer $BIBHELPER_API_TOKEN" \
  -F "files=@/path/to/file1.pdf;type=application/pdf" \
  -F "files=@/path/to/file2.pdf;type=application/pdf" \
  -o result.zip
```

### Check job metadata

```bash
curl "$BIBHELPER_BASE_URL/api/v1/jobs/<job_id>" \
  -H "Authorization: Bearer $BIBHELPER_API_TOKEN"
```

### Download stored ZIP

```bash
curl "$BIBHELPER_BASE_URL/api/v1/jobs/<job_id>/download" \
  -H "Authorization: Bearer $BIBHELPER_API_TOKEN" \
  -o result.zip
```

## Where to put the token

Best practice: keep the token outside the repo and outside the skill folder.

Preferred locations:

1. Agent runtime environment variables
2. A local untracked env file loaded by the agent launcher
3. A secrets manager supported by the host agent platform

Do not place the real token in:

- this repo
- `skills/bibhelper-service/`
- tracked YAML/TOML/JSON config
- prompt examples committed to Git

## Minimal agent decision flow

1. Set `base_url = env(BIBHELPER_BASE_URL)` if present, otherwise `https://bib.obao.me`
2. Read `token = env(BIBHELPER_API_TOKEN)`
3. If `token` is missing:
   ask the user for the token
4. Probe `GET {base_url}/healthz`
5. If the probe fails or returns an unexpected result:
   ask the user for the correct base URL
6. Only after both values are confirmed, call the processing API

## Practical setups

### Codex

Set environment variables before launching Codex or before running the task shell:

```bash
export BIBHELPER_BASE_URL="https://bib.obao.me"
export BIBHELPER_API_TOKEN="replace-with-real-token"
```

### OpenClaw or other agents

Use that platform's secret or environment injection feature if it has one.
If not, create a local untracked env file on the machine that launches the agent, for example:

```bash
~/.config/bibhelper/agent.env
```

with:

```dotenv
BIBHELPER_BASE_URL=https://bib.obao.me
BIBHELPER_API_TOKEN=replace-with-real-token
```

Then source or inject that file before the agent starts.
