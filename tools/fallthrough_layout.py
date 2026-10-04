#!/usr/bin/env python3
"""Lay out translated blocks so section-final direct jumps become fall-throughs.

Every translated block lives in its own floating `SECTION ..., ROMX, BANK[n]`,
so a block ending in `jp nes_X` pays a taken jump (4 M-cycles) even when the
target block is in the same bank. When block A ends in an unconditional
`jp L` and L is the first label of another code section in the same bank
(no ALIGN/address constraint), section X's body is emitted directly after A
inside A's section and the jump is dropped. Each section gets at most one
predecessor; cycles are broken. Code is moved verbatim (labels, local scopes,
IF/ENDC blocks), so only the removed jump changes; RGBDS resolves every other
reference. Runs last (after bank packing and all peephole passes).
"""
from __future__ import annotations
import re, sys
from pathlib import Path

SEC = re.compile(r'^SECTION\s+"([^"]*)",\s*ROMX,\s*BANK\[(\d+)\]\s*$')
GLABEL = re.compile(r'^([A-Za-z_]\w*):{1,2}\s*(;.*)?$')
JP = re.compile(r'^\s*jp\s+([A-Za-z_]\w*)\s*(;.*)?$')


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    # split into chunks: [start, end) per SECTION header
    heads = [i for i, l in enumerate(lines) if l.startswith("SECTION")]
    if not heads:
        return
    chunks = []
    for k, s in enumerate(heads):
        e = heads[k + 1] if k + 1 < len(heads) else len(lines)
        chunks.append((s, e))
    prefix = lines[:heads[0]]
    info = []
    first_label = {}
    for idx, (s, e) in enumerate(chunks):
        m = SEC.match(lines[s].rstrip("\n"))
        bank = int(m.group(2)) if m else None
        # first meaningful line must be a global label
        fl = None
        for j in range(s + 1, e):
            c = code(lines[j])
            if not c:
                continue
            g = GLABEL.match(c)
            if g:
                fl = g.group(1)
            break
        # last code line
        last = None
        for j in range(e - 1, s, -1):
            if code(lines[j]):
                last = j
                break
        info.append({"bank": bank, "first": fl, "last": last})
        if bank is not None and fl and fl.startswith("nes_"):
            first_label[fl] = idx
    succ, pred = {}, {}
    for idx, it in enumerate(info):
        if it["bank"] is None or it["last"] is None:
            continue
        m = JP.match(lines[it["last"]])
        if not m:
            continue
        t = first_label.get(m.group(1))
        if t is None or t == idx or t in pred or info[t]["bank"] != it["bank"]:
            continue
        succ[idx] = t
        pred[t] = idx
    # break cycles
    seen = set()
    for idx in list(succ):
        if idx in seen:
            continue
        path, cur = [], idx
        while cur is not None and cur not in seen:
            seen.add(cur)
            path.append(cur)
            cur = succ.get(cur)
        if cur is not None and cur in path:
            # cycle: drop the edge into `cur`
            a = pred.pop(cur)
            del succ[a]
    out = list(prefix)
    emitted = set()
    n = 0
    for idx in range(len(chunks)):
        if idx in emitted or idx in pred:
            continue
        cur = idx
        while cur is not None:
            emitted.add(cur)
            s, e = chunks[cur]
            body = lines[s:e] if cur == idx else lines[s + 1:e]
            nxt = succ.get(cur)
            if nxt is not None:
                drop = info[cur]["last"] - s - (0 if cur == idx else 1)
                body = body[:drop] + ["    ; fall through (tools/fallthrough_layout.py)\n"] + body[drop + 1:]
                n += 1
            out.extend(body)
            cur = nxt
    assert len(emitted) == len(chunks)
    p.write_text("".join(out))
    print(f"fallthrough-layout: {n} section-final jumps became fall-throughs")


if __name__ == "__main__":
    main(sys.argv[1])
