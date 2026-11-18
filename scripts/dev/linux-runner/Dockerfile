# Linux test runner for hosts where Application Control blocks compiled Python extensions
# (psycopg's binary driver, mypyc). See docs/development/local-runbook.md §7.1 and DC-018.
# Contains: Python 3.12, uv, and the docker CLI + compose plugin (to start the stacks that
# integration tests create through the host's Docker socket).
FROM docker:29-cli AS dockercli
FROM python:3.12-slim
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
RUN pip install --no-cache-dir -q "uv>=0.11,<0.12"
