# Blockers

| ID | Opened | Affects | Description | Status / resolution |
|---|---|---|---|---|
| BLK-001 | 2026-11-16 | All Docker-based tasks | Docker Desktop crashed on start because stale AF_UNIX socket stubs (`%LOCALAPPDATA%\Docker\run\dockerInference`, `%LOCALAPPDATA%\docker-secrets-engine\engine.sock`, dated 2026-10-10) could not be removed (Windows error 1920). | **Resolved 2026-11-16.** Renamed the stale folders to `run.stale-20261116`, `run.stale-20261116b` and `docker-secrets-engine.stale-20261116`; nothing deleted. Engine 29.3.1 up (24 CPU, ~12.4 GB). **Recurrence risk:** a force-killed Docker Desktop leaves new stubs. Quit Docker from the tray, never kill the process. The runbook documents the fix. |
| BLK-002 | 2026-11-16 | M00.03.04 | No local inference runtime is installed (Ollama is absent). The benchmark runs `ollama/ollama` in Docker. GPU passthrough to Docker (RTX 5050 Laptop, 8 GB) is unverified; if it's unavailable, the benchmark runs on CPU and records that. | Open — handled inside M00.03.04 |
