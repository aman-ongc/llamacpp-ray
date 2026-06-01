# Handover

## Current State

The platform is running end to end on WS-11 with a four-node Ray cluster and a working gateway path.

### Live Services

- Docker stack is up on WS-11: Postgres, Redis, Gateway, Prometheus, Grafana, NGINX.
- Ray head is running on WS-11.
- Ray Serve is deployed with `4` replicas.
- llama.cpp is running on the controller and on the worker nodes.
- Gateway health, Ray Serve health, NGINX health, and Grafana health all pass.

### Ray Cluster

Active GPU nodes:
- WS-11 `10.208.211.62`
- WS-03 `10.208.211.54`
- WS-08 `10.208.211.59`
- WS-13 `10.208.211.64`

The corrected WS-13 address is `10.208.211.64` and it is now joined to Ray.

### Validation

- `pytest tests -q --tb=short` passes: `19 passed`
- `python -m json.tool infra/grafana/dashboards/llm_platform.json` passes
- `curl http://10.208.211.62:8001/health` returns OK
- `curl http://10.208.211.62:18000/health` returns OK
- `curl http://10.208.211.62:10080/health` returns OK
- `curl http://10.208.211.59:8080/health` returns OK
- `curl http://10.208.211.64:8080/health` returns OK

## Important Code Changes

- `gateway/routers/chat.py`
  - injects `/no_think` into chat payloads when needed
  - normalizes completion responses so empty `content` can fall back to reasoning text
- `gateway/config.py`
  - tracks the corrected worker IP set
  - exposes `serve_replicas=4`
- `worker/ray_worker.py`
  - Serve deployment scales to the configured replica count
- `infra/grafana/provisioning/datasources/prometheus.yml`
  - adds deterministic Prometheus UID and a Postgres datasource
- `infra/grafana/dashboards/llm_platform.json`
  - adds per-user / per-key usage panel
- `docker-compose.yml`
  - Grafana depends on Postgres
  - Postgres health check is tighter
- `scripts/start_linux_worker.sh`
  - used to bootstrap WS-03, WS-08, and WS-13 into Ray
- `scripts/start_node_exporter.sh`
  - installed on the controller and worker hosts
- `scripts/setup_ws11_portproxy.ps1`
  - created localhost portproxy rules for WS-11 browser access

## Remaining Work

- Monitoring rollout still needs a final scrape verification pass for node-exporter on every node.
- If the Windows/WSL network state changes again, re-run the portproxy helper on WS-11.

## Useful Commands

Run tests:
```bash
wsl -d Ubuntu-24.04 --exec bash -lc "source /mnt/d/VirtualEnvironments/llm-platform/bin/activate && cd /home/administrator/projects/llm-inference-service && NO_PROXY='*' pytest tests -q --tb=short"
```

Check Ray:
```bash
wsl -d Ubuntu-24.04 --exec bash -lc "source /mnt/d/VirtualEnvironments/llm-platform/bin/activate && cd /home/administrator/projects/llm-inference-service && ray status"
```

Deploy Serve:
```bash
wsl -d Ubuntu-24.04 --exec bash -lc "cd /home/administrator/projects/llm-inference-service && source /mnt/d/VirtualEnvironments/llm-platform/bin/activate && python scripts/deploy_serve.py"
```

## Notes

- Repository has no git history.
- `ADMIN_SECRET` is no longer `changeme`; it is set in `.env`.
- WS-13 was initially misread as `10.208.211.66`; the correct IP is `10.208.211.64`.
