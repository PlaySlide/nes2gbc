#!/usr/bin/env python3
"""Run the standard serial joypad-read loop natively.

Matches a translated block nes_XXXX whose 6502 bytes (checked in the ROM) are

    XXXX: PHA / LDA $4016,X (or $4017,X) / STA zp / LSR / ORA zp / LSR / PLA /
          ROL / DEY / BNE XXXX

(Nintendo's ReadPortBits idiom: bit0|bit1 of each read, Famicom expansion
compatible; SMB $8E6C). The block's loop body is replaced by
`call nes_joy_serial_loop`, which performs all Y (0 = 256) iterations with
the same observable effects: A, C from the last ROL, Y = 0 with Z set/N
clear, zp = last read value, the PHA stack byte, controller shift state;
X/V/SP unchanged. The block prologue (trace hook, NMI poll) and its
fall-through tail after the self-branch are kept.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

LABEL_RE = re.compile(r"^(nes_([0-9A-F]{4})):$")
PC_COMMENT = re.compile(r"^\s*; \$([0-9A-F]{4}): \$[0-9A-F]{2} ")


def code(l: str) -> str:
    return l.split(";", 1)[0].strip()


def prg_reader(rom: bytes):
    banks = rom[4]
    trainer = 512 if rom[6] & 4 else 0
    prg = rom[16 + trainer:16 + trainer + banks * 16384]
    if banks not in (1, 2) or (rom[6] >> 4 | (rom[7] & 0xF0)) != 0:
        return None  # only NROM: fixed PC -> byte mapping
    return lambda pc, n: prg[(pc - 0x8000) % len(prg):(pc - 0x8000) % len(prg) + n]


def matches(b: bytes) -> tuple[int, int] | None:
    # 48 BD 16|17 40 85 zz 4A 05 zz 4A 68 2A 88 D0 F3
    if len(b) < 15:
        return None
    if b[0] == 0x48 and b[1] == 0xBD and b[2] in (0x16, 0x17) and b[3] == 0x40 and b[4] == 0x85 \
            and b[6] == 0x4A and b[7] == 0x05 and b[8] == b[5] and b[9:15] == bytes([0x4A, 0x68, 0x2A, 0x88, 0xD0, 0xF1]):
        return b[2], b[5]
    return None


def main(asm: str, rom_path: str) -> None:
    read = prg_reader(Path(rom_path).read_bytes())
    p = Path(asm)
    lines = p.read_text().splitlines(keepends=True)
    if read is None:
        print("native-joypad-loops: non-NROM mapper, skipped")
        return
    done = 0
    for i, l in enumerate(lines):
        m = LABEL_RE.match(code(l))
        if not m:
            continue
        pc = int(m.group(2), 16)
        if pc < 0x8000:
            continue
        mt = matches(read(pc, 15))
        if not mt:
            continue
        lo, zp = mt
        label = m.group(1)
        # Block extent.
        e = i + 1
        while e < len(lines) and not code(lines[e]).startswith("SECTION") and not LABEL_RE.match(code(lines[e])):
            e += 1
        first = next((k for k in range(i + 1, e) if PC_COMMENT.match(lines[k])), None)
        if first is None or int(PC_COMMENT.match(lines[first]).group(1), 16) != pc:
            continue
        seen = {int(PC_COMMENT.match(lines[k]).group(1), 16) for k in range(first, e) if PC_COMMENT.match(lines[k])}
        want = {pc, pc + 1, pc + 4, pc + 6, pc + 7, pc + 9, pc + 10, pc + 11, pc + 12, pc + 13}
        if not want <= seen:
            continue
        back = [k for k in range(first, e) if re.match(rf"^jp nz, {label}\b", code(lines[k]))]
        if len(back) != 1:
            continue
        k = back[0]
        ind = "    "
        lines[first:k + 1] = [
            f"{ind}; ${pc:04X}-${pc + 14:04X}: serial joypad loop (PHA/LDA $40{lo:02X},X/STA ${zp:02X}/LSR/ORA/LSR/PLA/ROL/DEY/BNE) run natively\n",
            f"{ind}ld de, ${lo:02X}{zp:02X}\n",
            f"{ind}call nes_joy_serial_loop\n",
            f"{ind}xor a ; loop exit leaves the BNE fall-through host flags (Z, NC)\n",
        ]
        done += 1
        break  # line indices shifted; rescan
    p.write_text("".join(lines))
    print(f"native-joypad-loops: {done} loop(s) replaced")
    if done:
        main(asm, rom_path)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
