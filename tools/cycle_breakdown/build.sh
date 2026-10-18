#!/bin/bash
# build.sh ROM OUTDIR/NAME [make vars]: build ROM at the current worktree, then
# rebuild with per-instruction labels (annotate_asm.py) and check the
# annotated ROM is byte-identical. Produces NAME.{gbc,sym,cfg.txt,lmap.tsv}.
set -e
D=$(cd "$(dirname "$0")/../.." && pwd); ROM=$1; N=$2; shift 2
for v in $(env | grep -o "^NES2GBC_[A-Z0-9_]*"); do unset $v; done
cd "$D"
make generate ROM="$ROM" "$@" > "$N.gen.log" 2>&1
make -B -C runtime "$@" > "$N.b0.log" 2>&1
cp runtime/build/runtime.gbc "$N.plain.gbc"
cp runtime/generated.asm "$N.asm"
cp runtime/generated.cfg.txt "$N.cfg.txt" 2>/dev/null || true
python3 tools/cycle_breakdown/annotate_asm.py runtime/generated.asm "$N"
make -B -C runtime "$@" > "$N.b1.log" 2>&1
cp runtime/build/runtime.gbc "$N.gbc"; cp runtime/build/runtime.sym "$N.sym"
cp "$N.asm" runtime/generated.asm
cmp "$N.gbc" "$N.plain.gbc" && echo "built $N (annotated ROM byte-identical)"
