#!/usr/bin/env python3
"""Drop an A load that is immediately overwritten by another A load (late pass).

    ld a, $00        ; e.g. left behind when a dead CLC/SEC shadow store is elided
    ld a, d       -> ld a, d

LD never touches flags and GB memory reads have no side effects, so the first
load is dead when the very next instruction (no label in between) loads A
again from a source other than A itself. [hli]/[hld] first loads are kept
(they update HL).
"""
import re, sys
from pathlib import Path

A_LOAD = re.compile(r"^(ld a, (?!\[hl[id]\])(?!a$)\S.*|ldh a, \S.*)$")


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    n = 0
    i = 0
    while i < len(lines):
        if A_LOAD.match(code(lines[i])):
            j = i + 1
            while j < len(lines) and not code(lines[j]):
                j += 1
            if j < len(lines) and A_LOAD.match(code(lines[j])) and "[hl" not in code(lines[j]).replace("[hl]", "") :
                ind = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
                lines[i] = f"{ind}; dead A load removed (overwritten by next load)\n"
                n += 1
        i += 1
    p.write_text("".join(lines))
    print(f"dead-a-reload: {n} dead A loads removed")


if __name__ == "__main__":
    main(sys.argv[1])
