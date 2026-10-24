#!/bin/bash
# check_byte_identical.sh {ref|check} MANIFEST HASHFILE
#
# Builds every ROM/env combination listed in MANIFEST from this worktree and
# records (ref) or compares (check) the sha256 of runtime/generated.asm and
# the final runtime/build/runtime.gbc. Used to prove refactors of the
# translator (docs/IR_REWRITE.md) keep output byte-identical.
#
# MANIFEST lines: NAME ROM_PATH [ENV=VAL ...] [-- MAKEVAR=VAL ...]; '#' comments.
# Needs rgbds on PATH (RGBDS_PATH may prepend a dir). CARGO_TARGET_DIR honoured.
set -u
MODE=$1; MAN=$2; HASHES=$3
ROOT=$(cd "$(dirname "$0")/.." && pwd)
[ -n "${RGBDS_PATH:-}" ] && export PATH=$RGBDS_PATH:$PATH
export PYTHONDONTWRITEBYTECODE=1
TMP=$(mktemp); fail=0; n=0
cd "$ROOT"
while read -r name rom rest; do
  [ -z "${name:-}" ] && continue; case $name in \#*) continue;; esac
  envs=(); mvars=(); seen=0
  for t in $rest; do
    if [ "$t" = "--" ]; then seen=1; elif [ $seen = 0 ]; then envs+=("$t"); else mvars+=("$t"); fi
  done
  if ! env "${envs[@]}" make generate ROM="$rom" "${mvars[@]}" > /tmp/cbi_gen_$name.log 2>&1; then
    echo "GENFAIL $name (see /tmp/cbi_gen_$name.log)"; fail=1; continue
  fi
  if ! env "${envs[@]}" make -B -C runtime "${mvars[@]}" > /tmp/cbi_build_$name.log 2>&1; then
    echo "BUILDFAIL $name (see /tmp/cbi_build_$name.log)"; fail=1; continue
  fi
  a=$(sha256sum runtime/generated.asm | cut -c1-16); g=$(sha256sum runtime/build/runtime.gbc | cut -c1-16)
  echo "$name $a $g" >> "$TMP"; n=$((n+1))
done < "$MAN"
git -C "$ROOT" checkout -q -- runtime/build runtime/generated.asm runtime/generated.chr.bin \
  runtime/generated.chr.gbc.bin runtime/generated.prg.bin runtime/generated_config.inc 2>/dev/null || true
if [ "$MODE" = ref ]; then
  cp "$TMP" "$HASHES"; echo "recorded $n builds -> $HASHES"
else
  if diff <(sort "$HASHES") <(sort "$TMP") > /tmp/cbi_diff.txt; then
    echo "BYTE-IDENTICAL: $n builds match $HASHES"
  else
    echo "MISMATCH:"; cat /tmp/cbi_diff.txt; fail=1
  fi
fi
rm -f "$TMP"; exit $fail
