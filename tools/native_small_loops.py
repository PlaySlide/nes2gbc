#!/usr/bin/env python3
"""Native replacements for small hot 6502 loops, matched by ROM bytes.

Each match is inserted at the head of its translated entry block (the entry
PC's first instruction) and ends by jumping to the canonical block at the
loop's exit PC with exact A/X/Y/N/Z/C state (V is never produced, so the ROM
must not observe V, as in tools/dead_overflow.py). A runtime guard falls
through to the untouched translation whenever an index could leave one RAM
mirror or wrap differently than the native walk assumes.

Patterns (a/b/c = RAM absolutes <$0800, z = zero page):
  dec_nonzero: L: LDA a,X / BEQ +3 / DEC a,X / DEX / BPL L
  shift_chain: L: ROR|ROL a,X / INX / DEY / BNE L
  delay:       L: DEY|DEX / BNE L
  cmp_add_wrap (SMB $81CF):
      L: LDA a,X / CMP z / BCC S / LDY b / CLC / ADC c,Y / BCC T / CLC /
         ADC z / T: STA a,X / S: DEX / BPL L
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dead_overflow import v_observable  # noqa: E402

PC_COMMENT = re.compile(r"^\s*; \$([0-9A-F]{4}): ")
LABEL = re.compile(r"^([A-Za-z_][\w.]*):")


def code(l):
    return l.split(";", 1)[0].strip()


def prg_view(rom):
    banks = rom[4]
    trainer = 512 if rom[6] & 4 else 0
    if banks not in (1, 2) or (rom[6] >> 4 | (rom[7] & 0xF0)) != 0:
        return None
    prg = rom[16 + trainer:16 + trainer + banks * 16384]
    return prg * (2 // banks)


def w(b, i):
    return b[i] | b[i + 1] << 8


def find(prg):
    out = []
    for o in range(len(prg) - 26):
        b = prg[o:o + 26]
        pc = 0x8000 + o
        if b[0] == 0xBD and b[3:6] == bytes([0xF0, 0x03, 0xDE]) and w(b, 6) == w(b, 1) and b[8:11] == bytes([0xCA, 0x10, 0xF5]) and w(b, 1) < 0x800:
            out.append(("dec_nonzero", pc, pc + 11, dict(a=w(b, 1))))
        if b[0] in (0x88, 0xCA) and b[1:3] == bytes([0xD0, 0xFD]):
            out.append(("delay", pc, pc + 3, dict(y=b[0] == 0x88)))
        if b[0] in (0x7E, 0x3E) and b[3:7] == bytes([0xE8, 0x88, 0xD0, 0xF9]) and w(b, 1) < 0x800:
            out.append(("shift_chain", pc, pc + 7, dict(a=w(b, 1), ror=b[0] == 0x7E)))
        if (b[0] == 0xBD and b[3] == 0xC5 and b[5:7] == bytes([0x90, 0x0F]) and b[7] == 0xAC and b[10:12] == bytes([0x18, 0x79])
                and b[14:17] == bytes([0x90, 0x03, 0x18]) and b[17] == 0x65 and b[18] == b[4] and b[19] == 0x9D and w(b, 20) == w(b, 1)
                and b[22:25] == bytes([0xCA, 0x10, 0xE7])):
            a, z, bb, c = w(b, 1), b[4], w(b, 8), w(b, 12)
            if a + 0x7F < 0x800 and bb < 0x800 and c + 0xFF < 0x800 and not (a <= z <= a + 0x7F) and not (a <= bb <= a + 0x7F):
                out.append(("cmp_add_wrap", pc, pc + 25, dict(a=a, z=z, b=bb, c=c)))
    return out


def ram(a):
    return 0xC000 | a


def tail(k, exit_pc):
    return [f"ld a, BANK(nes_{exit_pc:04X})", f"ld hl, nes_{exit_pc:04X}",
            "jp nes_jump_known_hl_a_8bit ; 8-bit translated-code bank switch", f".{k}_slow:"]


def emit(kind, pc, exit_pc, p, xs, ys):
    k = f"nsl_{pc:04X}"
    ldx = "ld a, b" if xs == "b" else "ldh a, [nes_x]"
    ldy = "ld a, c" if ys == "c" else "ldh a, [nes_y]"
    L = [f"; native {kind} loop ${pc:04X} (tools/native_small_loops.py)"]
    if kind == "delay":
        # L: DEY|DEX / BNE L  ->  register 0, Z set, N clear, nothing else.
        other_res = (xs == "b") if p["y"] else (ys == "c")
        if other_res:
            L += ["ld a, b", "ldh [nes_x], a"] if p["y"] else ["ld a, c", "ldh [nes_y], a"]
        L += ["xor a", "ldh [nes_y], a" if p["y"] else "ldh [nes_x], a", "ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a"]
        L += tail(k, exit_pc)[:-1]
        return [(x if (x.endswith(":") and not x.startswith(";")) else "    " + x) + "\n" for x in L]
    if kind == "dec_nonzero":
        A = ram(p["a"])
        lim = min(0x7F, 0x7FF - p["a"])
        if ys == "c":
            L += ["ld a, c", "ldh [nes_y], a"]  # canonical exit reloads Y from HRAM
        L += [ldx, f"cp ${lim + 1:02X}", f"jp nc, .{k}_slow", "ld c, a", "inc c",
              f"add ${A & 0xFF:02X}", "ld l, a", f"ld a, ${A >> 8:02X}", "adc $00", "ld h, a",
              f".{k}_l:", "ld a, [hl]", "and a", f"jr z, .{k}_s", "dec [hl]", f".{k}_s:", "dec hl", "dec c", f"jr nz, .{k}_l",
              "ldh [nes_a], a", "ld a, $FF", "ldh [nes_x], a", "ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a"]
    elif kind == "shift_chain":
        A = ram(p["a"])
        op = "rr [hl]" if p["ror"] else "rl [hl]"
        # guard: Y != 0, X + Y <= $100, a + X + Y - 1 <= $7FF
        L += [ldy, "and a", f"jp z, .{k}_slow", "ld c, a", ldx, "ld e, a", "add c",
              f"jr nc, .{k}_g1", f"jp nz, .{k}_slow", f".{k}_g1:"]  # carry and nonzero sum: X+Y > $100
        lim = 0x800 - p["a"]
        if lim <= 0xFF:
            L += [f"jp c, .{k}_slow", f"cp ${lim + 1:02X}", f"jp nc, .{k}_slow"]
        L += ["ld d, a",  # final X = X + Y (mod 256)
              "ld a, e", f"add ${A & 0xFF:02X}", "ld l, a", f"ld a, ${A >> 8:02X}", "adc $00", "ld h, a",
              "ldh a, [nes_c_shadow]", "add $FF",  # GB carry = 6502 C
              f".{k}_l:", op, "inc hl", "dec c", f"jr nz, .{k}_l",
              "ld a, $00", "adc a", "ldh [nes_c_shadow], a",
              "ld a, d", "ldh [nes_x], a", "xor a", "ldh [nes_y], a", "ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a"]
    elif kind == "cmp_add_wrap":
        A, B, C = ram(p["a"]), ram(p["b"]), ram(p["c"])
        L += [ldy, "ld c, a", ldx, f"cp $80", f"jp nc, .{k}_slow", "ld b, a", f"ld a, [${ram(p['z']):04X}]", "ld e, a",
              "ld a, b", f"add ${A & 0xFF:02X}", "ld l, a", f"ld a, ${A >> 8:02X}", "adc $00", "ld h, a",
              f".{k}_l:",
              "ld a, [hl]", "cp e", f"jr c, .{k}_skip",
              "ld d, a", f"ld a, [${B:04X}]", "ld c, a", "push hl",
              f"add ${C & 0xFF:02X}", "ld l, a", f"ld a, ${C >> 8:02X}", "adc $00", "ld h, a",
              "ld a, d", "add [hl]", "pop hl", f"jr nc, .{k}_st", "add e", f".{k}_st:", "ld [hl], a", f"jr .{k}_nx",
              f".{k}_skip:", "and a",  # 6502 C = 0 (A < z)
              f".{k}_nx:", "dec hl", "dec b", "bit 7, b", f"jr z, .{k}_l",
              "ldh [nes_a], a", "ld a, $00", "adc a", "ldh [nes_c_shadow], a",
              "ld a, c", "ldh [nes_y], a", "ld a, $FF", "ldh [nes_x], a", "ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a"]
    L += tail(k, exit_pc)
    return [(x if (x.endswith(":") and not x.startswith(";")) else "    " + x) + "\n" for x in L]


# 6502 opcodes (and lengths) that neither read nor write Y and fall through.
NOY = {0xE6: 2, 0xEE: 3, 0xC6: 2, 0xCE: 3, 0xA2: 2, 0xA9: 2, 0xA5: 2, 0xAD: 3, 0x85: 2, 0x8D: 3, 0x86: 2, 0x8E: 3,
       0x18: 1, 0x38: 1, 0xE8: 1, 0xCA: 1, 0xAA: 1, 0x8A: 1, 0x29: 2, 0x09: 2, 0x49: 2}


def y_dead_at(prg, pc):
    for _ in range(16):
        op = prg[pc - 0x8000]
        if op in (0xA0, 0xA4, 0xAC):
            return True
        if op not in NOY:
            return False
        pc += NOY[op]
    return False


def reg_source(lines, start, reg):
    """'b'/'c' if the block's first reference to X/Y uses the resident register,
    'hram' if it reads nes_x/nes_y, None if unknown."""
    r, hr = ("b", "nes_x") if reg == "x" else ("c", "nes_y")
    for l in lines[start:start + 400]:
        s = l.strip()
        if s.startswith("SECTION"):
            return None
        if s.startswith(f"ldh a, [{hr}]"):
            return "hram"
        if s.startswith(f"ld a, {r}") and (f"materialize {reg.upper()}" in s or f"cached {hr}" in s):
            return r
        if f"ldh [{hr}]" in s:
            return None  # written before read: unknown liveness, skip
        if re.search(rf"\bld {r}, ", s) or re.search(rf"\b(inc|dec) {r}\b", s):
            if reg.upper() in s.split(";", 1)[-1] or hr in s or "resident" in s:
                return None
            continue  # plain scratch use: the register is not caching X/Y here
    return None


def main(asm, rom_path):
    prg = prg_view(Path(rom_path).read_bytes())
    if prg is None:
        print("native-small-loops: non-NROM mapper, skipped")
        return
    p = Path(asm)
    text = p.read_text()
    sites = find(prg)
    if sites and v_observable(text):
        print("native-small-loops: V observable, skipped")
        return
    lines = text.splitlines(keepends=True)
    labels = {}
    for i, l in enumerate(lines):
        m = LABEL.match(l)
        if m:
            labels[m.group(1)] = i
    done = []
    for kind, pc, exit_pc, prm in sites:
        if f"nes_{exit_pc:04X}" not in labels:
            continue
        for want in (f"nes_{pc:04X}_trace", f"nes_{pc:04X}"):
            idx = labels.get(want)
            if idx is None:
                continue
            j = idx + 1
            while j < len(lines) and not PC_COMMENT.match(lines[j]):
                if code(lines[j]).startswith("SECTION") or LABEL.match(lines[j]) or (code(lines[j]).startswith("jp ") and code(lines[j]) != "jp nz, nes_nmi_entry"):
                    j = None
                    break
                j += 1
            if j is None or j >= len(lines) or int(PC_COMMENT.match(lines[j]).group(1), 16) != pc:
                continue
            xs = reg_source(lines, j, "x")
            ys = reg_source(lines, j, "y")
            # A plain block head directly under its own SECTION is only entered
            # by jumps with all 6502 state in HRAM (no resident registers).
            if not want.endswith("_trace") and code(lines[idx - 1]).startswith("SECTION"):
                xs = xs or "hram"
                ys = ys or "hram"
            if kind == "dec_nonzero" and ys is None and y_dead_at(prg, exit_pc):
                ys = "dead"
            if kind == "delay":  # the counted register is overwritten
                if prm["y"]:
                    ys = ys or "dead"
                else:
                    xs = xs or "dead"
            if xs is None or ys is None:
                print(f"native-small-loops: ${pc:04X} {kind}: register source unknown at {want}, skipped")
                break
            new = emit(kind, pc, exit_pc, prm, xs, ys)
            lines[j:j] = new
            for q in labels:
                if labels[q] >= j:
                    labels[q] += len(new)
            done.append(f"${pc:04X} {kind} ({want}, X:{xs} Y:{ys})")
            break
    p.write_text("".join(lines))
    print(f"native-small-loops: {len(done)} loop(s): " + ", ".join(done))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
