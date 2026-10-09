#!/usr/bin/env python3
"""Banked mappers: resolve PRG-ROM reads whose region is known at translation time.

fast_nonram_reads.py leaves indexed reads of a constant PRG base as

    ld hl, $BASE / <A = index> / add l / ld l, a / jr nc, :+ / inc h / :
    ld a, h / cp $20 / jr nc, :+ / and $07 / or $C0 / ld h, a / ld a, [hl] / jr :++
    : / call nes_cpu_read_hi / :

When $8000 <= BASE and BASE + $FF stays inside one 16 KiB window, the RAM
test can never pass and nes_cpu_read_hi's region tests are fixed. The read
becomes a call to a ROM0 helper that maps the window's GBC bank directly:

* $8000-$BFFF from code translated for PRG bank K at $8000-$BFFF (label
  nes_m2_bKK_* with an instruction PC in that window): the CPU is fetching
  from bank K, so the switchable window holds bank K -> `ld a, K+1` /
  `call nes_prg_read_lo_a`.
* $8000-$BFFF from other code -> `call nes_prg_read_lo_dyn` (nes_prg_bank).
* $C000-$FFFF on UxROM -> `ld a, LAST+1` / `call nes_prg_read_hi_a`;
  on other banked mappers `call nes_prg_read_hi_dyn` (nes_prg_hi_bank).

Helpers clobber only A, L and flags, like nes_cpu_read_hi. Set
NES2GBC_PRG_READ_CHECK=1 to emit the checked lo helper (faults if the view
bank assumption is ever wrong). Mapper 0/3 builds are untouched.
"""
import os, re, sys

TAIL = ["ld a, h", "cp $20", "jr nc, :+", "and $07", "or $C0", "ld h, a", "ld a, [hl]", "jr :++", ":"]
IDX = ["add l", "ld l, a", "jr nc, :+", "inc h", ":"]
ALOAD = re.compile(r"^(ldh a, \[nes_[xy]\]|ld a, [bcde])$")
LABEL = re.compile(r"^(nes_\w+):")
M2 = re.compile(r"^nes_m2_b([0-9A-F]{2})_([0-9A-F]{4})$")
PC = re.compile(r";\s*\$([0-9A-F]{4}): \$")


def code(l):
    return l.split(";", 1)[0].strip()


def main(path, rom):
    text = open(path).read()
    m = re.search(r"nes_generated_init:\n\s+ld a, \$([0-9A-F]{2})\n\s+ld \[nes_mapper\], a", text)
    mapper = int(m.group(1), 16) if m else None
    if mapper not in (1, 2):
        print(f"banked-static-prg-reads: mapper {mapper}, skipped")
        return
    hdr = open(rom, "rb").read(16)
    last = hdr[4] - 1
    check = os.environ.get("NES2GBC_PRG_READ_CHECK") == "1"
    L = text.splitlines(keepends=True)
    C = [code(l) for l in L]
    label = None
    pc = None
    out = []
    n = {"lo_a": 0, "lo_dyn": 0, "hi_a": 0, "hi_dyn": 0}
    skipped = 0
    i = 0
    edits = {}
    for i, l in enumerate(L):
        mm = LABEL.match(l)
        if mm:
            label = mm.group(1)
        mp = PC.search(l)
        if mp:
            pc = int(mp.group(1), 16)
        if C[i] != "call nes_cpu_read_hi":
            continue
        # walk back over non-code lines collecting code-line indices
        ks = []
        j = i - 1
        while j >= 0 and len(ks) < 20:
            if C[j]:
                ks.append(j)
            elif LABEL.match(L[j]) or L[j].lstrip().startswith(("IF", "ENDC", "ELSE")):
                break
            j -= 1
        ks.reverse()
        if len(ks) < len(TAIL) + len(IDX) + 2:
            skipped += 1
            continue
        tail = ks[-len(TAIL):]
        idx = ks[-len(TAIL) - len(IDX):-len(TAIL)]
        if [C[k] for k in tail] != TAIL or [C[k] for k in idx] != IDX:
            skipped += 1
            continue
        a_ld = ks[-len(TAIL) - len(IDX) - 1]
        hl_ld = ks[-len(TAIL) - len(IDX) - 2]
        if ALOAD.match(C[a_ld]):
            base_line = C[hl_ld]
        else:
            base_line = C[a_ld]  # index already in A ("redundant load removed")
        mb = re.fullmatch(r"ld hl, \$([0-9A-F]{4})", base_line)
        if not mb:
            skipped += 1
            continue
        base = int(mb.group(1), 16)
        lab = M2.match(label or "")
        if 0x8000 <= base and (base & 0x3FFF) + 0xFF <= 0x3FFF and base < 0xC000:
            if lab and pc is not None and 0x8000 <= pc < 0xC000 and 0x8000 <= int(lab.group(2), 16) < 0xC000:
                k = int(lab.group(1), 16)
                rep = [f"    ld a, ${k + 1:02X} ; PRG bank {k} is the executing view\n",
                       "    call nes_prg_read_lo_check\n" if check else "    call nes_prg_read_lo_a\n"]
                n["lo_a"] += 1
            else:
                rep = ["    call nes_prg_read_lo_dyn\n"]
                n["lo_dyn"] += 1
        elif 0xC000 <= base and (base & 0x3FFF) + 0xFF <= 0x3FFF:
            if mapper == 2:
                rep = [f"    ld a, ${last + 1:02X} ; UxROM fixed bank {last}\n", "    call nes_prg_read_hi_a\n"]
                n["hi_a"] += 1
            else:
                rep = ["    call nes_prg_read_hi_dyn\n"]
                n["hi_dyn"] += 1
        else:
            skipped += 1
            continue
        # Replace TAIL (keeping its trailing anon label) and the call; keep the
        # anon label count: TAIL[-1] ":" before call and ":" after call stay.
        for k in tail[:-1]:
            edits[k] = []
        edits[tail[-1]] = rep + [":\n"]
        edits[i] = []
    res = []
    for k, l in enumerate(L):
        if k in edits:
            res.extend(edits[k])
        else:
            res.append(l)
    open(path, "w").write("".join(res))
    print("banked-static-prg-reads: lo view-bank %(lo_a)d, lo dynamic %(lo_dyn)d, hi fixed %(hi_a)d, hi dynamic %(hi_dyn)d" % n,
          f"({skipped} nes_cpu_read_hi site(s) kept)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
