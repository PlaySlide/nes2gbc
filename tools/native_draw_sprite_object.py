#!/usr/bin/env python3
"""Run SMB's DrawSpriteObject natively.

Matches (by ROM bytes, NROM, any address P) the 72-byte leaf routine

  LDA $03 / LSR / LSR / LDA $00 / BCC a / STA $0205,Y / LDA $01 / STA $0201,Y /
  LDA #$40 / BNE b / a: STA $0201,Y / LDA $01 / STA $0205,Y / LDA #$00 /
  b: ORA $04 / STA $0202,Y / STA $0206,Y / LDA $02 / STA $0200,Y / STA $0204,Y /
  LDA $05 / STA $0203,Y / CLC / ADC #8 / STA $0207,Y / LDA $02 / CLC / ADC #8 /
  STA $02 / TYA / CLC / ADC #8 / TAY / INX / INX / RTS

(SMB $F282). The canonical entry block nes_P is replaced by GB code that
writes the two OAM entries with ld [hli], updates $02, A=Y=Y+8 (C from that
ADC), X+=2 with Z/N, then performs the routine's own translated RTS return
dispatch (copied from the block holding the RTS, from its
PROFILE_INC nes_profile_rts_pop onward, which depends only on nes_sp).
V is not reproduced, so V must be unobservable.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dead_overflow import v_observable  # noqa: E402

PAT = bytes.fromhex(
    "a5034a4aa500900c990502a501990102a940d00a990102a501990502a9000504990202990602a502990002990402a505990302186908990702a502186908850298186908a8e8e860")
LABEL_RE = re.compile(r"^nes_([0-9A-F]{4}):$")
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


def block_end(lines, i):
    e = i + 1
    while e < len(lines) and not code(lines[e]).startswith("SECTION") and not ANY_LABEL.match(code(lines[e])):
        e += 1
    return e


def rts_tail(lines, rts_pc):
    """Lines from PROFILE_INC nes_profile_rts_pop to the end of the section
    holding the translated RTS at rts_pc, plus the owning block label."""
    for i, l in enumerate(lines):
        m = PC_COMMENT.match(l)
        if not m or int(m.group(1), 16) != rts_pc:
            continue
        # owning section and its first label
        s = i
        while s > 0 and not code(lines[s]).startswith("SECTION"):
            s -= 1
        e = i + 1
        while e < len(lines) and not code(lines[e]).startswith("SECTION"):
            e += 1
        own = next((code(lines[q])[:-1] for q in range(s, i) if LABEL_RE.match(code(lines[q]))), None)
        k = next((q for q in range(i, e) if code(lines[q]) == "PROFILE_INC nes_profile_rts_pop"), None)
        if own is None or k is None:
            continue
        # nothing between the RTS comment and the pop may be required: we
        # materialize X/Y/A ourselves, so only accept the tail if it reads
        # nothing but nes_sp before its first write.
        tail = lines[k:e]
        first = next(code(x) for x in tail[1:] if code(x))
        if first != "ldh a, [nes_sp]":
            continue
        return own, tail
    return None, None


def main(asm, rom_path):
    read = prg_reader(Path(rom_path).read_bytes())
    if read is None:
        print("native-draw-sprite-object: non-NROM mapper, skipped")
        return
    p = Path(asm)
    text = p.read_text()
    if v_observable(text):
        print("native-draw-sprite-object: V observable; skipped")
        return
    lines = text.splitlines(keepends=True)
    done = 0
    i = 0
    while i < len(lines):
        m = LABEL_RE.match(code(lines[i]))
        if not m:
            i += 1
            continue
        pc = int(m.group(1), 16)
        if pc < 0x8000 or read(pc, len(PAT)) != PAT:
            i += 1
            continue
        e = block_end(lines, i)
        first = next((q for q in range(i + 1, e) if PC_COMMENT.match(lines[q])), None)
        if first is None or int(PC_COMMENT.match(lines[first]).group(1), 16) != pc:
            i += 1
            continue
        own, tail = rts_tail(lines, pc + 0x47)
        if tail is None:
            print(f"native-draw-sprite-object: no usable RTS tail for ${pc:04X}; skipped")
            i += 1
            continue
        defs = [code(x)[:-1] for x in tail if ANY_LABEL.match(code(x))]
        new_tail = []
        for x in tail:
            for d in defs:
                x = re.sub(rf"\b{re.escape(d)}\b", f"{d}_dso{pc:04X}", x)
            new_tail.append(x)
        P = f"{pc:04X}"
        body = f"""; ${P}-${pc + 0x46:04X}: DrawSpriteObject run natively
ldh a, [nes_y]
ld l, a
ld h, $C2
ld a, [$C003]
bit 1, a
ld a, [$C000]
ld b, a
ld a, [$C001]
ld c, a
ld e, $00
jr z, .dso_ok_{P}
ld a, b
ld b, c
ld c, a
ld e, $40
.dso_ok_{P}:
ld a, [$C004]
or e
ld e, a
ld a, [$C002]
ld d, a
ld [hli], a
ld a, b
ld [hli], a
ld a, e
ld [hli], a
ld a, [$C005]
ld [hli], a
add $08
ld b, a
ld a, d
ld [hli], a
ld a, c
ld [hli], a
ld a, e
ld [hli], a
ld a, b
ld [hl], a
ld a, d
add $08
ld [$C002], a
ldh a, [nes_y]
add $08
ldh [nes_y], a
ldh [nes_a], a
ld a, $00
rla
ldh [nes_c_shadow], a
ldh a, [nes_x]
add $02
ldh [nes_x], a
ldh [nes_z_shadow], a
ldh [nes_n_shadow], a""".splitlines()
        out = [(x if x.startswith(".") else "    " + x) + "\n" for x in body]
        lines[first:e] = out + new_tail
        done += 1
        i = first + len(out) + len(new_tail)
    p.write_text("".join(lines))
    print(f"native-draw-sprite-object: {done} routine(s) replaced")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
