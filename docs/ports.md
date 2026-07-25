# Port Registry

Reference for every network port JobRadar uses or depends on, so ports can be
found, changed, and kept from colliding. All ports listed here bind to
`localhost` / `127.0.0.1` only — JobRadar is a local single-user tool and does
**not** expose any port to the public internet.

## Ports JobRadar owns

| Port | Service | Configurable? | Default bind | Notes |
|------|---------|---------------|--------------|-------|
| `8765` | JobRadar Web UI / REST API / SSE | Yes — `jobradar serve --port <N>` (`--host` for address) | `127.0.0.1` | Main entry point. Also serves the Gmail OAuth callback at `/api/email/google/callback`, so if you change this port you must update the authorized redirect URI in Google Cloud Console to match. |

## External services JobRadar connects to

These are *not* started by JobRadar — you run them separately and point
JobRadar at them via environment variables.

| Port | Service | Env var | Default | Notes |
|------|---------|---------|---------|-------|
| `8080` | llama.cpp (local LLM, OpenAI-compatible) | `LLAMACPP_BASE_URL` | `http://localhost:8080/v1` | Used by the `ollama` provider (shown as "llama.cpp (本地)" in the UI). Start the server before any local-model search/eval. |
| `1234` | LM Studio / generic OpenAI-compatible server | `LOCAL_LLM_BASE_URL` | `http://localhost:1234/v1` | Used by the `local` provider. |

## Optional monitoring stack

Only relevant if you run the local Prometheus + Grafana stack (see
`monitoring/`). Not required for JobRadar itself.

| Port | Service | Configurable? | Notes |
|------|---------|---------------|-------|
| `9090` | Prometheus | Yes (compose `ports:`) | Scrapes JobRadar's `/metrics` endpoint. |
| `3000` | Grafana | Yes (compose `ports:`) | ⚠️ **Collision risk:** `3000` is a common example port for `jobradar serve --port 3000`. Do not run JobRadar on `3000` while Grafana is up, or remap Grafana (e.g. `"3001:3000"`). |

## Conventions

- **Configurable, with a default.** Never hardcode a port at a call site;
  read it from a flag or env var with a sensible default (as above).
- **Localhost-only by default.** Nothing here is meant to be reachable from
  outside the machine. There is intentionally no reverse proxy / TLS — those
  are only needed when a service is exposed to real external users.
- **Update this table** whenever a new port is introduced.
