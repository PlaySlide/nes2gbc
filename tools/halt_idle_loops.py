#!/usr/bin/env python3
"""Halt or start the next NMI on provable 6502 idle spins (NES2GBC_HALT_IDLE).

The recompiler lists provable idle spins in runtime/generated.idle.txt
(src/idle.rs): single 6502 blocks that loop to themselves and only read NES
RAM / PRG RAM, so nothing but an NMI can make them exit. This pass retargets
each such block's back-edge(s) to a small stub placed just before the block:

* a host VBlank event is pending: re-enter the block, whose head poll starts
  the NMI exactly as before;
* the game is lagging (two or more NMIs were delivered since the spin last
  ran, i.e. its main-thread frame overran host frames) and no early start is
  outstanding: start the next NMI now and owe the host VBlank that would have
  started it (nes_idle_debt, repaid by the VBlank ISR), so the long-run NMI
  rate - and the APU, which is clocked per NMI - stays at the host VBlank rate;
* otherwise HALT until the next host interrupt instead of burning cycles.

The stub only runs after one full iteration of the spin has not exited, so a
loop whose condition already holds never halts. Game logic per NES frame is
unchanged: a spin iteration has no architectural effect besides its exit test.

Opt out with NES2GBC_HALT_IDLE=0 (the Makefile then skips this pass and the
runtime support, giving byte-identical output).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

GLOBAL_LABEL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
UNCOND = re.compile(r"^\s*(jp|jr)\s+[^,]+$|^\s*(ret|reti)\s*$")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def stub(label: str) -> list[str]:
    s = label
    return [
        f"{s}_hidle: ; tools/halt_idle_loops.py: spin back-edge",
        # lagging = 2+ NMIs since the previous back-edge (main thread overran)
        "    push hl",
        "    ld hl, nes_idle_last",
        "    ld a, [nes_idle_nmis]",
        "    sub [hl]",
        "    cp 2",
        "    ld a, [nes_idle_nmis]",
        "    ld [hl], a",
        "    pop hl",
        "    jr c, .hidle_wait",
        "    ldh a, [nes_host_vblank_pending]",
        "    and a",
        f"    jr nz, {s}",
        "    ld a, [nes_idle_debt]",
        "    and a",
        "    jr nz, .hidle_halt",
        "    ld a, [nes_ppuctrl]",
        "    add a",
        "    jr nc, .hidle_halt",
        "    ld a, [nes_nmi_active]",
        "    and a",
        "    jr nz, .hidle_halt",
        "    inc a",
        "    ld [nes_idle_debt], a",
        "    ldh [nes_host_vblank_pending], a",
        f"    jr {s}",
        ".hidle_wait:",
        "    ldh a, [nes_host_vblank_pending]",
        "    and a",
        f"    jr nz, {s}",
        ".hidle_halt:",
        "    halt",
        "    nop",
        f"    jr {s}",
    ]


def main() -> int:
    asm_path = Path(sys.argv[1])
    idle_path = Path(sys.argv[2]) if len(sys.argv) > 2 else asm_path.with_suffix(".idle.txt")
    if not idle_path.exists():
        print("halt_idle_loops: no idle list, skipped")
        return 0
    wanted = {l.strip() for l in idle_path.read_text().split() if l.strip()}
    lines = asm_path.read_text().split("\n")
    heads = {}
    for i, l in enumerate(lines):
        m = GLOBAL_LABEL.match(l)
        if m and m.group(1) in wanted:
            heads[m.group(1)] = i
    done, skipped = [], []
    # Process bottom-up so earlier indices stay valid.
    for label, i in sorted(heads.items(), key=lambda kv: -kv[1]):
        # Head must start with the ordinary NMI poll (A/flags dead there).
        j = i + 1
        depth = 0
        while j < len(lines):
            c = code(lines[j])
            if c.startswith("IF "):
                depth += 1
            elif c == "ENDC" and depth:
                depth -= 1
            elif c and not depth:
                break
            j += 1
        if j >= len(lines) or code(lines[j]) != "ldh a, [nes_host_vblank_pending]":
            skipped.append((label, "no head poll"))
            continue
        # Block region: until the next global label or SECTION.
        end = i + 1
        while end < len(lines) and not GLOBAL_LABEL.match(lines[end]) and not lines[end].startswith("SECTION"):
            end += 1
        back = re.compile(rf"^(\s*)(jr|jp) ((?:z|nz|c|nc), )?{label}(\s*(?:;.*)?)$")
        hits = [k for k in range(i + 1, end) if back.match(lines[k])]
        if not hits:
            skipped.append((label, "no back-edge"))
            continue
        for k in hits:
            m = back.match(lines[k])
            lines[k] = f"{m.group(1)}jp {m.group(3) or ''}{label}_hidle ; halt_idle_loops{m.group(4) or ''}"
        # Fall-through guard: previous code line may run into the stub.
        p = i - 1
        while p >= 0 and not code(lines[p]):
            p -= 1
        prev = code(lines[p]) if p >= 0 else ""
        guard = [] if (prev.startswith("SECTION") or UNCOND.match(prev)) else [f"    jr {label} ; halt_idle_loops fall-through guard"]
        lines[i:i] = guard + stub(label)
        done.append(label)
    asm_path.write_text("\n".join(lines))
    print(f"halt_idle_loops: {len(done)} spin(s) {' '.join(sorted(done))}"
          + (f"; skipped {skipped}" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
