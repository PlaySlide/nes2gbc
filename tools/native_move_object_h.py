#!/usr/bin/env python3
"""Run SMB's horizontal object movement leaf natively (SMB $BF0F).

Matches (by ROM bytes, NROM, any address P) the 62-byte routine

  LDA $57,X / ASL x4 / STA $01 / LDA $57,X / LSR x4 / CMP #8 / BCC + /
  ORA #$F0 / +: STA $00 / LDY #0 / CMP #0 / BPL + / DEY / +: STY $02 /
  LDA $0400,X / CLC / ADC $01 / STA $0400,X / LDA #0 / ROL / PHA / ROR /
  LDA $86,X / ADC $00 / STA $86,X / LDA $6D,X / ADC $02 / STA $6D,X /
  PLA / CLC / ADC $00 / RTS

At its translated entry `nes_P_trace` GB code performs the same memory
writes ($00-$02, $0400,X, $86,X, $6D,X and the PHA byte at $0100+SP),
materializes A/X/Y/Z/N/C (V is not reproduced, so V must be unobservable)
and enters the routine's own translated RTS return dispatch (which starts
by reading nes_sp). Other entries into the routine stay translated. Runs
after the peephole passes, so the code it splices into is final.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dead_overflow import v_observable  # noqa: E402

PAT = bytes.fromhex(
    "b5570a0a0a0a8501b5574a4a4a4ac908900209f08500a000c9001001888402"
    "bd00041865019d0004a9002a486ab58665009586b56d6502956d6818650060")
PC_COMMENT = re.compile(r"^\s*; \$([0-9A-F]{4}): \$[0-9A-F]{2} ")
TRACE_RE = re.compile(r"^nes_([0-9A-F]{4})_trace:$")
GLABEL = re.compile(r"^[A-Za-z_]\w*:")


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


def bank_of(lines, i):
    while i > 0 and not lines[i].startswith("SECTION"):
        i -= 1
    m = re.search(r"BANK\[(\d+)\]", lines[i])
    return m.group(1) if m else None


def body(P, load_x, rts):
    return [s + "\n" for s in [
        f"    ; ${P}: horizontal object movement run natively (tools/native_move_object_h.py)",
        *(["    ldh a, [nes_x]", "    ld b, a"] if load_x else []),
        "    ld a, b",
        "    add $57",
        "    ld l, a",
        "    ld h, $C0",
        "    ld a, [hl] ; LDA $57,X",
        "    ld c, a",
        "    swap a",
        "    and $F0",
        "    ld [$C001], a ; low nibble << 4",
        "    ld e, a",
        "    ld a, c",
        "    swap a",
        "    and $0F",
        "    cp $08",
        f"    jr c, .nmoh_pos_{P}",
        "    or $F0",
        f".nmoh_pos_{P}:",
        "    ld [$C000], a ; sign-extended high nibble",
        "    ld d, a",
        "    add a",
        "    sbc a ; Y = $FF if negative else 0",
        "    ld [$C002], a",
        "    ldh [nes_y], a",
        "    ld c, a",
        "    ld h, $C4",
        "    ld l, b",
        "    ld a, [hl]",
        "    add e",
        "    ld [hl], a ; $0400,X += $01",
        "    ld a, $00",
        "    rla",
        "    ld e, a ; carry as 0/1",
        "    ldh a, [nes_sp]",
        "    ld l, a",
        "    ld h, $C1",
        "    ld [hl], e ; PHA byte (PLA restores SP)",
        "    ld a, b",
        "    add $86",
        "    ld l, a",
        "    ld h, $C0",
        "    ld a, e",
        "    rra",
        "    ld a, [hl]",
        "    adc d",
        "    ld [hl], a ; $86,X += $00 + C",
        "    push af",
        "    ld a, b",
        "    add $6D",
        "    ld l, a",
        "    pop af",
        "    ld a, [hl]",
        "    adc c",
        "    ld [hl], a ; $6D,X += $02 + C",
        "    ld a, e",
        "    add d",
        "    ld l, a",
        "    sbc a",
        "    ldh [nes_c_shadow], a",
        "    ld a, l",
        "    ldh [nes_a], a",
        "    ldh [nes_z_shadow], a",
        "    ldh [nes_n_shadow], a",
        "    ld a, b",
        "    ldh [nes_x], a",
        f"    jp {rts}",
    ]]


def main(asm, rom_path):
    read = prg_reader(Path(rom_path).read_bytes())
    if read is None:
        print("native-move-object-h: non-NROM mapper, skipped")
        return
    p = Path(asm)
    text = p.read_text()
    lines = text.splitlines(keepends=True)
    sites = []
    for i, l in enumerate(lines):
        m = TRACE_RE.match(code(l))
        if m and int(m.group(1), 16) >= 0x8000 and read(int(m.group(1), 16), len(PAT)) == PAT:
            sites.append(i)
    if not sites:
        print("native-move-object-h: routine not present")
        return
    if v_observable(text):
        print("native-move-object-h: V observable; skipped")
        return
    n = 0
    for ent in reversed(sites):
        pc = int(TRACE_RE.match(code(lines[ent])).group(1), 16)
        P = f"{pc:04X}"
        j = ent + 1
        if code(lines[j]).startswith("IF DEF(NES2GBC_PROFILE_TRACE)"):
            while code(lines[j]) != "ENDC":
                j += 1
            j += 1
        m = PC_COMMENT.match(lines[j])
        first = next((code(x) for x in lines[j + 1:j + 4] if code(x)), "")
        if not m or int(m.group(1), 16) != pc or first not in ("ld a, b", "ldh a, [nes_x]"):
            print(f"native-move-object-h: unexpected entry shape at ${P}; skipped")
            continue
        rts_pc = pc + len(PAT) - 1
        r = next((i for i, l in enumerate(lines) if (mm := PC_COMMENT.match(l)) and int(mm.group(1), 16) == rts_pc), None)
        if r is None:
            print(f"native-move-object-h: no RTS block for ${P}; skipped")
            continue
        k = r
        while k < len(lines) and code(lines[k]) != "PROFILE_INC nes_profile_rts_pop":
            if lines[k].startswith("SECTION") or GLABEL.match(code(lines[k])):
                k = len(lines)
                break
            k += 1
        if k >= len(lines):
            print(f"native-move-object-h: no RTS pop for ${P}; skipped")
            continue
        nxt = next(code(x) for x in lines[k + 1:] if code(x))
        if nxt != "ldh a, [nes_sp]" or bank_of(lines, k) != bank_of(lines, ent):
            print(f"native-move-object-h: unusable RTS tail for ${P}; skipped")
            continue
        g = k
        while not GLABEL.match(code(lines[g])):
            g -= 1
        rts = f"{code(lines[g]).split(':')[0]}.nmoh_rts_{P}"
        if k > j:
            lines[k:k] = [f".nmoh_rts_{P}: ; native movement return (state in HRAM)\n"]
            lines[j:j] = body(P, first != "ld a, b", rts)
        else:
            lines[j:j] = body(P, first != "ld a, b", rts)
            k2 = k + len(body(P, first != "ld a, b", rts))
            lines[k2:k2] = [f".nmoh_rts_{P}: ; native movement return (state in HRAM)\n"]
        n += 1
    p.write_text("".join(lines))
    print(f"native-move-object-h: {n} routine{'s' if n != 1 else ''} run natively")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
