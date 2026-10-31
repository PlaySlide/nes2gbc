#!/usr/bin/env python3
"""Run translated 6502 strided store self-loops as tight native loops.

Matches self-loop blocks whose 6502 body is exactly

    LDA #v / STA $PP00,X / TXA / CLC / ADC #s / TAX / DEC zp / BNE self
    (or SEC / SBC #s instead of CLC / ADC #s)

e.g. Rush'n Attack's unused-sprite clear at $E6BE (X steps by $C4 through
the OAM buffer). Each iteration otherwise pays the NMI poll, the 6502 V/C
publication and the X/A/Z/N spills. The NMI poll still runs on block entry;
the loop body then runs `count` (0 = 256) native iterations without polls,
like a briefly faster CPU, and the final 6502 state (A = X = last sum, C and
V of the last ADC/SBC, zp counter = 0, Z = 1, N = 0) is published once.

Opt-out: NES2GBC_STRIDED_FILL=0.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

LABEL_RE = re.compile(r"^(nes_[0-9A-F]{4}):$")
INSN_RE = re.compile(r"; \$([0-9A-F]{4}): \$([0-9A-F]{2}) ([A-Za-z]+) ([A-Za-z]+)")
HEX = r"\$([0-9A-F]{2})"


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def optimize(lines: list[str]) -> int:
    done = 0
    i = 0
    while i < len(lines):
        m = LABEL_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        label = m.group(1)
        # block extent: up to the next global label / SECTION
        j = i + 1
        while j < len(lines):
            c = code(lines[j])
            if LABEL_RE.match(c) or c.startswith("SECTION"):
                break
            j += 1
        insns = []  # (line index, mnemonic, mode)
        for k in range(i + 1, j):
            mm = INSN_RE.search(lines[k])
            if mm:
                insns.append((k, mm.group(3), mm.group(4)))
        mn = [(a, b) for _, a, b in insns]
        add = [("Clc", "Implied"), ("Adc", "Immediate")]
        sub = [("Sec", "Implied"), ("Sbc", "Immediate")]
        shape = lambda mid: [("Lda", "Immediate"), ("Sta", "AbsoluteX"), ("Txa", "Implied")] + mid + [
            ("Tax", "Implied"), ("Dec", "ZeroPage"), ("Bne", "Relative")]
        if mn not in (shape(add), shape(sub)):
            i = j
            continue
        is_sub = mn == shape(sub)
        seg = lambda n: [code(lines[k]) for k in range(insns[n][0] + 1, insns[n + 1][0] if n + 1 < len(insns) else j)]
        lda, sta, adc, dec, bne = seg(0), seg(1), seg(4), seg(6), seg(7)
        v = next((re.fullmatch(r"ld a, " + HEX, c) for c in lda if re.fullmatch(r"ld a, " + HEX, c)), None)
        page = next((re.fullmatch(r"ld hl, \$(C[0-7])00", c) for c in sta if re.fullmatch(r"ld hl, \$(C[0-7])00", c)), None)
        st = None
        for c in adc:
            st = re.fullmatch(r"ld e, " + HEX, c) or re.fullmatch(r"(?:add|sub) " + HEX, c)
            if st:
                break
        zp = next((re.fullmatch(r"ld hl, \$C0([0-9A-F]{2})", c) for c in dec if re.fullmatch(r"ld hl, \$C0([0-9A-F]{2})", c)), None)
        jp = [k for k in range(insns[7][0], j) if code(lines[k]) == f"jp nz, {label}"]
        if not (v and page and st and zp and "dec [hl]" in dec and len(jp) == 1):
            i = j
            continue
        if page.group(1) == "C0" or "ld [hl], a" not in sta:
            i = j  # counter could alias the stored page
            continue
        val, pg, s, z = v.group(1), page.group(1), st.group(1), zp.group(1)
        if is_sub:
            s = f"{(-int(s, 16)) & 0xFF:02X}"  # unused: SBC handled below
        start = insns[0][0]
        end = jp[0]
        ind = "    "
        op = "sub" if is_sub else "add"
        sv = st.group(1)
        new = [
            f"{ind}; native strided fill: LDA #${val} / STA ${pg}00,X / X {'-' if is_sub else '+'}= ${sv} / DEC ${z} / BNE (NES2GBC_STRIDED_FILL)\n",
            f"{ind}ld a, [$C0{z}]\n",
            f"{ind}ld c, a\n",
            f"{ind}ldh a, [nes_x]\n",
            f"{ind}ld b, a\n",
            f"{ind}ld h, ${pg}\n",
            f"{ind}ld e, ${sv}\n",
            ".sfill_loop:\n",
            f"{ind}ld l, b\n",
            f"{ind}ld [hl], ${val}\n",
            f"{ind}ld a, b\n",
            f"{ind}ld d, a\n",
            f"{ind}{op} e\n",
            f"{ind}ld b, a\n",
            f"{ind}dec c ; host C kept from the {op}\n",
            f"{ind}jr nz, .sfill_loop\n",
            # 6502 C: ADC carry = host carry; SBC carry = NOT host borrow
            f"{ind}sbc a\n" if not is_sub else f"{ind}ccf\n",
        ]
        if is_sub:
            new += [f"{ind}sbc a\n"]
        new += [
            f"{ind}ldh [nes_c_shadow], a\n",
            f"{ind}ldh a, [nes_p]\n",
            f"{ind}and $BF\n",
            f"{ind}ldh [nes_p], a\n",
            # V: ADC (d^e^$80)&(d^r)&$80 ; SBC (d^e)&(d^r)&$80
            f"{ind}ld a, d\n",
            f"{ind}xor e\n",
        ]
        if not is_sub:
            new += [f"{ind}cpl\n"]
        new += [
            f"{ind}ld h, a\n",
            f"{ind}ld a, d\n",
            f"{ind}xor b\n",
            f"{ind}and h\n",
            f"{ind}and $80\n",
            f"{ind}jr z, .sfill_nov\n",
            f"{ind}ldh a, [nes_p]\n",
            f"{ind}or $40\n",
            f"{ind}ldh [nes_p], a\n",
            ".sfill_nov:\n",
            f"{ind}xor a\n",
            f"{ind}ld [$C0{z}], a\n",
            f"{ind}ldh [nes_z_shadow], a\n",
            f"{ind}ldh [nes_n_shadow], a\n",
            f"{ind}ld l, b\n",
            f"{ind}ld a, b\n",
            f"{ind}ldh [nes_a], a\n",
            f"{ind}ldh [nes_x], a\n",
            f"{ind}ld hl, $C0{z}\n",
            f"{ind}xor a ; Z set like the exiting BNE\n",
        ]
        lines[start:end + 1] = new
        done += 1
        i = start + len(new)
    return done


def main() -> int:
    asm = Path(sys.argv[1])
    if os.environ.get("NES2GBC_STRIDED_FILL", "1") == "0":
        print("strided-fill: disabled (NES2GBC_STRIDED_FILL=0)")
        return 0
    lines = asm.read_text(encoding="utf-8").splitlines(keepends=True)
    r = optimize(lines)
    if r:
        asm.write_text("".join(lines), encoding="utf-8")
    print(f"strided-fill: rewrote {r} strided store self-loop(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
