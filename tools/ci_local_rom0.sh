#!/bin/bash
# Local ROM0 budget smoke (needs real ROMs, so not in GitHub CI):
#   tools/ci_local_rom0.sh /path/smb.nes [more.nes ...]
# Builds each ROM with the default APU=1 and APU_TEST_SPEED=1 and fails if it
# does not link or ROM0 headroom drops below ${ROM0_MIN:-256} bytes.
set -e
for rom in "$@"; do
  make generate ROM="$rom" APU=1 APU_TEST_SPEED=1 >/tmp/rom0-gen.log 2>&1 || { echo "GENFAIL $rom"; tail -5 /tmp/rom0-gen.log; exit 1; }
  make -B -C runtime APU_TEST_SPEED=1 >/tmp/rom0-build.log 2>&1 || { echo "LINKFAIL $rom"; grep -i -m3 error /tmp/rom0-build.log; exit 1; }
  echo -n "$(basename "$rom"): "; python3 tools/check_rom0_headroom.py runtime/build/runtime.map --min "${ROM0_MIN:-256}"
done
