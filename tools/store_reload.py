#!/usr/bin/env python3
"""Drop a reload of a value just stored from A (late pass).

    ldh [nes_z_shadow], a
:                         ; anonymous label nothing jumps to (left by earlier passes)
    ldh a, [nes_z_shadow] -> (removed: A already holds the value)

Only 6502-state HRAM shadows and NES internal RAM ($C000-$C7FF) qualify (no
ISR writes them, reads have no side effects). Loads don't touch flags, so the
removal is exact. Only blank/comment lines and anonymous labels that no
:+/:- reference resolves to may sit between the two instructions.
"""
import re, sys
from pathlib import Path

STATE = {"nes_a", "nes_x", "nes_y", "nes_z_shadow", "nes_n_shadow", "nes_c_shadow", "nes_sp", "nes_p"}
REF = re.compile(r"(?<![\w.]):(\++|-+)(?![\w])")


def code(l):
    return l.split(";", 1)[0].strip()


def ok_addr(x):
    if x in STATE:
        return True
    m = re.fullmatch(r"\$([0-9A-Fa-f]{4})", x)
    return bool(m) and 0xC000 <= int(m.group(1), 16) <= 0xC7FF


def referenced_anon(lines):
    anon = [i for i, l in enumerate(lines) if code(l) == ":"]
    refd = set()
    import bisect
    for i, l in enumerate(lines):
        c = code(l)
        if c == ":" or ":" not in c:
            continue
        for m in REF.finditer(c):
            s = m.group(1); n = len(s)
            k = bisect.bisect_right(anon, i)
            if s[0] == "+":
                t = k + n - 1
            else:
                t = k - n
            if 0 <= t < len(anon):
                refd.add(anon[t])
            else:
                return None
    return refd


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    refd = referenced_anon(lines)
    if refd is None:
        print("store-reload: unresolved anonymous ref, skipped"); return
    n = 0
    for i, l in enumerate(lines):
        m = re.fullmatch(r"(ldh?) \[([^\]]+)\], a", code(l))
        if not m or not ok_addr(m.group(2)):
            continue
        j = i + 1
        while j < len(lines):
            c = code(lines[j])
            if not c or (c == ":" and j not in refd):
                j += 1
                continue
            break
        if j < len(lines):
            m2 = re.fullmatch(r"(ldh?) a, \[([^\]]+)\]", code(lines[j]))
            if m2 and m2.group(2) == m.group(2):
                ind = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
                lines[j] = f"{ind}; reload of just-stored {m.group(2)} removed\n"
                n += 1
    p.write_text("".join(lines))
    print(f"store-reload: {n} reloads removed")


if __name__ == "__main__":
    main(sys.argv[1])
