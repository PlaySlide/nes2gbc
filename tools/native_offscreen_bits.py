#!/usr/bin/env python3
"""Run SMB's GetOffScreenBitsSet call tree natively.

Matches (by ROM bytes, NROM, exact span $F1C0-$F281) the routine

  F1C0: TYA / PHA / JSR F1D7 / (F1C5 tail stays translated)
  F1D7: JSR F1F6 / LSR x4 / STA $00 / JMP F239
  F1F6: GetXOffscreenBits, F239: GetYOffscreenBits (2-iteration loops over
        Y=1,0 using DividePDiff at F26D)

The translated block at nes_F1C0_trace (X resident in B, Y in C) is
replaced by straight GB code that unrolls both loops (all table indices
become constants except the DividePDiff result) and reproduces every
observable effect up to the JSR's return point F1C5: ZP $00/$04-$07 (as
written, including the conditional $05/$06), the PHA/JSR stack bytes in
their final overwrite order, A, X, Y and SP. It then enters the canonical
translated block nes_F1C5 (whose ASL defines C/Z/N). The V flag from the
SBCs is not reproduced, so the pass requires V to be unobservable
(tools/dead_overflow.v_observable).
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dead_overflow import v_observable  # noqa: E402

SPAN = bytes.fromhex(
    "984820d7f10a0a0a0a0500850068a8a50099d003a6086020f6f14a4a4a4a85004c39f27f3f1f0f0703010080c0e0f0f8fcfeff070f078604a001b91c0738f5868507b91a07f56dbef3f1c9003010bef4f1c9011009a9388506a908206df2bde3f1a604c900d0038810d06000080c0e0f07030100040004ff008604a001b937f238f5ce8507a901f5b5be34f2c9003010be35f2c9011009a9208506a904206df2bd2bf2a604c900d0038810d1608505a507c506b00c4a4a4a2907c001b0026505aa60")
BASE = 0xF1C0
ANY_LABEL = re.compile(r"^[A-Za-z_][\w]*:$")
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


def emit(read, entry=BASE):
    """entry=$F1C0: TYA/PHA/JSR F1D7 from nes_F1C0_trace, ends in nes_F1C5.
    entry=$F1D7: canonical nes_F1D7 (all state in HRAM); ends in the translated
    RTS block nes_F26C_trace with SP at F1D7's frame and exact A/X/Y/Z/N/C."""
    r = lambda a: read(a, 1)[0]
    P = f"{entry:04X}"
    offs = {"x": 5, "y": 3} if entry == BASE else {"x": 2, "y": 0}
    tx = [r(0xF1E3 + i) for i in range(16)]   # GetX bit masks (index 0..15)
    ty = [r(0xF22B + i) for i in range(12)]   # GetY bit masks (index 0..11)
    out = []
    A = out.append

    def loop(kind):
        # kind 'x': lo = [$071C+y] - m1(d), hi = [$071A+y] - m2(e) - borrow
        # kind 'y': lo = rom[F237+y] - m3(d), hi = 1 - m4(e) - borrow
        for y in (1, 0):
            t = f"{kind}{y}_{P}"
            if kind == "x":
                k1, k2 = r(0xF1F3 + y), r(0xF1F4 + y)
                lim, add5, ret, off = 0x38, 0x08, 0xF21D, offs["x"]
                tab = tx
                A(f"ld a, [${0xC71C + y:04X}]")
                A("sub d")
                A("ld [$C007], a")
                A(f"ld a, [${0xC71A + y:04X}]")
                A("sbc e")
            else:
                k1, k2 = r(0xF234 + y), r(0xF235 + y)
                lim, add5, ret, off = 0x20, 0x04, 0xF25F, offs["y"]
                tab = ty
                A(f"ld a, ${r(0xF237 + y):02X}")
                A("sub d")
                A("ld [$C007], a")
                A("ld a, $01")
                A("sbc e")
            assert k1 < len(tab) and k2 < len(tab)
            A("bit 7, a")
            A(f"jr nz, .osb_k1_{t}")
            A("and a")
            A(f"jr nz, .osb_k2_{t}")
            A(f"ld a, ${lim:02X}")
            A("ld [$C006], a")
            A(f"ld a, ${add5:02X}")
            A("ld [$C005], a")
            A("ldh a, [nes_sp]")
            if off:
                A(f"sub {off}")
            A("ld l, a")
            A("ld h, $C1")
            A(f"ld [hl], ${ret >> 8:02X}")
            A("dec l")
            A(f"ld [hl], ${ret & 0xFF:02X}")
            A("ld a, [$C007]")
            A(f"cp ${lim:02X}")
            A(f"jr nc, .osb_k2_{t}")
            A("rrca")
            A("rrca")
            A("rrca")
            A("and $07")
            if y == 0:
                A(f"add ${add5:02X}")
            assert 7 + (add5 if y == 0 else 0) < len(tab)
            tl = f"nes_native_osb_t{kind}_{P}"
            A(f"add LOW({tl})")
            A("ld l, a")
            A(f"adc HIGH({tl})")
            A("sub l")
            A("ld h, a")
            A("ld a, [hl]")
            A(f"jr .osb_have_{t}")
            A(f".osb_k1_{t}:")
            A(f"ld a, ${tab[k1]:02X}")
            A(f"jr .osb_have_{t}")
            A(f".osb_k2_{t}:")
            A(f"ld a, ${tab[k2]:02X}")
            A(f".osb_have_{t}:")
            A("and a")
            A(f"ld c, ${y:02X}")
            A(f"jr nz, .osb_done_{kind}_{P}")
        A("ld c, $FF")
        A(f".osb_done_{kind}_{P}:")

    if entry == BASE:
        A(f"; ${BASE:04X}-$F1C4 + JSR tree F1D7/F1F6/F239/F26D: offscreen bits run natively")
        # TYA/PHA and the two JSR return pushes (F1C4, then F1D9).
        A("ldh a, [nes_sp]")
        A("ld l, a")
        A("ld h, $C1")
        A("ld [hl], c")
        A("dec l")
        A("ld [hl], $F1")
        A("dec l")
        A("ld [hl], $C4")
        A("dec l")
        A("ld [hl], $F1")
        A("dec l")
        A("ld [hl], $D9")
        A("ld a, b")
        A("ldh [nes_x], a")
        A("ld [$C004], a")
    else:
        A(f"; ${entry:04X} JSR tree F1F6/F239/F26D: offscreen bits run natively")
        A("ldh a, [nes_sp]")
        A("ld l, a")
        A("ld h, $C1")
        A("ld [hl], $F1")
        A("dec l")
        A("ld [hl], $D9")
        A("ldh a, [nes_x]")
        A("ld b, a")
        A("ld [$C004], a")
    # m1 = [$86+X], m2 = [$6D+X]
    A("add $86")
    A("ld l, a")
    A("ld h, $C0")
    A("ld d, [hl]")
    A("ld a, b")
    A("add $6D")
    A("ld l, a")
    A("ld e, [hl]")
    loop("x")
    A("swap a")
    A("and $0F")
    A("ld [$C000], a")
    # m3 = [$CE+X], m4 = [$B5+X]
    A("ld a, b")
    A("add $CE")
    A("ld l, a")
    A("ld h, $C0")
    A("ld d, [hl]")
    A("ld a, b")
    A("add $B5")
    A("ld l, a")
    A("ld e, [hl]")
    loop("y")
    A("ldh [nes_a], a")
    if entry == BASE:
        A("ld a, c")
        A("ldh [nes_y], a")
        A("ldh a, [nes_sp]")
        A("dec a")
        A("ldh [nes_sp], a")
        A("ld a, BANK(nes_F1C5)")
        A("ld hl, nes_F1C5")
        A("jp nes_jump_known_hl_a_8bit ; 8-bit translated-code bank switch")
    else:
        # at $F26C: A != 0 -> CMP #0 flags (Z=0, N=A.7); A == 0 -> DEY to $FF; C=1
        A("and a")
        A(f"jr nz, .osb_flags_{P}")
        A("dec a")
        A(f".osb_flags_{P}:")
        A("ldh [nes_z_shadow], a")
        A("ldh [nes_n_shadow], a")
        A("ld a, $01")
        A("ldh [nes_c_shadow], a")
        A("ld a, c")
        A("ldh [nes_y], a")
        A("ld a, BANK(nes_F26C_trace)")
        A("ld hl, nes_F26C_trace")
        A("jp nes_jump_known_hl_a_8bit ; translated RTS block (all state in HRAM)")
    lines = []
    for x in out:
        if x.startswith("."):
            lines.append(x + "\n")
        else:
            lines.append("    " + x + "\n")
    lines.append(f"nes_native_osb_tx_{P}:\n")
    lines.append("    db " + ", ".join(f"${v:02X}" for v in tx) + "\n")
    lines.append(f"nes_native_osb_ty_{P}:\n")
    lines.append("    db " + ", ".join(f"${v:02X}" for v in ty) + "\n")
    return lines


def main(asm, rom_path):
    read = prg_reader(Path(rom_path).read_bytes())
    if read is None:
        print("native-offscreen-bits: non-NROM mapper, skipped")
        return
    if read(BASE, len(SPAN)) != SPAN:
        print("native-offscreen-bits: routine not present")
        return
    p = Path(asm)
    text = p.read_text()
    if v_observable(text):
        print("native-offscreen-bits: V observable; skipped")
        return
    lines = text.splitlines(keepends=True)
    if not any(code(l) == "nes_F1C5:" for l in lines):
        print("native-offscreen-bits: no nes_F1C5 entry; skipped")
        return
    for i, l in enumerate(lines):
        if code(l) != f"nes_{BASE:04X}_trace:":
            continue
        e = i + 1
        while e < len(lines) and not code(lines[e]).startswith("SECTION") and not ANY_LABEL.match(code(lines[e])):
            e += 1
        first = next((q for q in range(i + 1, e) if PC_COMMENT.match(lines[q])), None)
        body = "".join(lines[i:e])
        pcs = [int(PC_COMMENT.match(lines[q]).group(1), 16) for q in range(i, e) if PC_COMMENT.match(lines[q])]
        if first is None or pcs != [0xF1C0, 0xF1C1, 0xF1C2] or "superblock cached Y" not in body \
                or not re.search(r"ld a, c ; superblock cached Y", body) or "ld a, b ; superblock materialize X" not in body:
            print("native-offscreen-bits: unexpected block shape; skipped")
            return
        lines[first:e] = emit(read)
        n = 1
        labels = {code(l) for l in lines}
        for j, l in enumerate(lines):
            if code(l) == "nes_F1D7:" and "nes_F26C_trace:" in labels and code(lines[j - 1]).startswith("SECTION"):
                q = j + 1
                while q < len(lines) and not PC_COMMENT.match(lines[q]):
                    q += 1
                if q < len(lines) and int(PC_COMMENT.match(lines[q]).group(1), 16) == 0xF1D7:
                    lines[q:q] = emit(read, 0xF1D7)
                    n += 1
                break
        p.write_text("".join(lines))
        print(f"native-offscreen-bits: {n} entr{'y' if n == 1 else 'ies'} replaced")
        return
    print("native-offscreen-bits: nes_F1C0_trace not found; skipped")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
