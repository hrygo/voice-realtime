#!/usr/bin/env bash
set -euo pipefail

exec uv run sona-meeting-mock "$@"
