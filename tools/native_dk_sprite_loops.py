#!/usr/bin/env python3
"""Run two Donkey Kong OAM-buffer loops natively ($F11E and $F139).

ROM-byte matched (NROM):

  F11E: LDA $02 / LDX $08 / LDY #1 /
  F124: STA ($04),Y / CLC / ADC #1 / INY / PHA / LDA ($04),Y / AND #$3F /
        STA ($04),Y / PLA / INY / INY / INY / DEX / BNE F124 / RTS
  F139: LDY #0 / F13B: LDX $06 / LDA $01 / STA $09 /
  F141: LDA $09 / STA ($04),Y / CLC / ADC #8 / STA $09 / INY x3 / LDA $00 /
        STA ($04),Y / INY / DEX / BNE F141 / LDA $00 / CLC / ADC #8 /
        STA $00 / DEC $07 / BNE F13B / RTS

At each routine's canonical entry block, GB code handles the common case:
($04) points into internal RAM outside pages $00/$01, the counts are non-zero and every access
stays inside the pointer's page (otherwise the unchanged translated code
runs). It writes the same bytes (including the PHA byte at $0100+SP and
$00/$07/$09), materializes A/X/Y/Z/N/C (V is not reproduced, so V must be
unobservable) and enters the routine's own translated RTS dispatch.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dead_overflow import v_observable  # noqa: E402

PC_COMMENT = re.compile(r"^\s*; \$([0-9A-F]{4}): \$[0-9A-F]{2} ")
GLABEL = re.compile(r"^[A-Za-z_]\w*:")


def code(l):
    return l.split(";", 1)[0].strip()


def ptr_setup(P, span_reg):
    # HL = RAM address of ($04); slow unless page-local for offsets 0..span
    return [
        "    ld a, [$C005]",
        "    cp $20",
        f"    jp nc, .nsl_slow_{P}",
        "    cp $02",
        f"    jp c, .nsl_slow_{P} ; page 0/1 could alias the counters, pointer or stack",
        "    and $07",
        "    or $C0",
        "    ld h, a",
        "    ld a, [$C004]",
        "    ld l, a",
        f"    add {span_reg}",
        f"    jp c, .nsl_slow_{P}",
    ]


def body_f11e(P, rts):
    return [
        f"    ; ${P}-$F138: OAM tile/attribute loop run natively (tools/native_dk_sprite_loops.py)",
        "    ld a, [$C008]",
        "    and a",
        f"    jp z, .nsl_slow_{P}",
        "    cp $40",
        f"    jp nc, .nsl_slow_{P}",
        "    ld b, a",
        "    add a",
        "    add a",
        "    sub $02",
        "    ld c, a ; last offset 4X-2",
        *ptr_setup(P, "c"),
        "    ld a, b",
        "    add a",
        "    add a",
        "    inc a",
        "    ldh [nes_y], a ; Y = 1 + 4X",
        "    inc l",
        "    ld a, [$C002]",
        f".nsl_loop_{P}:",
        "    ld [hli], a ; STA ($04),Y",
        "    inc a ; CLC / ADC #1",
        "    ld e, a",
        "    ld a, [hl]",
        "    and $3F",
        "    ld [hl], a",
        "    ld a, e",
        "    inc l",
        "    inc l",
        "    inc l",
        "    dec b",
        f"    jr nz, .nsl_loop_{P}",
        "    ldh [nes_a], a",
        "    ld e, a",
        "    sub $01",
        "    sbc a ; C of the last ADC #1: result wrapped to 0",
        "    ldh [nes_c_shadow], a",
        "    ldh a, [nes_sp]",
        "    ld l, a",
        "    ld h, $C1",
        "    ld [hl], e ; last PHA byte",
        "    xor a",
        "    ldh [nes_x], a",
        "    ldh [nes_z_shadow], a ; DEX -> 0",
        "    ldh [nes_n_shadow], a",
        f"    jp {rts}",
        f".nsl_slow_{P}:",
    ]


def body_f139(P, rts):
    return [
        f"    ; ${P}-$F160: OAM position grid run natively (tools/native_dk_sprite_loops.py)",
        "    ld a, [$C006]",
        "    and a",
        f"    jp z, .nsl_slow_{P}",
        "    ld b, a ; columns",
        "    ld a, [$C007]",
        "    and a",
        f"    jp z, .nsl_slow_{P}",
        "    ld c, a ; rows",
        "    ld e, a",
        "    xor a",
        f".nsl_mul_{P}:",
        "    add b",
        f"    jp c, .nsl_slow_{P}",
        "    dec e",
        f"    jr nz, .nsl_mul_{P}",
        "    cp $41",
        f"    jp nc, .nsl_slow_{P}",
        "    add a",
        "    add a",
        "    ld d, a ; 4*R*C (0 = 256)",
        "    dec a",
        "    ld e, a ; last offset",
        *ptr_setup(P, "e"),
        "    ld a, d",
        "    ldh [nes_y], a",
        f".nsl_row_{P}:",
        "    ld a, [$C001]",
        "    ld e, a ; $09",
        "    ld a, [$C000]",
        "    ld d, a",
        "    push bc",
        f".nsl_col_{P}:",
        "    ld a, e",
        "    ld [hli], a ; STA ($04),Y  ($09)",
        "    add $08",
        "    ld e, a",
        "    inc l",
        "    inc l",
        "    ld a, d",
        "    ld [hli], a ; STA ($04),Y+3  ($00)",
        "    dec b",
        f"    jr nz, .nsl_col_{P}",
        "    pop bc",
        "    ld a, e",
        "    ld [$C009], a",
        "    ld a, d",
        "    add $08",
        "    ld [$C000], a",
        "    dec c",
        f"    jr nz, .nsl_row_{P}",
        "    ldh [nes_a], a",
        "    sbc a ; C of the last ADC #8",
        "    ldh [nes_c_shadow], a",
        "    xor a",
        "    ld [$C007], a",
        "    ldh [nes_x], a",
        "    ldh [nes_z_shadow], a ; DEC $07 -> 0",
        "    ldh [nes_n_shadow], a",
        f"    jp {rts}",
        f".nsl_slow_{P}:",
    ]


ROUTINES = [
    (0xF11E, "a502a608a0019104186901c848b104293f910468c8c8c8cad0ec60", body_f11e),
    (0xF139, "a000a606a5018509a50991041869088509c8c8c8a5009104c8cad0eca5001869088500c607d0db60", body_f139),
]


def bank_of(lines, i):
    while i > 0 and not lines[i].startswith("SECTION"):
        i -= 1
    m = re.search(r"BANK\[(\d+)\]", lines[i])
    return m.group(1) if m else None


def main(asm, rom_path):
    rom = Path(rom_path).read_bytes()
    banks = rom[4]
    if banks not in (1, 2) or (rom[6] >> 4 | (rom[7] & 0xF0)) != 0 or rom[6] & 4:
        print("native-dk-sprite-loops: non-NROM, skipped")
        return
    prg = rom[16:16 + banks * 16384]
    read = lambda a, n: bytes(prg[(a - 0x8000 + i) % len(prg)] for i in range(n))
    todo = [(pc, bytes.fromhex(h), f) for pc, h, f in ROUTINES if read(pc, len(bytes.fromhex(h))) == bytes.fromhex(h)]
    if not todo:
        print("native-dk-sprite-loops: routines not present")
        return
    p = Path(asm)
    text = p.read_text()
    if v_observable(text):
        print("native-dk-sprite-loops: V observable; skipped")
        return
    lines = text.splitlines(keepends=True)
    n = 0
    for pc, pat, f in todo:
        P = f"{pc:04X}"
        ent = next((i for i, l in enumerate(lines) if code(l) == f"nes_{P}:"), None)
        if ent is None:
            print(f"native-dk-sprite-loops: no canonical block ${P}; skipped")
            continue
        j = ent + 1
        if code(lines[j]).startswith("IF DEF(NES2GBC_PROFILE_TRACE)"):
            while code(lines[j]) != "ENDC":
                j += 1
            j += 1
        m = PC_COMMENT.match(lines[j])
        if not m or int(m.group(1), 16) != pc:
            print(f"native-dk-sprite-loops: unexpected entry shape ${P}; skipped")
            continue
        rts_pc = pc + len(pat) - 1
        r = next((i for i, l in enumerate(lines) if (mm := PC_COMMENT.match(l)) and int(mm.group(1), 16) == rts_pc), None)
        k = r
        while k is not None and k < len(lines) and code(lines[k]) != "PROFILE_INC nes_profile_rts_pop":
            if lines[k].startswith("SECTION") or GLABEL.match(code(lines[k])):
                k = None
                break
            k += 1
        if k is None or k >= len(lines):
            print(f"native-dk-sprite-loops: no RTS tail ${P}; skipped")
            continue
        nxt = next(code(x) for x in lines[k + 1:] if code(x))
        if nxt != "ldh a, [nes_sp]" or bank_of(lines, k) != bank_of(lines, ent):
            print(f"native-dk-sprite-loops: unusable RTS tail ${P}; skipped")
            continue
        g = k
        while not GLABEL.match(code(lines[g])):
            g -= 1
        rts = f"{code(lines[g]).split(':')[0]}.nsl_rts_{P}"
        b = [s + "\n" for s in f(P, rts)]
        lab = [f".nsl_rts_{P}: ; native loop return (state in HRAM)\n"]
        if k > j:
            lines[k:k] = lab
            lines[j:j] = b
        else:
            lines[j:j] = b
            lines[k + len(b):k + len(b)] = lab
        n += 1
    p.write_text("".join(lines))
    print(f"native-dk-sprite-loops: {n} routine{'s' if n != 1 else ''} run natively")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
