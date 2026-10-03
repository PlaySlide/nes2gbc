#!/usr/bin/env python3
"""Keep A in a dead scratch register instead of push af / pop af (late pass).

Indexed stores spill A around their address computation:

    push af                  ld e, a
    ld a, b                  ld a, b
    add $8F          ->      add $8F
    ld l, a                  ld l, a
    ld h, $C0                ld h, $C0
    pop af                   ld a, e
    ld [hl], a               ld [hl], a

(4+3 M -> 1+1 M). The scratch register T (E, then D) must be untouched by
the body and dead after the pop, and F must be dead after the pop too (pop af
restored the pre-push flags; ld a, T leaves the body's flags). Liveness is a
forward straight-line scan (tools/gbregs.py): labels are transparent, an
unconditional jump to a translated block head (SECTION-entry label, entered by
the dispatcher with no live registers) kills everything, any other control
transfer / directive / unknown line counts as a read of everything. The body
may only branch to anonymous labels inside itself, and those labels must not
be referenced from outside it. Code inside IF/ENDC is left alone.
"""
import bisect, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gbregs import F, code, effect, is_label  # noqa: E402

ANON_REF = re.compile(r"(?<![\w.]):(\++|-+)(?!\w)")


def main(path):
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    codes = [code(l) for l in lines]
    heads = set()
    for i in range(1, len(lines)):
        m = re.match(r"^(nes_[0-9A-F]{4}):$", codes[i])
        if m and lines[i - 1].startswith("SECTION"):
            heads.add(m.group(1))
    depth = [0] * len(lines)
    d = 0
    for i, c in enumerate(codes):
        if re.match(r"^IF\b", c):
            d += 1
        depth[i] = d
        if c.startswith("ENDC"):
            d -= 1
    anon = [i for i, c in enumerate(codes) if c == ":"]
    refs = {}  # anon line -> set of referencing lines
    for i, c in enumerate(codes):
        if c == ":" or ":" not in c:
            continue
        for m in ANON_REF.finditer(c):
            s = m.group(1); k = bisect.bisect_right(anon, i)
            t = k + len(s) - 1 if s[0] == "+" else k - len(s)
            if 0 <= t < len(anon):
                refs.setdefault(anon[t], set()).add(i)

    def dead_after(j, regs):
        need = set(regs)
        k = j + 1
        while k < len(lines) and need:
            c = codes[k]
            if not c or is_label(c) or c.startswith("PROFILE_INC"):
                k += 1
                continue
            if c == "IF DEF(NES2GBC_PROFILE_TRACE)":
                # trace-only PC log: writes D/E/H/L, preserves AF/BC, reads
                # nothing live, so release-build deadness implies trace-build
                while k < len(lines) and codes[k] != "ENDC":
                    k += 1
                k += 1
                continue
            m = re.match(r"^jp (nes_[0-9A-F]{4})$", c)
            if m and m.group(1) in heads:
                return True
            e = effect(c)
            if e is None:
                return False
            r, w = e
            if r & need:
                return False
            need -= w
            k += 1
        return not need

    n = 0
    for i, c in enumerate(codes):
        if c != "push af" or depth[i]:
            continue
        j = i + 1; used = set(); ok = True; body = []
        while j < len(lines) and j - i < 40:
            cj = codes[j]
            if cj == "pop af":
                break
            if not cj:
                j += 1; continue
            if cj == ":":
                if not refs.get(j) or not all(i < r < j for r in refs[j]):
                    ok = False; break
                j += 1; continue
            if is_label(cj) or depth[j]:
                ok = False; break
            mj = re.match(r"^jr (n?[cz]), (:\+)$", cj)
            if mj:
                body.append(j); used |= F; j += 1; continue
            e = effect(cj)
            if e is None or cj.startswith(("push", "pop")):
                ok = False; break
            used |= e[0] | e[1]; body.append(j); j += 1
        else:
            ok = False
        if not ok or j >= len(lines) or codes[j] != "pop af":
            continue
        # every in-body anon jump must land inside the body
        for b in body:
            if codes[b].startswith("jr "):
                k = bisect.bisect_right(anon, b)
                if k >= len(anon) or anon[k] >= j:
                    ok = False
        if not ok:
            continue
        for t in ("e", "d"):
            if t in used:
                continue
            if dead_after(j, {t} | F):
                ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                lines[i] = f"{ind}ld {t}, a ; A parked in dead {t.upper()} (was push af)\n"
                ind = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
                lines[j] = f"{ind}ld a, {t} ; (was pop af)\n"
                codes[i] = f"ld {t}, a"; codes[j] = f"ld a, {t}"
                n += 1
                break
    p.write_text("".join(lines))
    print(f"push-af-temp: {n} push/pop af pairs replaced")


if __name__ == "__main__":
    main(sys.argv[1])
