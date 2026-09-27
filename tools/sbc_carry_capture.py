#!/usr/bin/env python3
"""Capture the 6502 carry with SBC A, keeping host C live (late pass).

nes_c_shadow readers only test bit 0 (RRA / AND $01) or nonzero (AND A), so a
captured value of $FF is as good as $01:

    ld a, $00 / rl a (or rla) / ldh [nes_c_shadow], a     (4 or 3 M, C := 0)
 -> sbc a / ldh [nes_c_shadow], a                        (1 M, C preserved)

Z matches for RL A (Z = !C either way); RLA left Z=0, so a RLA site is only
rewritten when Z is provably rewritten before any read. Host C now stays the
6502 carry instead of 0, so the site needs C written before any read on the
fall-through path (a jump to a translated block entry counts as dead), except
for the immediate re-seed

    ldh a, [nes_c_shadow] / rra / ld a, <r>

which is deleted instead: host C already equals the stored carry, and the
instruction after `ld a, <r>` must rewrite Z/N/H (ADC/SBC/RL/RR family).
"""
import re, sys
from pathlib import Path

C_WRITERS = re.compile(r"^(and|or|xor|cp|sub|add a|adc|sbc|rla|rra|rlca|rrca|rlc|rrc|rl |rr |sla|sra|srl|swap|scf|pop af)")
C_READERS = re.compile(r"^(adc|sbc|rla|rra|rl |rr |ccf|jr n?c,|jp n?c,|ret n?c|call n?c,|daa)")
Z_WRITERS = re.compile(r"^(and|or|xor|cp|sub|sbc|add|adc|inc [abcdehl]$|dec [abcdehl]$|inc \[hl\]|dec \[hl\]|bit|swap|rlc |rrc |rl |rr |sla|sra|srl|pop af)")
Z_READERS = re.compile(r"^(jr n?z,|jp n?z,|ret n?z|call n?z,)")
NOFLAG = re.compile(r"^(ld (?!hl, sp)|ldh |push (bc|de|hl)|pop (bc|de|hl)|inc (bc|de|hl|sp)|dec (bc|de|hl|sp)|nop|PROFILE_INC)")
FULL_WRITE_C_READ = re.compile(r"^(adc|sbc|rl |rr )")
BLOCK_JP = re.compile(r"^jp nes_[0-9A-F]{4}(_trace)?$")
BLOCK_LABEL = re.compile(r"^nes_[0-9A-F]{4}(_trace)?:$")
COND_BLOCK_JP = re.compile(r"^jp n?[zc], nes_[0-9A-F]{4}(_trace)?$")


def code(l):
    return l.split(";", 1)[0].strip()


def dead_after(lines, i, need_c, need_z):
    """True if (C if need_c) and (Z if need_z) are rewritten before any read."""
    j = i - 1
    end = min(i + 48, len(lines))
    while j + 1 < end:
        j += 1
        s = code(lines[j])
        if not s or s == ":":
            continue  # anonymous merge: the other path never set C/Z for us
        if BLOCK_LABEL.match(s):
            return True  # falls into the next translated block entry
        if s.startswith(("IF", "ENDC", "ELSE")) or s.endswith(":"):
            return False
        if s == "push af":
            # PUSH AF ... POP AF (index math) restores these flags.
            k = j + 1
            while k < end:
                t = code(lines[k])
                if t == "pop af":
                    break
                if t == "push af" or t.startswith(("IF", "ENDC", "ELSE", "jp", "call", "ret", "rst")) \
                        or (t.endswith(":") and t != ":"):
                    return False
                k += 1
            if k >= end:
                return False
            j = k
            continue
        rc = need_c and C_READERS.match(s)
        rz = need_z and Z_READERS.match(s)
        if rc or rz:
            return False
        if need_c and C_WRITERS.match(s):
            need_c = False
        if need_z and Z_WRITERS.match(s):
            need_z = False
        if not need_c and not need_z:
            return True
        if BLOCK_JP.match(s):
            return True
        if NOFLAG.match(s) or Z_WRITERS.match(s) or C_WRITERS.match(s) or COND_BLOCK_JP.match(s):
            continue
        return False
    return False


def next_code(lines, k):
    while k < len(lines) and not code(lines[k]):
        k += 1
    return k


def branch_fuse(lines, j):
    """`ldh a,[nes_c_shadow] / and a / jr|jp z|nz, T` -> `jr|jp nc|c, T`.

    A and flags after the test must be dead on both paths: T is a translated
    block entry (or an anonymous label directly followed by one), and the
    fall-through overwrites A without reading flags before a jump.
    """
    if code(lines[j]) != "ldh a, [nes_c_shadow]" or code(lines[j + 1]) != "and a":
        return None
    m = re.match(r"^(jr|jp) (n?z), (\S+)$", code(lines[j + 2]))
    if not m:
        return None
    op, cond, tgt = m.groups()
    if tgt == ":+":
        k = j + 3
        while k < len(lines) and code(lines[k]) != ":":
            k += 1
        k2 = next_code(lines, k + 1)
        if not BLOCK_LABEL.match(code(lines[k2])):
            return None
    elif not re.match(r"^nes_[0-9A-F]{4}(_trace)?$", tgt):
        return None
    # Fall-through: A overwritten, no flag reads, then a jump or block label.
    k = next_code(lines, j + 3)
    first = True
    for _ in range(6):
        t = code(lines[k])
        if BLOCK_LABEL.match(t) or BLOCK_JP.match(t) or t.startswith("jp nes_jump_known_hl_a"):
            break
        if first:
            if not (t == "xor a" or re.match(r"^ld a, [^\[]", t)):
                return None
            first = False
        elif not (re.match(r"^ld (a|hl), ", t)):
            return None
        k = next_code(lines, k + 1)
    else:
        return None
    ind = lines[j + 2][:len(lines[j + 2]) - len(lines[j + 2].lstrip())]
    return f"{ind}{op} {'nc' if cond == 'z' else 'c'}, {tgt} ; fused 6502 carry branch\n"


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    n = reseed = skipped = fused = 0
    i = 0
    while i < len(lines) - 3:
        if code(lines[i]) != "ld a, $00" or code(lines[i + 1]) not in ("rl a", "rla") \
                or code(lines[i + 2]) != "ldh [nes_c_shadow], a":
            i += 1
            continue
        is_rla = code(lines[i + 1]) == "rla"
        # Look for an immediate re-seed after flag-neutral instructions.
        j = i + 3
        while j < len(lines) and (not code(lines[j]) or NOFLAG.match(code(lines[j]))) \
                and code(lines[j]) != "ldh a, [nes_c_shadow]":
            j += 1
        k = j
        ok = False
        drop = []
        br = branch_fuse(lines, j)
        if br is not None:
            ok = True
            drop = []
            lines[j] = f"{lines[j][:len(lines[j]) - len(lines[j].lstrip())]}; carry test fused (host C live after SBC A capture)\n"
            lines[j + 1] = "\n"
            lines[j + 2] = br
            fused += 1
        elif code(lines[j]) == "ldh a, [nes_c_shadow]" and code(lines[j + 1]) == "rra" \
                and re.match(r"^ld a, [a-z]$", code(lines[j + 2])) \
                and FULL_WRITE_C_READ.match(code(lines[j + 3])):
            ok = True
            drop = [j, j + 1]
        else:
            ok = dead_after(lines, i + 3, True, is_rla)
        if is_rla and ok and drop:
            ok = True  # Z rewritten by the ADC/SBC/RL/RR right after the re-seed
        if not ok:
            skipped += 1
            i += 1
            continue
        ind = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
        for d in sorted(drop, reverse=True):
            lines[d] = f"{ind}; carry re-seed removed (host C live after SBC A capture)\n"
        lines[i:i + 2] = [f"{ind}sbc a ; capture 6502 C as 0/$FF, host C preserved\n"]
        n += 1
        reseed += bool(drop)
        i += 1
    p.write_text("".join(lines))
    print(f"sbc-carry-capture: {n} sites ({reseed} re-seeds removed, {fused} branches fused), {skipped} kept")


if __name__ == "__main__":
    main(sys.argv[1])
