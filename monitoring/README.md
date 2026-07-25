# Local Monitoring Stack

A local Prometheus + Grafana stack that scrapes JobRadar's `/metrics` endpoint
and visualises it. Everything runs on `localhost`; nothing is exposed publicly.

```
JobRadar (host, :8765)  ──/metrics──>  Prometheus (:9090)  ──query──>  Grafana (:3000)
```

## Prerequisites

- Docker Desktop running.
- JobRadar running on the host so there is something to scrape:
  ```bash
  uv run jobradar serve --no-browser
  ```

## Run

```bash
cd monitoring
docker compose up -d
```

- Prometheus → http://localhost:9090
- Grafana    → http://localhost:3000  (login `admin` / `admin`)

## Verify

1. **Prometheus is scraping JobRadar:** open
   http://localhost:9090/targets — the `jobradar` target should be **UP**.
2. **Data flows into Grafana:** open Grafana → *Explore* → pick the
   pre-provisioned *Prometheus* data source → query e.g.
   `jobradar_llm_tokens_total` and confirm a value comes back.

## Exposed metrics

All counters, served by JobRadar at `/metrics`:

| Metric | Meaning |
|--------|---------|
| `jobradar_searches_total` | Total searches run |
| `jobradar_jobs_found_total` | Total jobs found across all searches |
| `jobradar_llm_tokens_total{direction="in"\|"out"}` | Cumulative LLM tokens |
| `jobradar_search_seconds_total` | Cumulative time spent searching |

## Stop

```bash
docker compose down          # stop containers, keep Grafana dashboards
docker compose down -v       # also delete the grafana-data volume
```

## Notes

- **Networking:** Grafana reaches Prometheus by service name (`prometheus:9090`)
  because they share the compose network; Prometheus reaches JobRadar via
  `host.docker.internal:8765` because JobRadar runs on the host, not in a
  container.
- **Port collision:** Grafana uses `3000`. Do not run `jobradar serve --port 3000`
  while Grafana is up (see `../docs/ports.md`).
