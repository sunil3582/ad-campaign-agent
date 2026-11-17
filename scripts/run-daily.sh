#!/bin/sh
set -eu
# Call with /bin/sh from cron. Compose reads the repository's .env file.
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
if [ -x .venv/bin/guardian ]; then
    exec .venv/bin/guardian run-daily
fi
exec docker compose run --rm app run-daily

