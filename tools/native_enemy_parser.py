#!/usr/bin/env python3
"""Native fast path for SMB's enemy-object data parser ($C144..$C1D4), ROM-byte matched.

Hot path (about three times per frame): read the two enemy-data bytes at
($E9),Y with Y=[$0739], update the screen-edge coordinates ($06/$07), the
page-flag bookkeeping ($073A/$073B), store the object's page/Y ($6E,X / $87,X)
and find the object still right of the screen, ending at the canonical $C216
block (the 6502 BCC at $C1D3 taken) with exact A/Y/N/Z/C (V is never produced,
so the ROM must not observe V; see tools/dead_overflow.py).

Guards (decided before any store, otherwise the untouched translation runs):
($E9)+Y in PRG $8000-$BEFF, Y != $FF, the data is not the $FF terminator,
the path reaches $C164 (no early RTS), and not the $C192 page-select command.
After the stores, the two rare branches re-enter the canonical $C1AB / $C1CB
blocks, which only re-store identical values before branching.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dead_overflow import v_observable  # noqa: E402
from native_small_loops import prg_view, code, LABEL, PC_COMMENT, tail  # noqa: E402

PATTERN = bytes.fromhex(
    "ac3907b1e9c9ffd0034c16c2290fc90ef00ee005900ac8b1e9293fc92ef00160ad1d0718693029f08507ad1b0769008506"
    "ac3907c8b1e90a900bad3b07d006ee3b07ee3a0788b1e9290fc90fd019ad3b07d014c8b1e9293f8d3a07ee3907ee3907"
    "ee3b074cccc0ad3a07956eb1e929f09587cd1d07b56eed1b07b00bb1e9290fc90ef0694c50c2a507d587a506f56e9041")


EXIT_PC = PATTERN[10] | PATTERN[11] << 8  # JMP $C216 operand


def jump(target):
    return [f"ld a, BANK(nes_{target:04X})", f"ld hl, nes_{target:04X}",
            "jp nes_jump_known_hl_a_8bit ; 8-bit translated-code bank switch"]


def emit(pc):
    k = f"nep_{pc:04X}"
    exit_pc = EXIT_PC
    c1ab, c1cb = pc + 0x67, pc + 0x87

    L = [f"; native SMB enemy-data parser ${pc:04X} (tools/native_enemy_parser.py)",
         "ld a, [$C739]", "ld c, a", "inc a", f"jp z, .{k}_slow",  # INY must not wrap
         "ld a, [$C0E9]", "add c", "ld l, a", "ld a, [$C0EA]", "adc $00", "ld h, a",
         "sub $80", "cp $3F", f"jp nc, .{k}_slow",  # q in $8000-$BEFF
         # one SVBK window for both bytes: e=[q], d=[q+1]; a 4K-window crossing
         # (q+1 = $x000) takes the translated path
         "ld a, h", "and $30", "swap a", "add $02", "ldh [rSVBK], a",
         "ld a, h", "and $0F", "or $D0", "ld h, a", "ld a, [hl]", "ld e, a",
         "inc l", f"jr nz, .{k}_rd1",
         "inc h", "ld a, h", "cp $E0", f"jp z, .{k}_slow",
         f".{k}_rd1:", "ld a, [hl]", "ld d, a"] + [
         "ld a, e", "cp $FF", f"jp z, .{k}_slow",
         "and $0F", "cp $0E", f"jr z, .{k}_hot",
         "ldh a, [nes_x]", "cp $05", f"jr c, .{k}_hot",
         "ld a, d", "and $3F", "cp $2E", f"jp nz, .{k}_early",
         f".{k}_hot:",
         "ld a, e", "and $0F", "cp $0F", f"jr nz, .{k}_np",
         "ld a, [$C73B]", "and a", f"jr nz, .{k}_np",
         "bit 7, d", f"jp z, .{k}_slow",  # $C192 page select command
         f".{k}_np:",
         # $C164: $07 = ([$071D]+$30)&$F0, $06 = [$071B]+carry
         "ld a, [$C71D]", "add $30", "ld b, a", "ld a, [$C71B]", "adc $00", "ld [$C006], a",
         "ld a, b", "and $F0", "ld [$C007], a",
         # $C175: ASL [q+1]; carry and [$073B]==0 -> INC $073B / INC $073A
         "bit 7, d", f"jr z, .{k}_c189",
         "ld a, [$C73B]", "and a", f"jr nz, .{k}_c189",
         "inc a", "ld [$C73B], a", "ld hl, $C73A", "inc [hl]",
         f".{k}_c189:",
         # $C1AB
         "ld a, e", "and $F0", "ld c, a",
         "ldh a, [nes_x]", "ld b, a", "add $6E", "ld e, a", "ld d, $C0",
         "ld a, b", "add $87", "ld l, a", "ld h, $C0",
         "ld a, [$C73A]", "ld [de], a", "ld a, c", "ld [hl], a",
         "ld a, [$C71D]", "ld b, a", "ld a, c", "cp b",  # GB carry = !C
         "ld a, [$C71B]", "ld b, a", "ld a, [de]", "sbc b",
         f"jp c, .{k}_b1ab",  # 6502 BCS $C1CB not taken: rare, re-run $C1AB
         # $C1CB
         "ld a, [$C007]", "cp [hl]", "ld a, [de]", "ld b, a", "ld a, [$C006]", "sbc b",
         f"jp nc, .{k}_b1cb",  # 6502 BCC not taken: rare, re-run $C1CB
         "ldh [nes_a], a", "ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a",
         "xor a", "ldh [nes_c_shadow], a", "ld a, [$C739]", "ldh [nes_y], a"] + jump(exit_pc) + [
         f".{k}_b1ab:", "ld a, [$C739]", "ldh [nes_y], a"] + jump(c1ab) + [
         f".{k}_b1cb:", "ld a, [$C739]", "ldh [nes_y], a"] + jump(c1cb) + [
         # $C15A..$C163: INY / LDA ($E9),Y / AND #$3F / CMP #$2E / BEQ (not taken) / RTS
         f".{k}_early:", "ldh [nes_a], a", "sub $2E", "ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a",
         "sbc a", "inc a", "ldh [nes_c_shadow], a", "ld a, c", "inc a", "ldh [nes_y], a",
         f"ld a, BANK(nes_{pc + 0x1F:04X}_trace)", f"ld hl, nes_{pc + 0x1F:04X}_trace",
         "jp nes_jump_known_hl_a_8bit ; translated RTS block (all state in HRAM)",
         f".{k}_slow:"]
    return [(x if (x.endswith(":") and not x.startswith(";")) else "    " + x) + "\n" for x in L]


def main(asm, rom_path):
    prg = prg_view(Path(rom_path).read_bytes())
    if prg is None:
        print("native-enemy-parser: non-NROM mapper, skipped")
        return
    o = prg.find(PATTERN)
    if o < 0:
        print("native-enemy-parser: pattern not found")
        return
    pc = 0x8000 + o
    if pc != 0xC144:  # absolute JMP targets inside the pattern pin its location
        print(f"native-enemy-parser: pattern at ${pc:04X}, expected $C144, skipped")
        return
    p = Path(asm)
    text = p.read_text()
    if v_observable(text):
        print("native-enemy-parser: V observable, skipped")
        return
    lines = text.splitlines(keepends=True)
    labels = {m.group(1): i for i, l in enumerate(lines) if (m := LABEL.match(l))}
    need = [f"nes_{a:04X}" for a in (pc, EXIT_PC, pc + 0x67, pc + 0x87)] + [f"nes_{pc + 0x1F:04X}_trace"]
    if any(n not in labels for n in need):
        print("native-enemy-parser: canonical labels missing, skipped")
        return
    idx = labels[need[0]]
    if not code(lines[idx - 1]).startswith("SECTION"):
        print("native-enemy-parser: entry not a plain block head, skipped")
        return
    j = idx + 1
    while j < len(lines) and not PC_COMMENT.match(lines[j]):
        if code(lines[j]).startswith(("SECTION", "jp ")) or LABEL.match(lines[j]):
            print("native-enemy-parser: unexpected block head, skipped")
            return
        j += 1
    if int(PC_COMMENT.match(lines[j]).group(1), 16) != pc:
        print("native-enemy-parser: entry PC mismatch, skipped")
        return
    lines[j:j] = emit(pc)
    p.write_text("".join(lines))
    print(f"native-enemy-parser: ${pc:04X} hot path native (exit ${EXIT_PC:04X})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
