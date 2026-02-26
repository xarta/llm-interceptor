#!/usr/bin/env bash
# Reload llm-interceptor config without restarting the container.
# The proxy picks up config.yaml changes immediately via SIGHUP.
set -euo pipefail
docker kill -s HUP llm-interceptor && echo "llm-interceptor config reloaded."
