#!/usr/bin/env python3
"""Fold the superblock SEC+SBC form into a native SUB (late pass).

The translator emits a known-carry 6502 SBC as one's-complement ADC so the
host carry equals the 6502 carry:

    [ld e, $NN / ld a, e | ld a, SRC]  cpl / ld e, a / scf / ld a, R / adc e

SUB computes the identical 8-bit result and Z, with the host carry inverted
(borrow = !C). The rewrite is applied only when E is provably overwritten
before any read on the straight-line path, and the carry is either dead or
consumed by one of the two capture idioms, which are rewritten to their
inverted-carry form (`sbc a / inc a` yields 6502 C as 1/0).
"""
import re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gbregs import code, effect, is_label, if_depth  # noqa: E402

REG = {"b", "c", "d", "h", "l"}


PLAIN = set()  # canonical block heads directly under SECTION: no live registers/flags
JP = re.compile(r"jp (?:(?:n?z|n?c), )?(nes_[0-9A-F]{4})$")


def e_dead(codes, j, reg="e"):
    stacked = 0
    optional = False  # inside a region a forward `jr cc, :+` may skip
    k = j
    end = min(j + 80, len(codes))
    while k < end:
        c = codes[k]
        k += 1
        if not c:
            continue
        if c == ":":
            optional = False
            continue
        if is_label(c):
            if optional:
                return False
            continue
        if c.startswith("IF DEF(NES2GBC_PROFILE_TRACE)"):
            while k < end and codes[k] != "ENDC":
                k += 1
            k += 1
            continue
        if c == "PROFILE_INC nes_profile_rts_pop":
            return True  # RTS pop: every continuation is a dispatched block head
        if c.startswith("PROFILE_INC "):
            continue
        if re.fullmatch(r"jr n?[zc], :\+", c):
            if optional or (reg == "cf" and "c," in c):
                return False
            optional = True
            continue
        if c.startswith(("SECTION", "IF", "ENDC", "ELSE")):
            return False
        m = JP.fullmatch(c)
        if m and m.group(1) in PLAIN and (reg != "cf" or not re.match(r"jp n?c,", c)):
            if c.startswith("jp nes_") and not optional:
                return True
            continue  # conditional exit to a plain head: keep scanning fallthrough
        if c == "jp nes_jump_known_hl_a_8bit" and not optional:
            return True
        if reg == "cf" and c == "push af":
            stacked += 1  # the pushed carry comes back unchanged at pop af
            continue
        if reg == "cf" and c == "pop af" and stacked:
            stacked -= 1
            continue
        eff = effect(c)
        if eff is None:
            return False
        r, w = eff
        if reg in r:
            return False
        if reg in w and not optional:
            return True
    return False


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    codes = [code(l) for l in lines]
    depth = if_depth(codes)
    for i, c in enumerate(codes):
        if c.endswith(":") and re.fullmatch(r"nes_[0-9A-F]{4}:", c):
            j = i - 1
            while j >= 0 and not codes[j]:
                j -= 1
            if j >= 0 and codes[j].startswith("SECTION"):
                PLAIN.add(c[:-1])
    idx = [i for i, c in enumerate(codes) if c]
    pos = {i: n for n, i in enumerate(idx)}
    n_done = 0
    edits = []
    for n, i in enumerate(idx):
        if codes[i] != "scf" or depth[i]:
            continue
        if n < 3 or n + 4 >= len(idx):
            continue
        c_ = [codes[idx[n + d]] for d in range(-3, 5)]
        # c_[0..2] = before scf, c_[3] = scf
        if c_[1] != "cpl" or c_[2] != "ld e, a":
            continue
        m = re.fullmatch(r"ld a, ([bcdhl])", c_[4])
        if not m or c_[5] != "adc e" or c_[6] != "ld l, a":
            continue
        lhs = m.group(1)
        src = c_[0]
        start = idx[n - 3]
        prev = codes[idx[n - 4]] if n >= 4 else ""
        mi = re.fullmatch(r"ld e, (\$[0-9A-F]{2})", prev)
        if src == "ld a, e" and mi:
            start = idx[n - 4]
            new = [f"ld a, {lhs}", f"sub {mi.group(1)}"]
        elif src == "ld a, [hl]" and lhs not in ("h", "l"):
            new = [f"ld a, {lhs}", "sub [hl]"]
        elif re.fullmatch(r"ld a, \[\$[0-9A-F]{4}\]", src) or re.fullmatch(r"ldh a, \[[\w$]+\]", src):
            new = [src, "ld e, a", f"ld a, {lhs}", "sub e"]
        elif re.fullmatch(r"ld a, ([bcdhl])", src) and src[-1] != "e":
            new = [f"ld a, {lhs}", f"sub {src[-1]}"]
        else:
            continue
        # carry consumer after "ld l, a"
        k = n + 4
        cons = codes[idx[k]] if k < len(idx) else ""
        cdead = any("C dead on every path" in lines[x] for x in range(idx[n + 2], idx[k]))
        repl_cons = None
        if cons == "sbc a" and "capture 6502 C" in lines[idx[k]]:
            repl_cons = (idx[k], idx[k], ["sbc a", "inc a"])
        elif cons == "ld a, $00" and k + 1 < len(idx) and codes[idx[k + 1]] == "rla":
            repl_cons = (idx[k], idx[k + 1], ["sbc a", "inc a"])
        elif not cdead:
            continue
        if repl_cons and not e_dead(codes, repl_cons[1] + 1, "cf"):
            continue  # host carry read again after the capture
        if not e_dead(codes, idx[n + 3] if not repl_cons else repl_cons[1] + 1):
            # the folded form leaves E = SRC (mem/reg forms) or untouched
            continue
        edits.append((start, idx[n + 2], new, repl_cons))
        n_done += 1
    for start, end, new, rc in sorted(edits, reverse=True):
        if rc:
            a, b, txt = rc
            lines[a:b + 1] = [f"    {t} ; inverted-borrow carry capture (sec_sbc_to_sub)\n" for t in txt]
        lines[start:end + 1] = [f"    {t} ; SEC+SBC folded to SUB (sec_sbc_to_sub)\n" for t in new]
    p.write_text("".join(lines))
    print(f"sec-sbc-to-sub: {n_done} site(s) folded")


if __name__ == "__main__":
    main(sys.argv[1])
