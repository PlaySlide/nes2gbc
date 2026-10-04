#!/usr/bin/env python3
"""Run SMB-style block-buffer collision (BlockBufferCollision) natively.

Matches, by ROM bytes (NROM only), the routine at a translated superblock
entry nes_XXXX_trace:

    PHA / STY $04 / LDA T1,Y / CLC / ADC zA,X / STA $05 / LDA zB,X / ADC #0 /
    AND #1 / LSR / ORA $05 / ROR / LSR / LSR / LSR / JSR S /
    LDY $04 / LDA zC,X / CLC / ADC T2,Y / AND #$F0 / SEC / SBC #$20 / STA $02 /
    TAY / LDA ($06),Y / STA $03 / LDY $04 / PLA / BNE +5 / LDA zC,X /
    JMP K / LDA zA,X / K: AND #$0F / STA $04 / LDA $03 / RTS
    S:  PHA / LSR x4 / TAY / LDA H,Y / STA $07 / PLA / AND #$0F / CLC /
        ADC L,Y / STA $06 / RTS

(SMB $E3F0 with S = $9BE1). The block body is replaced by straight GB code
that performs everything up to K and then jumps to the translated K block
(canonical HRAM state): ZP $02-$07, the PHA/JSR/PHA stack bytes, A, Y,
6502 C and V from SBC #$20. S's index is A>>4 with A <= 31, so its table
reads become immediates; T1,Y / T2,Y are embedded 256-byte ROM copies.
Only applies when both H entries are RAM pages (< $1F).
"""
from __future__ import annotations
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dead_overflow import v_observable  # noqa: E402

LABEL_RE = re.compile(r"^(nes_([0-9A-F]{4})_trace):$")
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


def match(read, pc):
    b = read(pc, 66)
    W = None
    pat = [0x48, 0x84, 0x04, 0xB9, W, W, 0x18, 0x75, W, 0x85, 0x05, 0xB5, W, 0x69, 0x00,
           0x29, 0x01, 0x4A, 0x05, 0x05, 0x6A, 0x4A, 0x4A, 0x4A, 0x20, W, W,
           0xA4, 0x04, 0xB5, W, 0x18, 0x79, W, W, 0x29, 0xF0, 0x38, 0xE9, 0x20, 0x85, 0x02,
           0xA8, 0xB1, 0x06, 0x85, 0x03, 0xA4, 0x04, 0x68, 0xD0, 0x05, 0xB5, W, 0x4C, W, W,
           0xB5, W, 0x29, 0x0F, 0x85, 0x04, 0xA5, 0x03, 0x60]
    if len(pat) != 66 or any(p is not None and p != x for p, x in zip(pat, b)):
        return None
    zA, zB, zC = b[8], b[12], b[30]
    if b[53] != zC or b[58] != zA:
        return None
    t1 = b[4] | b[5] << 8
    s = b[25] | b[26] << 8
    t2 = b[33] | b[34] << 8
    k = b[55] | b[56] << 8
    if k != pc + 59:
        return None
    sb = read(s, 21)
    spat = [0x48, 0x4A, 0x4A, 0x4A, 0x4A, 0xA8, 0xB9, W, W, 0x85, 0x07, 0x68, 0x29, 0x0F,
            0x18, 0x79, W, W, 0x85, 0x06, 0x60]
    if any(p is not None and p != x for p, x in zip(spat, sb)):
        return None
    hi_t = sb[7] | sb[8] << 8
    lo_t = sb[16] | sb[17] << 8
    H = read(hi_t, 2)
    L = read(lo_t, 2)
    if max(H) > 0x1E:
        return None
    return dict(zA=zA, zB=zB, zC=zC, T1=read(t1, 256), T2=read(t2, 256), H=H, L=L,
                ret=pc + 26, k=k, t1=t1, t2=t2, s=s)


V_BLOCK = """ld a, d
cpl
and e
and $80
rrca
ld e, a
ldh a, [nes_p]
and $BF
or e
ldh [nes_p], a
"""


def emit(pc, m, keep_v=True, krts=False):
    K = f"{m['k']:04X}"
    if krts:
        # K: AND #$0F / STA $04 / LDA $03 (Z/N) inline; K's RTS stays shared.
        keep03 = "ld e, a\n"
        ktail = (f"and $0F\nld [$C004], a\nld a, e\nldh [nes_z_shadow], a\nldh [nes_n_shadow], a\n"
                 f"ldh [nes_a], a\nld a, BANK(nes_{K}_rts)\nld hl, nes_{K}_rts\n"
                 "jp nes_jump_known_hl_a_8bit ; 8-bit translated-code bank switch\n")
    else:
        keep03 = ""
        ktail = (f"ldh [nes_a], a\nld a, BANK(nes_{K})\nld hl, nes_{K}\n"
                 "jp nes_jump_known_hl_a_8bit ; 8-bit translated-code bank switch\n")
    ind = "    "
    vblock = V_BLOCK if keep_v else "; V not observable in this ROM: overflow flag not produced\n"
    t1 = f"nes_native_bbc_t1_{pc:04X}"
    t2 = f"nes_native_bbc_t2_{pc:04X}"
    r = m["ret"]
    body = f"""; ${pc:04X}-${m['k'] - 1:04X} (+ JSR ${m['s']:04X}): block-buffer collision run natively
ld a, b
ldh [nes_x], a
ldh a, [nes_sp]
ld l, a
ld h, $C1
ldh a, [nes_a]
ld [hl], a
dec l
ld [hl], ${r >> 8:02X}
dec l
ld [hl], ${r & 0xFF:02X}
dec l
push hl
ldh a, [nes_y]
ld c, a
ld [$C004], a
ld h, HIGH({t1})
ld l, a
ld e, [hl]
ld a, b
add ${m['zA']:02X}
ld l, a
ld h, $C0
ld a, [hl]
add e
ld d, a
ld [$C005], a
ld a, $00
rla
ld e, a
ld a, b
add ${m['zB']:02X}
ld l, a
ld a, [hl]
add e
and $01
rra
ld a, d
rra
srl a
srl a
srl a
pop hl
ld [hl], a
ld e, a
and $0F
ld d, a
bit 4, e
jr nz, .bbc_y1_{pc:04X}
ld a, ${m['H'][0]:02X}
ld [$C007], a
ld h, a
ld a, ${m['L'][0]:02X}
jr .bbc_yd_{pc:04X}
.bbc_y1_{pc:04X}:
ld a, ${m['H'][1]:02X}
ld [$C007], a
ld h, a
ld a, ${m['L'][1]:02X}
.bbc_yd_{pc:04X}:
add d
ld [$C006], a
ld l, a
push hl
ld h, HIGH({t2})
ld l, c
ld e, [hl]
ld a, b
add ${m['zC']:02X}
ld l, a
ld h, $C0
ld a, [hl]
add e
and $F0
ld e, a
sub $20
ld [$C002], a
ld d, a
sbc a
inc a ; 6502 C = !borrow as 1/0
ldh [nes_c_shadow], a
{vblock}pop hl
ld a, l
add d
ld l, a
ld a, h
adc $00
and $07
or $C0
ld h, a
ld a, [hl]
ld [$C003], a
{keep03}ld a, c
ldh [nes_y], a
ldh a, [nes_a]
and a
ld a, b
jr nz, .bbc_za_{pc:04X}
add ${m['zC']:02X}
jr .bbc_ld_{pc:04X}
.bbc_za_{pc:04X}:
add ${m['zA']:02X}
.bbc_ld_{pc:04X}:
ld l, a
ld h, $C0
ld a, [hl]
{ktail}""".splitlines()
    out = []
    for x in body:
        if x.endswith(":") or x.startswith(";"):
            out.append((x if x.endswith(":") else ind + x) + "\n")
        else:
            out.append(ind + x + "\n")
    def db(bs):
        return [ind + "db " + ", ".join(f"${v:02X}" for v in bs[i:i + 16]) + "\n" for i in range(0, 256, 16)]
    # 256-byte-aligned ROM0 copies: the index is the low byte (any code bank).
    tables = [f"\nSECTION \"Native bbc tables {pc:04X}\", ROM0, ALIGN[8]\n", f"{t1}:\n"] + db(m["T1"])
    tables += [f"{t2}:\n"] + db(m["T2"])
    return out, tables


def label_k_rts(lines, k):
    """Label K's RTS (after AND #$0F / STA $04 / LDA $03 materialized) as
    nes_K_rts so the native body can run K's three instructions itself."""
    K = f"{k:04X}"
    if any(code(l) == f"nes_{K}_rts:" for l in lines):
        return True
    for j, l in enumerate(lines):
        if code(l) != f"nes_{K}:":
            continue
        q = j + 1
        while q < len(lines) and not code(lines[q]).startswith("SECTION") and not ANY_LABEL.match(code(lines[q])):
            if code(lines[q]) == "PROFILE_INC nes_profile_rts_pop":
                prev = [code(x) for x in lines[j:q] if code(x)]
                pcs = [int(PC_COMMENT.match(x).group(1), 16) for x in lines[j:q] if PC_COMMENT.match(x)]
                if prev[-4:] == ["ld a, [$C003]", "ldh [nes_z_shadow], a", "ldh [nes_n_shadow], a", "ldh [nes_a], a"] \
                        and pcs == [k, k + 2, k + 4, k + 6]:
                    lines.insert(q, f"nes_{K}_rts:\n")
                    return True
                return False
            q += 1
        return False
    return False


def main(asm, rom_path):
    read = prg_reader(Path(rom_path).read_bytes())
    if read is None:
        print("native-blockbuf-collision: non-NROM mapper, skipped")
        return
    p = Path(asm)
    text = p.read_text()
    keep_v = v_observable(text)
    lines = text.splitlines(keepends=True)
    done = 0
    extra = []
    i = 0
    while i < len(lines):
        mm = LABEL_RE.match(code(lines[i]))
        if not mm:
            i += 1
            continue
        pc = int(mm.group(2), 16)
        if pc < 0x8000:
            i += 1
            continue
        m = match(read, pc)
        if not m or not any(re.fullmatch(rf"nes_{m['k']:04X}:", code(l)) for l in lines):
            i += 1
            continue
        here = lines[i]
        krts = label_k_rts(lines, m["k"])
        if lines[i] is not here:
            i += 1  # K's label was inserted above this block
        e = i + 1
        while e < len(lines) and not code(lines[e]).startswith("SECTION") and not ANY_LABEL.match(code(lines[e])):
            e += 1
        first = next((q for q in range(i + 1, e) if PC_COMMENT.match(lines[q])), None)
        if first is None or int(PC_COMMENT.match(lines[first]).group(1), 16) != pc:
            i += 1
            continue
        # The block must end at the JSR transfer (pc+24); nothing after it.
        pcs = [int(PC_COMMENT.match(lines[q]).group(1), 16) for q in range(first, e) if PC_COMMENT.match(lines[q])]
        if max(pcs) != pc + 24:
            i += 1
            continue
        body, tables = emit(pc, m, keep_v, krts)
        lines[first:e] = body
        extra.extend(tables)
        done += 1
        i = first + 1
    lines.extend(extra)
    p.write_text("".join(lines))
    print(f"native-blockbuf-collision: {done} routine(s) replaced")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
