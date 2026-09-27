#!/usr/bin/env python3
"""Native multi-byte compare-and-copy (SMB UpdateTopScore style), ROM-matched.

Matches, at any PRG address P:
    LDY #n / SEC / L: LDA a1,X / SBC a2,Y / DEX / DEY / BPL L / BCC R
    INX / INY / C: LDA a1,X / STA a2,Y / INX / INY / CPY #n+1 / BCC C / R: RTS
i.e. "if the (n+1)-byte big-endian number at a1+X-n.. >= the one at a2..,
copy it". Both loops are unrolled natively and the exit state is exact:
A, X, Y, N/Z, C (the SBC V flag is not produced, so the pass requires V to be
unobservable in the ROM, like tools/dead_overflow.py). The native code is
inserted at the head of the translated entry and jumps to the canonical RTS
block at R; a runtime guard (n <= X <= $7FF - a1) keeps every access in one
RAM mirror, else it falls through to the untouched translation.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dead_overflow import v_observable  # noqa: E402

PC_COMMENT = re.compile(r"^\s*; \$([0-9A-F]{4}): ")


def code(l):
    return l.split(";", 1)[0].strip()


def prg_bytes(rom):
    banks = rom[4]
    trainer = 512 if rom[6] & 4 else 0
    if banks not in (1, 2) or (rom[6] >> 4 | (rom[7] & 0xF0)) != 0:
        return None
    prg = rom[16 + trainer:16 + trainer + banks * 16384]
    return prg * (2 // banks)  # $8000-$FFFF view


def find(prg):
    out = []
    for o in range(len(prg) - 30):
        b = prg[o:o + 30]
        if not (b[0] == 0xA0 and b[2] == 0x38 and b[3] == 0xBD and b[6] == 0xF9 and b[9:16] == bytes([0xCA, 0x88, 0x10, 0xF6, 0x90, 0x0E, 0xE8])
                and b[16] == 0xC8 and b[17] == 0xBD and b[20] == 0x99 and b[23:26] == bytes([0xE8, 0xC8, 0xC0]) and b[27:30] == bytes([0x90, 0xF4, 0x60])):
            continue
        n = b[1]; a1 = b[4] | b[5] << 8; a2 = b[7] | b[8] << 8
        if b[26] != n + 1 or b[18] | b[19] << 8 != a1 or b[21] | b[22] << 8 != a2:
            continue
        if n > 0x40 or a1 > 0x7FF or a2 + n > 0x7FF:
            continue
        out.append((0x8000 + o, n, a1, a2))
    return out


def emit(pc, n, a1, a2, xsrc):
    r = pc + 29
    k = f"nmcc_{pc:04X}"
    lim = 0x7FF - a1
    L = [f"; native multi-byte compare/copy ${pc:04X} (tools/native_multibyte_compare_copy.py)",
         "ld a, b" if xsrc == "b" else "ldh a, [nes_x]",
         f"cp ${n:02X}", f"jp c, .{k}_slow", f"cp ${lim + 1:02X}" if lim < 0xFF else "", f"jp nc, .{k}_slow" if lim < 0xFF else "",
         "ld b, a",
         f"add ${(0xC000 | a1) & 0xFF:02X}", "ld l, a", f"ld a, ${(0xC000 | a1) >> 8:02X}", "adc $00", "ld h, a",
         f"ld de, ${0xC000 | (a2 + n):04X}", "and a"]
    for _ in range(n + 1):
        L += ["ld a, [de]", "ld c, a", "ld a, [hl]", "sbc c", "dec hl", "dec de"]
    L += [f"jr c, .{k}_lt",
          "inc hl", "inc de"]
    for _ in range(n + 1):
        L += ["ld a, [hli]", "ld [de], a", "inc de"]
    L += ["ldh [nes_a], a", "ld a, b", "inc a", "ldh [nes_x], a", f"ld a, ${n + 1:02X}", "ldh [nes_y], a",
          "xor a", "ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a", "inc a", "ldh [nes_c_shadow], a",
          f"jr .{k}_out",
          f".{k}_lt:",
          "ldh [nes_a], a", "ld a, b", f"sub ${n + 1:02X}", "ldh [nes_x], a", "ld a, $FF", "ldh [nes_y], a",
          "ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a", "xor a", "ldh [nes_c_shadow], a",
          f".{k}_out:",
          f"ld a, BANK(nes_{r:04X})", f"ld hl, nes_{r:04X}", "jp nes_jump_known_hl_a_8bit ; 8-bit translated-code bank switch",
          f".{k}_slow:"]
    return ["".join(x if x.endswith(":") and not x.startswith(";") else "    " + x) + "\n" for x in L if x]


def main(asm, rom_path):
    rom = Path(rom_path).read_bytes()
    prg = prg_bytes(rom)
    if prg is None:
        print("native-multibyte-compare-copy: non-NROM mapper, skipped")
        return
    p = Path(asm)
    text = p.read_text()
    sites = find(prg)
    if not sites:
        print("native-multibyte-compare-copy: 0 routine(s)")
        return
    if v_observable(text):
        print("native-multibyte-compare-copy: V observable, skipped")
        return
    lines = text.splitlines(keepends=True)
    done = 0
    for pc, n, a1, a2 in sites:
        if not re.search(rf"^nes_{pc + 29:04X}:", text, re.M):
            continue
        for want in (f"nes_{pc:04X}_trace:", f"nes_{pc:04X}:"):
            idx = next((i for i, l in enumerate(lines) if code(l) == want), None)
            if idx is None:
                continue
            # first NES instruction of the block must be the LDY at pc
            j = idx + 1
            while j < len(lines) and not PC_COMMENT.match(lines[j]):
                if code(lines[j]).startswith("SECTION") or re.match(r"^[A-Za-z_]\w*:", code(lines[j])):
                    j = None
                    break
                j += 1
            if j is None or j >= len(lines) or int(PC_COMMENT.match(lines[j]).group(1), 16) != pc:
                continue
            # X source: resident B iff the block materializes X from B before any reseed.
            body = "".join(lines[idx:idx + 60])
            if want.endswith("_trace:"):
                mat = body.find("superblock materialize X")
                if mat < 0 or "ld a, b ; superblock materialize X" not in body:
                    continue
                xsrc = "b"
            else:
                if "ld b, a ; canonical adapter X" in body or "superblock materialize X" in body:
                    continue  # adapter/resident form; only the trace form is handled
                xsrc = "hram"
            lines[j:j] = emit(pc, n, a1, a2, xsrc)
            done += 1
            print(f"native-multibyte-compare-copy: ${pc:04X} n={n} a1=${a1:04X} a2=${a2:04X} at {want} (X from {xsrc})")
            break
    p.write_text("".join(lines))
    print(f"native-multibyte-compare-copy: {done} routine(s) replaced")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
