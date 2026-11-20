#!/bin/sh
# npm invokes the script shell in the package directory for each lifecycle hook.
set -eu
: "${ADAPTER_LIFECYCLE_PATHS:?}"
if printf '%s\n' "$ADAPTER_LIFECYCLE_PATHS" | grep -Fxq -- "$PWD"; then
  unset npm_config_script_shell
  exec /bin/sh "$@"
fi
