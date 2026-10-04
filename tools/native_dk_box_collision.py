#!/usr/bin/env python3
"""Run Donkey Kong's bounding-box collision routine natively.

Matches (by ROM bytes, NROM) the routine at $EFF5 (entry A = mode):

  EFF5: STA $0C / TXA / PHA / TYA / PHA / LDY #0 / LDA $0C / BNE F018
  F001: JSR F063 / STA $46 / JSR F069 / STA $47 / JSR F062 / STA $48 /
        JSR F069 / STA $49 / JMP F059
  F018: JSR F063 / STA $4A / ... / STA $4D / $9C=$4A-$46 / $9D=$4B-$47 /
        CMP chain $49>=$4B, $4D>=$47, $4C>=$46, $48>=$4A -> A=1 else A=0
  F059: STA $0C / PLA / TAY / PLA / TAX / LDA $0C / RTS
  F062: INY / F063: LDA ($02),Y / CLC / ADC $00 / RTS
  F069: INY / LDA ($02),Y / CLC / ADC $01 / RTS

At `nes_EFF5_trace` (A resident) GB code reads the four box bytes through
($02) with one address translation (internal RAM or the WRAM PRG cache; a
pointer at $2000-$7FFF or within 3 bytes of a page end falls back to the
unchanged translated code), writes $46-$49 (mode 0) or $4A-$4D, $9C, $9D and
the result like the original, leaves the same page-$01 bytes (pushed X, Y and
the last inner JSR's return address), materializes A/Z/N/C (V is not
reproduced, so V must be unobservable) and enters the routine's own
translated RTS return dispatch. Runs after the peephole passes, so the code
it splices into is final.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dead_overflow import v_observable  # noqa: E402

ENTRY = 0xEFF5
PAT = bytes.fromhex(
    "850c8a4898" "48a000a50cd017" "2063f08546" "2069f08547" "2062f08548" "2069f08549" "4c59f0"
    "2063f0854a" "2069f0854b" "2062f0854c" "2069f0854d"
    "a54a38e546859c" "a54b38e547859d"
    "a549c54b9017" "a54dc5479011" "a54cc546900b" "a548c54a9005" "a9014c59f0" "a900"
    "850c68a868aaa50c60" "c8" "b1021865" "0060" "c8b10218650160")
PC_COMMENT = re.compile(r"^\s*; \$([0-9A-F]{4}): \$[0-9A-F]{2} ")


def code(l):
    return l.split(";", 1)[0].strip()


def prg_reader(rom):
    banks = rom[4]
    trainer = 512 if rom[6] & 4 else 0
    prg = rom[16 + trainer:16 + trainer + banks * 16384]
    if banks not in (1, 2) or (rom[6] >> 4 | (rom[7] & 0xF0)) != 0:
        return None
    n = len(prg)
    return lambda pc, k: bytes(prg[(pc - 0x8000 + i) % n] for i in range(k))


def section_of(lines, i):
    s = i
    while s > 0 and not lines[s].startswith("SECTION"):
        s -= 1
    return s


def body(P, rts):
    return f"""    ; ${P}-$F06F: DK box collision run natively (tools/native_dk_box_collision.py)
    ld [$C00C], a ; STA $0C (fallback repeats it)
    ld e, a
    ld a, [$C002]
    cp $FD
    jp nc, .ndkb_slow_{P}
    ld l, a
    ld a, [$C003]
    cp $20
    jr c, .ndkb_ram_{P}
    bit 7, a
    jp z, .ndkb_slow_{P}
    ld h, a
    and $30
    swap a
    add $02
    ldh [rSVBK], a ; PRG cache bank (as the inlined PRG mirror read)
    ld a, h
    and $0F
    or $D0
    ld h, a
    jr .ndkb_go_{P}
.ndkb_ram_{P}:
    and $07
    or $C0
    ld h, a
.ndkb_go_{P}:
    ld a, e
    and a
    ld de, $C046
    jr z, .ndkb_m0_{P}
    ld e, $4A
.ndkb_m0_{P}:
    ld a, [$C000]
    ld b, a
    ld a, [$C001]
    ld c, a
    ld a, [hli]
    add b
    ld [de], a
    inc e
    ld a, [hli]
    add c
    ld [de], a
    inc e
    ld a, [hli]
    add b
    ld [de], a
    inc e
    ld a, [hl]
    add c
    ld [de], a
    ld b, a
    sbc a ; 6502 C of the last ADC as 0/$FF
    ld c, a
    ldh a, [nes_sp]
    ld l, a
    ld h, $C1
    ldh a, [nes_x]
    ld [hl], a ; PHA X
    dec l
    ldh a, [nes_y]
    ld [hl], a ; PHA Y
    dec l
    ld [hl], $F0 ; last inner JSR return high byte
    dec l
    ld a, e
    cp $49
    jr nz, .ndkb_m1_{P}
    ld [hl], $12 ; JSR $F069 at $F010
    ld a, c
    ldh [nes_c_shadow], a
    ld a, b ; mode 0 returns $49
    jr .ndkb_fin_{P}
.ndkb_m1_{P}:
    ld [hl], $29 ; JSR $F069 at $F027
    ld a, [$C046]
    ld b, a
    ld a, [$C04A]
    sub b
    ld [$C09C], a
    ld a, [$C047]
    ld b, a
    ld a, [$C04B]
    sub b
    ld [$C09D], a
    ld a, [$C04B]
    ld b, a
    ld a, [$C049]
    cp b
    jr c, .ndkb_zero_{P}
    ld a, [$C047]
    ld b, a
    ld a, [$C04D]
    cp b
    jr c, .ndkb_zero_{P}
    ld a, [$C046]
    ld b, a
    ld a, [$C04C]
    cp b
    jr c, .ndkb_zero_{P}
    ld a, [$C04A]
    ld b, a
    ld a, [$C048]
    cp b
    jr c, .ndkb_zero_{P}
    ld a, $01
    ldh [nes_c_shadow], a
    jr .ndkb_fin_{P}
.ndkb_zero_{P}:
    xor a
    ldh [nes_c_shadow], a
.ndkb_fin_{P}:
    ld [$C00C], a
    ldh [nes_a], a
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ldh a, [nes_sp]
    ld l, a
    ld h, $C1
    jp {rts}
.ndkb_slow_{P}:
    ld a, e ; A = mode for the translated path
""".splitlines(keepends=True)


def main(asm, rom_path):
    rom = Path(rom_path).read_bytes()
    read = prg_reader(rom)
    # the PRG cache address mapping below is the 16K (NROM-128) layout
    if read is None or rom[4] != 1 or read(ENTRY, len(PAT)) != PAT:
        print("native-dk-box-collision: routine not present")
        return
    p = Path(asm)
    text = p.read_text()
    if v_observable(text):
        print("native-dk-box-collision: V observable; skipped")
        return
    if "and $30 ; inlined PRG mirror read" not in text or "or $D0 ; inlined PRG mirror read" not in text:
        print("native-dk-box-collision: unexpected PRG cache layout; skipped")
        return
    lines = text.splitlines(keepends=True)
    P = f"{ENTRY:04X}"
    try:
        ent = next(i for i, l in enumerate(lines) if code(l) == f"nes_{P}_trace:")
    except StopIteration:
        print("native-dk-box-collision: no entry label; skipped")
        return
    # entry: label, optional IF/ENDC profile block, then the $EFF5 STA comment
    j = ent + 1
    if code(lines[j]).startswith("IF DEF(NES2GBC_PROFILE_TRACE)"):
        while code(lines[j]) != "ENDC":
            j += 1
        j += 1
    m = PC_COMMENT.match(lines[j])
    if not m or int(m.group(1), 16) != ENTRY or code(lines[j + 1]) != "ld [$C00C], a":
        print("native-dk-box-collision: unexpected entry shape; skipped")
        return
    # RTS tail at $F061
    rts_pc = ENTRY + len(PAT) - 15
    r = next((i for i, l in enumerate(lines) if (mm := PC_COMMENT.match(l)) and int(mm.group(1), 16) == rts_pc), None)
    if r is None:
        print("native-dk-box-collision: no RTS block; skipped")
        return
    k = r
    while k < len(lines) and code(lines[k]) != "PROFILE_INC nes_profile_rts_pop":
        if lines[k].startswith("SECTION") or re.match(r"^[A-Za-z_]\w*:", code(lines[k])):
            k = len(lines)
            break
        k += 1
    if k >= len(lines):
        print("native-dk-box-collision: no RTS pop; skipped")
        return
    nxt = next(code(x) for x in lines[k + 1:] if code(x))
    if nxt not in ("ld a, l", "ldh a, [nes_sp]"):
        print(f"native-dk-box-collision: RTS tail starts with '{nxt}'; skipped")
        return
    bank = lambda i: re.search(r"BANK\[(\d+)\]", lines[section_of(lines, i)])
    bk, be = bank(k), bank(ent)
    if not bk or not be or bk.group(1) != be.group(1):
        print("native-dk-box-collision: RTS tail in another bank; skipped")
        return
    # scope of the RTS local label: nearest preceding global label
    g = k
    while not re.match(r"^[A-Za-z_]\w*:", code(lines[g])):
        g -= 1
    scope = code(lines[g])[:-1]
    rts = f"{scope}.ndkb_rts_{P}"
    lines[k:k] = [f".ndkb_rts_{P}: ; native DK box collision return (L = SP, H = $C1)\n"]
    lines[j:j] = body(P, rts)
    p.write_text("".join(lines))
    print("native-dk-box-collision: $EFF5 routine run natively")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
