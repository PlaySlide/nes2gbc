#!/usr/bin/env python3
"""Inline a few tiny hot Donkey Kong leaf subroutines at their static JSR sites.

Same scheme as tools/native_relative_xy_leaf.py: at each static `JSR leaf`
whose continuation has a canonical block, the translated JSR (return push +
transfer) and the leaf's RTS are replaced by the same page-$01 return-byte
writes (virtual SP unchanged: push and pop cancel), the leaf body in GB code
with A/X and Z/N/C materialized in HRAM, and a transfer to the continuation.
Every leaf is matched by its ROM bytes (NROM-128: the PRG table read uses
the 16K WRAM PRG cache layout). V from the ADC leaves is not reproduced, so
V must be unobservable.

  $EFD5: LDA $5D / CLC / ADC #3 / JMP $EFE2   } $EFE2: ASL x4 / TAX / RTS
  $EFDD: LDA $AE / CLC / ADC #1 / (EFE2)      }
  $C847: TAX / LDA $C03C,X / STA $02 / LDA $C03D,X / STA $03 / RTS
  $EAEC: LDA $0203,X / STA $00 / LDA $0200,X / STA $01 / RTS
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dead_overflow import v_observable  # noqa: E402

PC_COMMENT = re.compile(r"^\s*; \$([0-9A-F]{4}): \$([0-9A-F]{2}) ")
ANY_LABEL = re.compile(r"^[A-Za-z_][\w]*:$")


def code(l):
    return l.split(";", 1)[0].strip()


def asl4_body(zp, k):
    return [
        f"    ld a, [$C0{zp:02X}]",
        f"    add ${k:02X} ; CLC / ADC #{k}",
        "    swap a",
        "    ld l, a",
        "    and $01",
        "    ldh [nes_c_shadow], a ; C = bit 4 of the sum (last ASL)",
        "    ld a, l",
        "    and $F0 ; ASL x4",
        "    ldh [nes_a], a",
        "    ldh [nes_x], a ; TAX",
        "    ldh [nes_z_shadow], a",
        "    ldh [nes_n_shadow], a",
    ]


C847 = [
    "    ldh a, [nes_a]",
    "    ldh [nes_x], a ; TAX",
    "    ld l, a",
    "    ld a, $02",
    "    ldh [rSVBK], a ; WRAM PRG cache bank for $C000-$CFFF",
    "    ld h, $D0",
    "    ld bc, $003C",
    "    add hl, bc",
    "    ld a, [hli] ; LDA $C03C,X",
    "    ld [$C002], a",
    "    ld a, [hl] ; LDA $C03D,X",
    "    ld [$C003], a",
    "    ldh [nes_a], a",
    "    ldh [nes_z_shadow], a",
    "    ldh [nes_n_shadow], a",
]
EAEC = [
    "    ldh a, [nes_x]",
    "    add $03",
    "    ld l, a",
    "    ld a, $C2",
    "    adc $00",
    "    ld h, a",
    "    ld a, [hl] ; LDA $0203,X",
    "    ld [$C000], a",
    "    ldh a, [nes_x]",
    "    ld l, a",
    "    ld h, $C2",
    "    ld a, [hl] ; LDA $0200,X",
    "    ld [$C001], a",
    "    ldh [nes_a], a",
    "    ldh [nes_z_shadow], a",
    "    ldh [nes_n_shadow], a",
]
# (leaf, [(addr, bytes) that must match], body)
LEAVES = [
    (0xEFD5, [(0xEFD5, "a55d1869034ce2ef"), (0xEFE2, "0a0a0a0aaa60")], asl4_body(0x5D, 3)),
    (0xEFDD, [(0xEFDD, "a5ae1869010a0a0a0aaa60")], asl4_body(0xAE, 1)),
    (0xC847, [(0xC847, "aabd3cc08502bd3dc0850360")], C847),
    (0xEAEC, [(0xEAEC, "bd03028500bd0002850160")], EAEC),
]


def body(leaf, lines_, jsr_pc):
    ret = (jsr_pc + 2) & 0xFFFF
    cont = (jsr_pc + 3) & 0xFFFF
    return [s + "\n" for s in [
        f"    ; native leaf ${leaf:04X} inlined (tools/native_tiny_leaves.py)",
        "    ldh a, [nes_sp]",
        "    ld l, a",
        "    ld h, $C1",
        f"    ld [hl], ${ret >> 8:02X} ; JSR return bytes (SP itself unchanged: RTS pops them)",
        "    dec l",
        f"    ld [hl], ${ret & 0xFF:02X}",
        *lines_,
        f"    ld a, BANK(nes_{cont:04X})",
        f"    ld hl, nes_{cont:04X}",
        "    jp nes_jump_known_hl_a_8bit ; leaf RTS continuation (all state in HRAM)",
    ]]


def main(asm, rom_path):
    rom = Path(rom_path).read_bytes()
    banks = rom[4]
    if banks != 1 or (rom[6] >> 4 | (rom[7] & 0xF0)) != 0 or rom[6] & 4:
        print("native-tiny-leaves: not NROM-128, skipped")
        return
    prg = rom[16:16 + 16384]
    read = lambda a, n: prg[(a - 0x8000) % 16384:(a - 0x8000) % 16384 + n]
    leaves = [(lf, b) for lf, req, b in LEAVES if all(read(a, len(bytes.fromhex(h))) == bytes.fromhex(h) for a, h in req)]
    if not leaves:
        print("native-tiny-leaves: no leaf present")
        return
    p = Path(asm)
    text = p.read_text()
    if v_observable(text):
        print("native-tiny-leaves: V observable; skipped")
        return
    lines = text.splitlines(keepends=True)
    labels = {code(l) for l in lines}
    want = {lf: b for lf, b in leaves}
    n = 0
    i = 0
    while i < len(lines):
        m = PC_COMMENT.match(lines[i])
        if not m or m.group(2) != "20":
            i += 1
            continue
        pc = int(m.group(1), 16)
        tgt = read(pc + 1, 2)
        leaf = tgt[0] | tgt[1] << 8
        if read(pc, 1) != b"\x20" or leaf not in want or f"nes_{pc + 3:04X}:" not in labels:
            i += 1
            continue
        e = i + 1
        while e < len(lines) and not PC_COMMENT.match(lines[e]) and not ANY_LABEL.match(code(lines[e])) \
                and not code(lines[e]).startswith("SECTION") and code(lines[e]) != ":":
            e += 1
        seg = "".join(lines[i + 1:e])
        tail = [code(x) for x in lines[i + 1:e] if code(x)]
        if f"inline static 6502 JSR return push ${pc + 2:04X}" not in seg or not tail \
                or not re.fullmatch(rf"jp nes_{leaf:04X}|jp nes_jump_known_hl_a_8bit", tail[-1]) \
                or ("nes_jump_known" in tail[-1] and f"ld hl, nes_{leaf:04X}" not in tail):
            print(f"native-tiny-leaves: unexpected JSR shape at ${pc:04X}; skipped")
            i += 1
            continue
        lines[i + 1:e] = body(leaf, want[leaf], pc)
        n += 1
        i += 1
    p.write_text("".join(lines))
    print(f"native-tiny-leaves: {n} JSR site{'s' if n != 1 else ''} inlined")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
