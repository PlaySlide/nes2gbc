#!/usr/bin/env python3
"""Thread direct jumps through canonical block adapters.

A translated block that is also the middle of a superblock keeps a canonical
entry `nes_X:` whose body is at most a few register-cache loads followed by
`jp nes_X_trace` (or another label). Every `jp nes_X` then pays a second
taken jump. For an unconditional `jp nes_X` in the same bank as the
adapter's target, emit the adapter's loads at the site and jump to the target
directly; for an 8-bit bank-switch transfer (`ld a, BANK(nes_X)` / `ld hl,
nes_X` / `jp nes_jump_known_hl_a_8bit`) to a load-free adapter, retarget both
operands. The executed instruction sequence is the same minus one `jp`. The
adapter stays in place for every other reference (dispatch tables, RTS
compare chains in other banks, fallthrough). Labels the benchmarks hook
(NMI handler, idle loops) are left alone. Runs last.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

SEC = re.compile(r'^SECTION\s+"[^"]*",\s*(ROMX|ROM0)(?:,\s*BANK\[(\d+)\])?')
GLABEL = re.compile(r'^(nes_[0-9A-F]{4}):\s*(;.*)?$')
ANYLABEL = re.compile(r'^[A-Za-z_.][\w.]*:{1,2}\s*(;.*)?$')
JP = re.compile(r'^(\s*)jp (nes_[0-9A-F]{4})\s*(;.*)?$')
SIMPLE = re.compile(r'^(ld [abcde], [abcde]|ldh a, \[nes_[a-z_]+\]|ld [bcde], a)$')
TARGET = re.compile(r'^jp ([A-Za-z_][\w.]*)$')
HOOKED = {"nes_8082", "nes_8057", "nes_C85F", "nes_C7E1"}


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    n = len(lines)
    codes = [code(l) for l in lines]
    bank = [None] * n
    b = None
    for i, l in enumerate(lines):
        m = SEC.match(l)
        if m:
            b = 0 if m.group(1) == "ROM0" else int(m.group(2) or -1)
        bank[i] = b
    label_line = {}
    for i, c in enumerate(codes):
        m = re.match(r'^([A-Za-z_][\w]*(?:\.[\w]+)?):{1,2}$', c)
        if m:
            label_line.setdefault(m.group(1), i)
    adapters = {}
    for i, l in enumerate(lines):
        m = GLABEL.match(l)
        if not m or m.group(1) in HOOKED:
            continue
        pre = []
        j = i + 1
        skip = False
        while j < n:
            c = codes[j]
            if c.startswith("IF DEF(NES2GBC_PROFILE_TRACE)"):
                while j < n and codes[j] != "ENDC":
                    j += 1
                j += 1
                continue
            if not c:
                j += 1
                continue
            if ANYLABEL.match(c) or c.startswith("SECTION") or c.startswith("IF") or c.startswith("PROFILE"):
                skip = True
                break
            t = TARGET.match(c)
            if t:
                break
            if SIMPLE.match(c) and len(pre) < 3:
                pre.append(c)
                j += 1
                continue
            skip = True
            break
        if skip or j >= n:
            continue
        tgt = TARGET.match(codes[j]).group(1)
        if tgt not in label_line or tgt.startswith("nes_jump_known") or tgt == m.group(1):
            continue
        adapters[m.group(1)] = (pre, tgt, bank[label_line[tgt]], i, j)
    # do not thread into another adapter chain more than once (target itself an adapter is fine: one hop saved)
    out = []
    direct = bankjp = 0
    i = 0
    while i < n:
        l = lines[i]
        m = JP.match(l)
        if m and m.group(2) in adapters:
            pre, tgt, tb, ai, aj = adapters[m.group(2)]
            if not (ai <= i <= aj) and (tb == 0 or tb == bank[i]) and bank[i] is not None:
                ind = m.group(1)
                for c in pre:
                    out.append(f"{ind}{c} ; adapter {m.group(2)} (tools/thread_adapter_jumps.py)\n")
                out.append(f"{ind}jp {tgt} ; threaded through adapter {m.group(2)}\n")
                direct += 1
                i += 1
                continue
        mb = re.match(r'^(\s*)ld a, BANK\((nes_[0-9A-F]{4})\)\s*(;.*)?$', l)
        if mb and mb.group(2) in adapters and i + 2 < n:
            pre, tgt, tb, ai, aj = adapters[mb.group(2)]
            x = mb.group(2)
            if (not pre and codes[i + 1] == f"ld hl, {x}" and codes[i + 2] == "jp nes_jump_known_hl_a_8bit"
                    and not tgt.startswith(".") and "." not in tgt):
                ind = mb.group(1)
                out.append(f"{ind}ld a, BANK({tgt}) ; threaded through adapter {x}\n")
                out.append(f"{ind}ld hl, {tgt}\n")
                out.append(lines[i + 2])
                bankjp += 1
                i += 3
                continue
        out.append(l)
        i += 1
    p.write_text("".join(out))
    print(f"thread-adapter-jumps: {len(adapters)} adapters; threaded {direct} direct jp, {bankjp} bank-switch transfers")


if __name__ == "__main__":
    main(sys.argv[1])
