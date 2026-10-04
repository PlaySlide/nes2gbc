#!/usr/bin/env python3
"""Run SMB's metatile-column renderer loop ($88D0-$8942) natively.

Matches (by ROM bytes, NROM, exact span) the 13-row loop

  88D0: STX $01 / LDA $06A1,X / AND #$C0 / STA $03 / ASL / ROL / ROL / TAY
        LDA $8B08,Y / STA $06 / LDA $8B0C,Y / STA $07
        LDA $06A1,X / ASL / ASL / STA $02 / LDA $071F / AND #1 / EOR #1 / ASL
        ADC $02 / TAY / LDX $00 / LDA ($06),Y / STA $0344,X / INY
        LDA ($06),Y / STA $0345,X / LDY $04 / LDA $05 / (attribute-bit shifts
        of $03, INC $04 on odd rows) / LDA $03F9,Y / ORA $03 / STA $03F9,Y
        INC $00 / INC $00 / LDX $01 / INX / CPX #$0D / BCC 88D0

The canonical nes_88D0 entry (all NES state in HRAM) jumps to a native loop
that reproduces every RAM write ($00-$07, $0344/$0345,X, $03F9,Y) and the
exit state at $8943 (A, X, Y, Z/N/C from the CPX). The ($06),Y metatile reads
come from copies of the four ROM metatile tables emitted next to the loop.
The ADC's V result is not reproduced, so the pass requires V to be
unobservable (tools/dead_overflow.v_observable).
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dead_overflow import v_observable  # noqa: E402

SPAN = bytes.fromhex(
    "8601bda10629c085030a2a2aa8b9088b8506b90c8b8507bda1060a0a8502ad1f07"
    "290149010a6502a8a600b1069d4403c8b1069d4503a404a505d00ea5014ab01926"
    "0326032603 4c3089a5014ab00f46034603460346034c3089460346 03e604b9f903"
    "0503 99f903e600e600a601e8e00d908d".replace(" ", ""))
BASE = 0x88D0
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


def routine(read) -> str:
    ptr = [read(0x8B08 + j, 1)[0] | read(0x8B0C + j, 1)[0] << 8 for j in range(4)]
    for p in ptr:
        if p < 0x8000:
            raise ValueError("metatile table pointer not in PRG")
    out = ['\nSECTION "Native metatile column 88D0", ROMX, ALIGN[8]\n',
           'nes_native_88D0_tabs: ; tab j, entry y = ROM[ptr_j + y]\n']
    for p in ptr:
        data = read(p, 256)
        for r in range(0, 256, 16):
            out.append("    db " + ", ".join(f"${b:02X}" for b in data[r:r + 16]) + "\n")
    out.append("nes_native_88D0_ptrs: ; lo[0..3], hi[0..3] ($8B08/$8B0C)\n")
    out.append("    db " + ", ".join(f"${p & 0xFF:02X}" for p in ptr) + ", "
               + ", ".join(f"${p >> 8:02X}" for p in ptr) + "\n")
    out.append('''nes_native_88D0:
    ldh a, [nes_x]
    ld b, a                  ; B = X
.loop:
    ld a, b
    ld [$C001], a            ; STX $01
    add $A1
    ld l, a
    ld a, $C6
    adc $00
    ld h, a
    ld a, [hl]               ; LDA $06A1,X
    ld c, a
    and $C0
    ld [$C003], a
    rlca
    rlca
    ld d, a                  ; table j = TAY value
    ld l, a
    ld h, HIGH(nes_native_88D0_ptrs)
    ld a, [hl]
    ld [$C006], a
    set 2, l
    ld a, [hl]
    ld [$C007], a
    ld a, c
    add a
    add a
    ld [$C002], a
    ld e, a
    ld a, [$C71F]
    and $01
    xor $01
    add a
    add e                    ; <= $FE: no carry
    ld l, a                  ; TAY
    ld a, d
    add HIGH(nes_native_88D0_tabs)
    ld h, a
    ld a, [hli]              ; LDA ($06),Y
    ld c, a
    ld e, [hl]               ; INY / LDA ($06),Y
    ld a, [$C000]            ; LDX $00
    add $44
    ld l, a
    ld a, $C3
    adc $00
    ld h, a
    ld [hl], c               ; STA $0344,X
    inc hl
    ld [hl], e               ; STA $0345,X
    ld a, [$C004]
    ld e, a                  ; LDY $04
    ld a, [$C003]
    ld c, a
    ld a, [$C005]
    and a
    jr nz, .five_nz
    bit 0, b
    jr nz, .odd_z
    ld a, c                  ; ROL $03 x3 (C=0): bits 7-6 -> 1-0
    rlca
    rlca
    and $03
    jr .store3
.odd_z:
    ld a, c                  ; LSR $03 x2, then INC $04
    srl a
    srl a
    jr .store3_inc
.five_nz:
    bit 0, b
    jr nz, .odd_nz
    ld a, c                  ; LSR $03 x4
    swap a
    and $0F
    jr .store3
.odd_nz:
    ld a, c                  ; INC $04 only
.store3_inc:
    ld hl, $C004
    inc [hl]
.store3:
    ld [$C003], a
    ld c, a
    ld a, e
    add $F9
    ld l, a
    ld a, $C3
    adc $00
    ld h, a
    ld a, [hl]               ; LDA $03F9,Y / ORA $03 / STA $03F9,Y
    or c
    ld [hl], a
    ld d, a
    ld hl, $C000
    inc [hl]
    inc [hl]
    inc b                    ; LDX $01 / INX
    ld a, b
    cp $0D
    jp c, .loop
    ld a, d
    ldh [nes_a], a
    ld a, b
    ldh [nes_x], a
    sub $0D
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ld a, e
    ldh [nes_y], a
    ld a, $01
    ldh [nes_c_shadow], a
    ld a, BANK(nes_8943)
    ld hl, nes_8943
    jp nes_jump_known_hl_a_8bit ; translated $8943 (all state in HRAM)
''')
    return "".join(out)


def main(asm, rom_path):
    read = prg_reader(Path(rom_path).read_bytes())
    if read is None:
        print("native-metatile-column: non-NROM mapper, skipped")
        return
    if read(BASE, len(SPAN)) != SPAN:
        print("native-metatile-column: routine not present")
        return
    p = Path(asm)
    text = p.read_text()
    if v_observable(text):
        print("native-metatile-column: V observable; skipped")
        return
    lines = text.splitlines(keepends=True)
    if not any(code(l) == "nes_8943:" for l in lines):
        print("native-metatile-column: no nes_8943 entry; skipped")
        return
    for i, l in enumerate(lines):
        if code(l) != "nes_88D0:":
            continue
        q = i + 1
        while q < len(lines) and not PC_COMMENT.match(lines[q]):
            if code(lines[q]).startswith("SECTION"):
                q = len(lines)
                break
            q += 1
        if q >= len(lines) or int(PC_COMMENT.match(lines[q]).group(1), 16) != BASE:
            print("native-metatile-column: unexpected nes_88D0 shape; skipped")
            return
        lines[q:q] = [
            "    ; native metatile column loop (tools/native_metatile_column.py)\n",
            "    ld a, BANK(nes_native_88D0)\n",
            "    ld hl, nes_native_88D0\n",
            "    jp nes_jump_known_hl_a_8bit\n",
        ]
        lines.append(routine(read))
        p.write_text("".join(lines))
        print("native-metatile-column: nes_88D0 replaced")
        return
    print("native-metatile-column: nes_88D0 not found; skipped")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
