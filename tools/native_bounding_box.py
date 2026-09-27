#!/usr/bin/env python3
"""Native SMB BoundingBoxCore ($E29C), matched by the exact ROM bytes.

    STX $00 / $02 = a02,Y / $01 = a01,Y / PHA X*4 / TAY
    X' = ctrl,X * 4 ; box[Y'+0] = $01+T[X'],   box[Y'+2] = $01+T[X'+2]
                      box[Y'+1] = $02+T[X'+1], box[Y'+3] = $02+T[X'+3]
    PLA / TAY / LDX $00 / RTS
X' is a multiple of 4, so the four table bytes come from an embedded copy
of T[0..255]. Exit state is exact (A=Y=X*4, X, N/Z from X, C of the last
ADC, the PHA byte left in the stack page); V is not produced, so V must be
unobservable (tools/dead_overflow.py). The native code sits at the block
head and jumps to the block's own translated RTS return dispatch.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dead_overflow import v_observable  # noqa: E402

SIG = bytes.fromhex(
    "8600b9b8038502b9ad0385018a0a0a48a8bd99040a0aaaa501187dfde1"
    "99ac04a501187dffe199ae04e8c8a502187dfde199ac04a502187dffe1"
    "99ae0468a8a60060")


def main(asm, rom_path):
    rom = Path(rom_path).read_bytes()
    banks = rom[4]
    if banks not in (1, 2) or (rom[6] >> 4 | (rom[7] & 0xF0)) != 0:
        print("native-bounding-box: non-NROM, skipped"); return
    prg = rom[16:16 + banks * 16384] * (2 // banks)
    off = prg.find(SIG)
    if off < 0:
        print("native-bounding-box: 0 routine(s)"); return
    pc = 0x8000 + off
    if pc != 0xE29C:  # table/RAM operands in SIG are absolute; accept only the known placement
        print("native-bounding-box: unexpected address, skipped"); return
    p = Path(asm); text = p.read_text()
    if v_observable(text):
        print("native-bounding-box: V observable, skipped"); return
    lines = text.splitlines(keepends=True)
    try:
        idx = next(i for i, l in enumerate(lines) if l.strip() == f"nes_{pc:04X}:")
    except StopIteration:
        print("native-bounding-box: no block, skipped"); return
    if not lines[idx - 1].startswith("SECTION"):
        print("native-bounding-box: not a block head, skipped"); return
    end = next(i for i in range(idx + 1, len(lines)) if lines[i].startswith("SECTION"))
    rts = next((i for i in range(idx, end) if "; $E2DD: $60 Rts" in lines[i]), None)
    pop = next((i for i in range(rts or end, end) if "PROFILE_INC nes_profile_rts_pop" in lines[i]), None) if rts else None
    first = next((i for i in range(idx, end) if lines[i].lstrip().startswith("; $E29C:")), None)
    if pop is None or first is None or any(re.match(r"^[A-Za-z_]\w*:", lines[i]) for i in range(idx + 1, pop)):
        print("native-bounding-box: block shape unexpected, skipped"); return
    tbl = bytes(prg[0xE1FD - 0x8000 + i] for i in range(256))
    k = "nbb"
    body = f"""; native SMB BoundingBoxCore (tools/native_bounding_box.py)
ldh a, [nes_x]
ld [$C000], a
ld b, a
ldh a, [nes_y]
ld e, a
add $B8
ld l, a
ld a, $C3
adc $00
ld h, a
ld a, [hl]
ld [$C002], a
ld d, a
ld a, e
add $AD
ld l, a
ld a, $C3
adc $00
ld h, a
ld a, [hl]
ld [$C001], a
ld e, a
ld a, b
add a
add a
ld c, a
ldh a, [nes_sp]
ld l, a
ld h, $C1
ld [hl], c
ld a, b
add $99
ld l, a
ld a, $C4
adc $00
ld h, a
ld a, [hl]
add a
add a
add LOW(.{k}_tbl)
ld l, a
ld a, HIGH(.{k}_tbl)
adc $00
ld h, a
ld a, [hli]
add e
ld b, a
ld a, [hli]
add d
ld c, a
ld a, [hli]
add e
ld e, a
ld a, [hl]
add d
ld d, a
push af
ld a, [$C000]
add a
add a
add $AC
ld l, a
ld a, $C4
adc $00
ld h, a
ld [hl], b
inc hl
ld [hl], c
inc hl
ld [hl], e
inc hl
ld [hl], d
pop af
ld a, $00
adc a
ldh [nes_c_shadow], a
ld a, [$C000]
ldh [nes_x], a
ldh [nes_z_shadow], a
ldh [nes_n_shadow], a
add a
add a
ldh [nes_a], a
ldh [nes_y], a
jp .{k}_rts
.{k}_tbl:""".splitlines()
    out = [(x if x.endswith(":") else "    " + x) + "\n" for x in body]
    out += ["    db " + ", ".join(f"${v:02X}" for v in tbl[i:i + 16]) + "\n" for i in range(0, 256, 16)]
    lines[pop:pop] = [f".{k}_rts:\n"]
    lines[first:first] = out
    p.write_text("".join(lines))
    print("native-bounding-box: 1 routine(s) replaced ($E29C)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
