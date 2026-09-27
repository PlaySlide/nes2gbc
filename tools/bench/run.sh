#!/bin/bash
# One command: build SMB with the full pipeline and print NES-frame speed metrics.
#   tools/bench/run.sh [--profile out.npy --profwin play|title] [--shots dir] [--json]
# Env: ROM (default /workspace/roms/smb.nes), RGBDS_BIN (dir with rgbasm/rgblink/rgbfix).
set -e
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../.." && pwd)
ROM=${ROM:-/workspace/roms/smb.nes}
[ -n "$RGBDS_BIN" ] && export PATH="$RGBDS_BIN:$PATH"
export PYTHONDONTWRITEBYTECODE=1
cd "$REPO"
make generate ROM="$ROM" > /tmp/nes2gbc-generate.log 2>&1 || { tail -20 /tmp/nes2gbc-generate.log; exit 1; }
make -B -C runtime > /tmp/nes2gbc-rgbds.log 2>&1 || { tail -20 /tmp/nes2gbc-rgbds.log; exit 1; }
"$HERE/.pyboy/venv/bin/python" "$HERE/bench.py" --rom "$REPO/runtime/build/runtime.gbc" "$@" 2>&1 | grep -v -e UserWarning -e "^$"
