#!/usr/bin/env python3
"""Batch SMB's VRAM-buffer copy loop into one runtime call.

Matches (by ROM bytes, NROM) the loop

  L:   BCS +1 / INY / LDA (zz),Y / STA $2007 / DEX / BNE L      ; L = $8EB6

entered by fall-through from the translated trace that sets X (count) and C
(repeat flag). Right before the loop's first BCS in that trace (all NES state
already materialized in HRAM), it inserts a call to nes_ppu_write_run
(runtime/ppu.asm). When every byte of the run would take the stitched $2007
fast path, the routine performs the whole loop with identical effects and
the code continues at the loop exit's canonical block; otherwise nothing has
changed and the translated loop runs as before.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

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


def main(asm, rom_path):
    read = prg_reader(Path(rom_path).read_bytes())
    if read is None:
        print("native-vram-run: non-NROM mapper, skipped")
        return
    p = Path(asm)
    text = p.read_text()
    if "nes_ppu_write_run" not in Path(__file__).resolve().parent.parent.joinpath("runtime/ppu.asm").read_text():
        print("native-vram-run: runtime routine missing; skipped")
        return
    cfg = Path(asm).parent / "generated_config.inc"
    if cfg.exists() and "NES2GBC_NO_STITCH_WRITE_FASTPATH" in cfg.read_text():
        print("native-vram-run: stitched write fast path compiled out; skipped")
        return
    lines = text.splitlines(keepends=True)
    labels = {code(l) for l in lines}
    n = 0
    for loop in range(0x8000, 0x10000 - 11):
        b = read(loop, 11)
        if not (b[0:4] == bytes([0xB0, 0x01, 0xC8, 0xB1]) and b[5:11] == bytes([0x8D, 0x07, 0x20, 0xCA, 0xD0, 0xF5])):
            continue
        zp = b[4]
        if zp == 0xFF:
            continue
        exit_pc = loop + 11
        if f"nes_{exit_pc:04X}:" not in labels:
            print(f"native-vram-run: no canonical exit block for ${loop:04X}; skipped")
            continue
        # Find a trace block that falls into the loop head after TAX.
        for i, l in enumerate(lines):
            m = PC_COMMENT.match(lines[i])
            if not m or int(m.group(1), 16) != loop:
                continue
            # Must be inside a *_trace block (not the canonical head).
            j = i - 1
            while j >= 0 and not ANY_LABEL.match(code(lines[j])) and not code(lines[j]).startswith("SECTION"):
                j -= 1
            if j < 0 or not code(lines[j]).endswith("_trace:"):
                continue
            prev = [code(x) for x in lines[j:i] if code(x)]
            tail = prev[-4:]
            if tail != ["ldh [nes_x], a", "ld a, c", "ldh [nes_y], a"][-3:] and \
                    prev[-3:] != ["ldh [nes_x], a", "ld a, c", "ldh [nes_y], a"]:
                print(f"native-vram-run: unexpected trace shape before ${loop:04X}: {prev[-4:]}")
                continue
            if "ldh [nes_c_shadow], a" not in prev:
                print(f"native-vram-run: C not materialized before ${loop:04X}; skipped")
                continue
            ins = [
                f"    ; native VRAM run (tools/native_vram_run.py): ${loop:04X}-${exit_pc - 1:04X}\n",
                f"    ld hl, ${0xC000 + zp:04X}\n",
                "    call nes_ppu_write_run\n",
                "    and a\n",
                "    jr z, .vram_run_slow_%04X\n" % loop,
                f"    ld a, BANK(nes_{exit_pc:04X})\n",
                f"    ld hl, nes_{exit_pc:04X}\n",
                "    jp nes_jump_known_hl_a_8bit ; 8-bit translated-code bank switch\n",
                ".vram_run_slow_%04X:\n" % loop,
            ]
            lines[i:i] = ins
            n += 1
            break
    p.write_text("".join(lines))
    print(f"native-vram-run: {n} site{'s' if n != 1 else ''} patched")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
