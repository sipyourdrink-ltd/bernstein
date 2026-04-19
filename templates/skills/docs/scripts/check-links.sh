#!/usr/bin/env bash
# Basic markdown link check — lists broken relative links under docs/.
set -euo pipefail

fails=0
while IFS= read -r -d '' md; do
    while read -r target; do
        [[ -z "$target" ]] && continue
        # Strip anchor fragments.
        file="${target%%#*}"
        [[ -z "$file" ]] && continue
        if [[ ! -e "$(dirname "$md")/$file" ]] && [[ ! -e "$file" ]]; then
            echo "broken: $md -> $target"
            fails=1
        fi
    done < <(grep -oE '\]\(([^)]+)\)' "$md" | sed -E 's/\]\((.*)\)/\1/' | grep -vE '^https?://|^mailto:')
done < <(find docs -name '*.md' -print0)

exit "$fails"
