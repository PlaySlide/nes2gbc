#!/usr/bin/env python3
"""Inline SMB's relative-position leaf ($F171) at its static JSR sites.

Matches (by ROM bytes, NROM)

  F171: LDA $CE,X / STA $03B8,Y / LDA $86,X / SEC / SBC $071C / STA $03AD,Y / RTS

At each static `JSR $F171` whose continuation has a canonical block, the
translated JSR (return push + transfer) and the leaf's RTS are replaced by
the same stack-byte writes (page $01 keeps the exact return bytes), the
leaf body in GB code, and a direct transfer to the continuation, leaving the
virtual SP unchanged (push and pop cancel). A, Z/N and C are materialized in
HRAM exactly; V from the SBC is not, so the pass requires V to be
unobservable (tools/dead_overflow.v_observable).
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dead_overflow import v_observable  # noqa: E402

LEAF = 0xF171
SPAN = bytes.fromhex("b5ce99b803b58638ed1c0799ad0360")
ANY_LABEL = re.compile(r"^[A-Za-z_][\w]*:$")
PC_COMMENT = re.compile(r"^\s*; \$([0-9A-F]{4}): \$([0-9A-F]{2}) ")


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


def body(jsr_pc: int) -> list[str]:
    ret = (jsr_pc + 2) & 0xFFFF
    cont = (jsr_pc + 3) & 0xFFFF
    return [s + "\n" for s in [
        f"    ; native leaf ${LEAF:04X} inlined (tools/native_relative_xy_leaf.py)",
        "    ldh a, [nes_sp]",
        "    ld l, a",
        "    ld h, $C1",
        f"    ld [hl], ${ret >> 8:02X} ; JSR return bytes (SP itself unchanged: RTS pops them)",
        "    dec l",
        f"    ld [hl], ${ret & 0xFF:02X}",
        "    ldh a, [nes_x]",
        "    ld b, a",
        "    add $CE",
        "    ld l, a",
        "    ld h, $C0",
        "    ld e, [hl] ; LDA $CE,X",
        "    ldh a, [nes_y]",
        "    ld c, a",
        "    add $B8",
        "    ld l, a",
        "    ld a, $C3",
        "    adc $00",
        "    ld h, a",
        "    ld [hl], e ; STA $03B8,Y",
        "    ld a, b",
        "    add $86",
        "    ld l, a",
        "    ld h, $C0",
        "    ld a, [$C71C]",
        "    ld d, a",
        "    ld a, [hl] ; LDA $86,X",
        "    sub d ; SEC / SBC $071C",
        "    ld e, a",
        "    sbc a",
        "    inc a ; 6502 C = no borrow",
        "    ldh [nes_c_shadow], a",
        "    ld a, e",
        "    ldh [nes_a], a",
        "    ldh [nes_z_shadow], a",
        "    ldh [nes_n_shadow], a",
        "    ld a, c",
        "    add $AD",
        "    ld l, a",
        "    ld a, $C3",
        "    adc $00",
        "    ld h, a",
        "    ld [hl], e ; STA $03AD,Y",
        f"    ld a, BANK(nes_{cont:04X})",
        f"    ld hl, nes_{cont:04X}",
        "    jp nes_jump_known_hl_a_8bit ; leaf RTS continuation (all state in HRAM)",
    ]]


def main(asm, rom_path):
    read = prg_reader(Path(rom_path).read_bytes())
    if read is None:
        print("native-relxy-leaf: non-NROM mapper, skipped")
        return
    if read(LEAF, len(SPAN)) != SPAN:
        print("native-relxy-leaf: routine not present")
        return
    p = Path(asm)
    text = p.read_text()
    if v_observable(text):
        print("native-relxy-leaf: V observable; skipped")
        return
    lines = text.splitlines(keepends=True)
    labels = {code(l) for l in lines}
    n = 0
    i = 0
    while i < len(lines):
        m = PC_COMMENT.match(lines[i])
        if not m or m.group(2) != "20":
            i += 1
            continue
        pc = int(m.group(1), 16)
        if read(pc, 3) != bytes([0x20, LEAF & 0xFF, LEAF >> 8]) or f"nes_{pc + 3:04X}:" not in labels:
            i += 1
            continue
        e = i + 1
        while e < len(lines) and not PC_COMMENT.match(lines[e]) and not ANY_LABEL.match(code(lines[e])) \
                and not code(lines[e]).startswith("SECTION") and code(lines[e]) != ":":
            e += 1
        seg = "".join(lines[i + 1:e])
        tail = [code(x) for x in lines[i + 1:e] if code(x)]
        if f"inline static 6502 JSR return push ${pc + 2:04X}" not in seg or not tail \
                or not re.fullmatch(rf"jp nes_{LEAF:04X}|jp nes_jump_known_hl_a_8bit", tail[-1]) \
                or ("nes_jump_known" in tail[-1] and f"ld hl, nes_{LEAF:04X}" not in tail):
            print(f"native-relxy-leaf: unexpected JSR shape at ${pc:04X}; skipped")
            i += 1
            continue
        lines[i + 1:e] = body(pc)
        n += 1
        i += 1
    p.write_text("".join(lines))
    print(f"native-relxy-leaf: {n} JSR site{'s' if n != 1 else ''} inlined")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
