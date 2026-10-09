#!/usr/bin/env bash
set -euo pipefail

project_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if ! command -v uv >/dev/null 2>&1; then
  echo "start_ui.sh: uv is required to install the project dependencies" >&2
  exit 1
fi

# Reuse a compatible project interpreter across user accounts. Container-wide
# uv settings can otherwise restrict root to its own managed installations.
python_bin="$project_dir/.venv/bin/python"
python_request="3.14+gil"
if [[ -x "$python_bin" ]] && "$python_bin" - <<'PYTHON'
import sys
import sysconfig
raise SystemExit(sys.version_info[:2] != (3, 14) or bool(sysconfig.get_config_var("Py_GIL_DISABLED")))
PYTHON
then
  python_request="$python_bin"
fi

# The local dbzero wheel requires regular CPython 3.14. Preserve installed
# development/browser tools when syncing the UI dependencies.
env -u UV_MANAGED_PYTHON -u UV_PYTHON \
  uv sync --project "$project_dir" --extra ui --inexact --python "$python_request"

if ! "$python_bin" - <<'PYTHON'
import dbzero
assert hasattr(dbzero, "enum")
PYTHON
then
  echo "start_ui.sh: the local dbzero build is missing or incompatible" >&2
  exit 1
fi

port=8001
forwarded=()
while (($#)); do
  case "$1" in
    --port)
      if (($# < 2)); then
        echo "start_ui.sh: --port requires a value" >&2
        exit 2
      fi
      port=$2
      shift 2
      ;;
    --port=*)
      port=${1#*=}
      shift
      ;;
    *)
      forwarded+=("$1")
      shift
      ;;
  esac
done

exec "$python_bin" -u -m trader_api.web_ui.main "${forwarded[@]}" --port "$port"
