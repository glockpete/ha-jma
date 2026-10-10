#!/usr/bin/env bash
# Usage: scripts/set_owner.sh <github-account-or-organisation>
# Replaces the placeholder OWNER in the repository address and the code owner entry, and nowhere else:
#   custom_components/jma/manifest.json   github.com/OWNER/ha-jma  and  "@OWNER"
#   README.md                             github.com/OWNER/ha-jma, and the whole note between the lines
#                                         <!-- owner-placeholder-note:start --> and <!-- owner-placeholder-note:end -->
# Prints the files it changed. Refuses an empty or unsafe name.
set -euo pipefail

tmp=""
cleanup() { if [ -n "$tmp" ]; then rm -f "$tmp"; fi; }
trap cleanup EXIT

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <github-account-or-organisation>" >&2
  exit 2
fi
name="$1"

# The whole argument is checked at once (not line by line): letters, digits and single hyphens, 1 to 39
# characters, no leading or trailing hyphen, no line break.
valid='^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?$'
if [[ "$name" == *$'\n'* ]] || [[ "$name" == *$'\r'* ]] || [[ "$name" == *--* ]] || ! [[ "$name" =~ $valid ]]; then
  echo "refused: not a valid GitHub account name" >&2
  exit 2
fi
if [ "$name" = "OWNER" ]; then
  echo "refused: 'OWNER' is the placeholder itself" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
files=("custom_components/jma/manifest.json" "README.md")

for f in "${files[@]}"; do
  path="$root/$f"
  [ -f "$path" ] || { echo "missing: $f" >&2; exit 1; }
  if grep -Eq 'github\.com/OWNER/ha-jma|"@OWNER"|owner-placeholder-note:start' "$path"; then
    tmp="$(mktemp)"
    awk -v name="$name" '
      /<!-- owner-placeholder-note:start -->/ { skip = 1; next }
      /<!-- owner-placeholder-note:end -->/   { skip = 0; blank = 1; next }
      skip { next }
      blank && /^$/ { blank = 0; next }
      {
        blank = 0
        gsub(/github\.com\/OWNER\/ha-jma/, "github.com/" name "/ha-jma")
        gsub(/"@OWNER"/, "\"@" name "\"")
        print
      }
    ' "$path" > "$tmp"
    cat "$tmp" > "$path"
    rm -f "$tmp"
    tmp=""
    echo "changed: $f"
  fi
done
