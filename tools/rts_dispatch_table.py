#!/usr/bin/env python3
"""Hashed return tables for fan-out RTS blocks (opt-out NES2GBC_RTS_TABLE=0).

The mapper-2 emitter marks an RTS fallback whose subroutine has more likely
return sites than compare guards (`; m2 RTS table $XXXX ...`, all candidate
return PCs). A shared routine called from dozens of places (a bank-switch or
sound helper) otherwise sends most returns through nes_dispatch_hl. Here the
block's last `jp nes_dispatch_hl` (HL = return PC) becomes a jump into a
per-block table, emitted in the same section (so the same bank):

    index = L & MASK; entry -> bucket stub comparing the exact 16-bit PC
    against that bucket's candidates, else the original nes_dispatch_hl.

Matches are exact, so anything unexpected still dispatches as before.
"""
import os, re, sys

MARK = re.compile(r"^\s*; m2 RTS table (.*)$")
SEC = re.compile(r'^SECTION "[^"]*", ROMX, BANK\[(\d+)\]')
LBL = re.compile(r"^(nes_[0-9A-Fa-f]{4}):")
MAX_ENTRIES = int(os.environ.get("NES2GBC_RTS_TABLE_MAX", "64"))
BANK_BUDGET = int(os.environ.get("NES2GBC_RTS_TABLE_BUDGET", "100000"))


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    if os.environ.get("NES2GBC_RTS_TABLE", "1") == "0":
        return
    lines = open(path).read().split("\n")
    # label -> bank of its section
    bank_of, cur = {}, None
    for l in lines:
        m = SEC.match(l)
        if m:
            cur = int(m.group(1))
        elif l.startswith("SECTION"):
            cur = None
        m = LBL.match(l)
        if m and cur is not None:
            bank_of[m.group(1).upper().replace("NES_", "nes_")] = cur
    helper = "nes_jump_known_hl_a_8bit" if any(l.startswith("nes_jump_known_hl_a_8bit:") for l in lines) else "nes_jump_known_hl_a"
    out, i, n_sites, spent, cur, cnt = [], 0, 0, {}, None, 0
    # Process section by section.
    starts = [k for k, l in enumerate(lines) if l.startswith("SECTION")] + [len(lines)]
    out = lines[: starts[0]]
    for s, e in zip(starts, starts[1:]):
        sec = lines[s:e]
        m = SEC.match(sec[0])
        marks = [k for k, l in enumerate(sec) if MARK.match(l)] if m else []
        extra = []
        for k in marks:
            bank = int(m.group(1))
            pcs = sorted({int(x[1:], 16) for x in MARK.match(sec[k]).group(1).split()})
            # the fallback jp after the marker
            j = next((q for q in range(k + 1, len(sec)) if code(sec[q]).startswith(("jp", "jr", "ret", "call"))), None)
            if j is None or code(sec[j]) != "jp nes_dispatch_hl":
                continue
            if any(code(sec[q]) not in ("", "ld l, c", "inc hl") for q in range(k + 1, j)):
                continue
            size = 1
            while size < 2 * len(pcs) and size < MAX_ENTRIES:
                size *= 2
            cost = 2 * size + 14 * len(pcs) + 30
            if spent.get(bank, 0) + cost > BANK_BUDGET:
                continue
            spent[bank] = spent.get(bank, 0) + cost
            cnt += 1
            t = f"nes_rtstab_{cnt}"
            sec[j] = f"    jp {t} ; hashed RTS return table (tools/rts_dispatch_table.py)"
            buckets = {}
            for pc in pcs:
                buckets.setdefault(pc & (size - 1), []).append(pc)
            x = [f"{t}:", "    ld b, h", "    ld c, l", "    ld a, l", f"    and ${size-1:02X}",
                 "    ld l, a", "    ld h, 0", "    add hl, hl", f"    ld de, {t}_tbl", "    add hl, de",
                 "    ld a, [hli]", "    ld h, [hl]", "    ld l, a", "    jp hl",
                 f"{t}_miss:", "    ld h, b", "    ld l, c", "    jp nes_dispatch_hl"]
            for b, lst in sorted(buckets.items()):
                x.append(f"{t}_b{b}:")
                for pc in lst:
                    tgt = f"nes_{pc:04X}"
                    x += [f"    ld a, c", f"    cp ${pc & 255:02X}", "    jr nz, :+", "    ld a, b",
                          f"    cp ${pc >> 8:02X}", "    jr nz, :+"]
                    # repack_code_banks_final.py turns same-bank trampolines into direct jumps
                    x += [f"    ld a, BANK({tgt})", f"    ld hl, {tgt}", f"    jp {helper}"]
                    x.append(":")
                x.append(f"    jp {t}_miss")
            x.append(f"{t}_tbl:")
            for b in range(size):
                x.append(f"    dw {t}_b{b}" if b in buckets else f"    dw {t}_miss")
            extra += x
        # place extra code at the end of the section (before trailing blank lines)
        if extra:
            q = len(sec)
            while q > 1 and not sec[q - 1].strip():
                q -= 1
            sec = sec[:q] + [""] + extra + sec[q:]
        out += sec
    open(path, "w").write("\n".join(out))
    print(f"rts_dispatch_table: {cnt} table(s)")


if __name__ == "__main__":
    main(sys.argv[1])
