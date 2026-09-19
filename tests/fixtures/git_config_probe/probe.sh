#!/bin/sh
# Records one invocation of a repository hook or filter driver.
#
# Every hook under hooks/ and the filter driver are this script. It appends one
# line -- when, which entry point, on which machine, as which user, from which
# directory -- to git-config-probe.log in TMPDIR and in HOME, then behaves as a
# no-op hook (exit 0) or a pass-through filter (copies stdin to stdout).
#
# The log is the evidence. A record written by the process that configured the
# repository is expected; a record written anywhere else means some other
# tooling executed configuration it found inside the checkout.
name=${PROBE_NAME:-${0##*/}}
line="$(date -u +%Y-%m-%dT%H:%M:%SZ) entry=$name host=$(hostname 2>/dev/null || echo ?) uid=$(id -u) pid=$$ cwd=$PWD"
for d in "${TMPDIR:-/tmp}" "${HOME:-}"; do
  [ -n "$d" ] && [ -d "$d" ] && printf '%s\n' "$line" >> "$d/git-config-probe.log" 2>/dev/null
done
case "$name" in
  filter) cat ;;
esac
exit 0
