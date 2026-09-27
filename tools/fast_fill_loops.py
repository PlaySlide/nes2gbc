#!/usr/bin/env python3
"""Run translated 6502 memory-fill self-loops as tight native loops.

Matches blocks of the form (after the X/Y/A/DE cache passes)

    nes_XXXX:                     ; [trace]  [NMI poll prologue]
        STA $PP00,Y               ; page-aligned NES RAM
        INY  (k times, k = 1/2/4/8)
        BNE  nes_XXXX

e.g. SMB's MoveAllSpritesOffscreen ($8227). Every iteration otherwise pays the
NMI poll plus Y/Z/N HRAM spills. When Y is a multiple of k the NES loop runs
exactly (256 - Y) / k iterations to Y = 0, so it is replaced by
`ld [hl],a / inc l*k / jr nz` and the final state (Y = 0, published Z/N
shadows, A = 0 with Z set, C = 0) is written once. The NMI poll still runs on
block entry; the fill (at most 256 short iterations) completes before the
next poll, like a briefly faster CPU. Otherwise (Y not a multiple of k, which
never terminates on the NES) the original polled loop runs unchanged.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


LABEL_RE = re.compile(r"(nes_[0-9A-F]{4}):")
PAGE_RE = re.compile(r"ld hl, \$(C[0-7])00")


def optimize(lines: list[str]) -> int:
    rewritten = 0
    i = 0
    n = len(lines)
    while i < n:
        m = LABEL_RE.fullmatch(code(lines[i]))
        if not m:
            i += 1
            continue
        label = m.group(1)
        # Walk to the body start: skip trace IF..ENDC and the poll prologue.
        j = i + 1
        cs: list[int] = []  # indices of code lines from here
        k = j
        while k < n and len(cs) < 40:
            c = code(lines[k])
            if c.startswith("SECTION") or (LABEL_RE.fullmatch(c) and k != i):
                break
            if c:
                cs.append(k)
            k += 1
        codes = [code(lines[x]) for x in cs]
        p = 0
        if p < len(codes) and codes[p].startswith("IF DEF(NES2GBC_PROFILE_TRACE)"):
            while p < len(codes) and codes[p] != "ENDC":
                p += 1
            p += 1
        poll = [
            "ldh a, [nes_host_vblank_pending]", "and a", "jr z, :+",
            f"ld hl, ${label[4:]}", "call nes_poll_nmi_hl", "and a",
            "jp nz, nes_nmi_entry", ":",
        ]
        if codes[p:p + len(poll)] == poll:
            p += len(poll)
        body_start = p
        head = ["ldh a, [nes_y]", "ld c, a"]
        if codes[p:p + 2] != head:
            i += 1
            continue
        p += 2
        pm = PAGE_RE.fullmatch(codes[p]) if p < len(codes) else None
        if not pm or codes[p + 1:p + 4] != ["ld l, c", "ldh a, [nes_a]", "ld [hl], a"]:
            i += 1
            continue
        page = pm.group(1)
        p += 4
        steps = 0
        while p < len(codes) and codes[p] == "inc c":
            steps += 1
            p += 1
        if steps not in (1, 2, 4, 8) or p >= len(codes) or codes[p] != "ld a, c":
            i += 1
            continue
        p += 1
        shadows = []
        while p < len(codes) and codes[p] in ("ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a"):
            shadows.append(codes[p])
            p += 1
        if codes[p:p + 3] != ["ldh [nes_y], a", "and a", f"jp nz, {label}"]:
            i += 1
            continue
        jp_line = cs[p + 2]
        first_body_line = cs[body_start]
        indent = "    "
        fast = [
            f"{indent}; native fill loop: STA ${page}00,Y / INY x{steps} / BNE (Y multiple of {steps})\n",
            f"{indent}ldh a, [nes_y]\n",
            f"{indent}ld l, a\n",
        ]
        if steps > 1:
            fast += [f"{indent}and ${steps - 1:02X}\n", f"{indent}jp nz, .fill_slow\n"]
        if steps in (4, 8):
            # Unrolled `ld l, n / ld [hl], a` table entered at entry Y/steps
            # through push/ret (touches only AF/HL): 4 M-cycles per store.
            shift = {4: 2, 8: 3}[steps]
            fast += [f"{indent}ld a, l\n"] + [f"{indent}srl a\n"] * shift + [
                f"{indent}ld l, a\n", f"{indent}add a\n", f"{indent}add l\n",
                f"{indent}add LOW(.fill_tbl)\n", f"{indent}ld l, a\n",
                f"{indent}ld a, HIGH(.fill_tbl)\n", f"{indent}adc $00\n", f"{indent}ld h, a\n",
                f"{indent}push hl\n",
                f"{indent}ld h, ${page}\n",
                f"{indent}ldh a, [nes_a]\n",
                f"{indent}ret ; enter the unrolled fill\n",
                ".fill_tbl:\n",
            ]
            for k in range(0, 256, steps):
                fast += [f"{indent}ld l, ${k:02X}\n", f"{indent}ld [hl], a\n"]
            fast += [f"{indent}xor a\n", f"{indent}ld c, a\n"]
        else:
            fast += [
                f"{indent}ld h, ${page}\n",
                f"{indent}ldh a, [nes_a]\n",
                ".fill_loop:\n",
                f"{indent}ld [hl], a\n",
            ]
            fast += [f"{indent}inc l\n"] * steps
            fast += [
                f"{indent}jr nz, .fill_loop\n",
                f"{indent}xor a\n",
                f"{indent}ld c, a\n",
            ]
        fast += [f"{indent}{s}\n" for s in shadows]
        fast += [f"{indent}ldh [nes_y], a\n", f"{indent}jp .fill_done\n", ".fill_slow:\n"]
        lines[jp_line] = lines[jp_line] + ".fill_done:\n"
        lines[first_body_line:first_body_line] = fast
        n = len(lines)
        rewritten += 1
        i = jp_line + len(fast) + 1
    return rewritten


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("asm", type=Path)
    a = ap.parse_args()
    lines = a.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    r = optimize(lines)
    a.asm.write_text("".join(lines), encoding="utf-8")
    print(f"fill-loops: rewrote {r} STA page,Y / INY / BNE self-loop(s) as native fills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
