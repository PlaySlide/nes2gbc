#!/usr/bin/env python3
"""Compare guarded RTS continuations against the raw stacked PC-1 (late pass).

The inline RTS pop leaves the raw stacked return address PC-1 in H (high)
and C (low), then builds HL = PC with `ld l, c / inc hl` before a guard
chain of `ld a, l / cp LO` and `ld a, h / cp HI` tests ending in
`jp nes_dispatch_hl`.  This pass drops the `ld l, c / inc hl` (12 T per
RTS) and compares the raw bytes instead; the +1 moves to the fallback:

    ld a, l / cp X      ->  ld a, c / cp X-1
    ld a, h / cp Y      ->  unchanged
    jp nes_dispatch_hl  ->  ld l, c / inc hl / jp nes_dispatch_hl

H equals the incremented high byte unless C = $FF.  So a chain is only
rewritten when (a) no L constant is $00 (X-1 = $FF), which makes every
L test fail when C = $FF, and (b) every exit into a continuation sits
behind a passed L test in its unit (since the last label).  With C = $FF
all L tests fail and control reaches the fixed-up fallback; otherwise H is
exact.  Continuations are translated block entries / bank trampolines that
load their own HL, so the different L and A on hit paths are dead.

Set NES2GBC_RTS_RAW_GUARD=0 to disable.
"""
import os, re, sys

POPTAIL = ("ld h, [hl]", "ld l, c", "inc hl")
CMP = re.compile(r"^cp \$([0-9A-Fa-f]{2})$")


def code(l):
    return l.split(";", 1)[0].strip()


def rewrite(lines, i):
    """lines[i] = 'ld l, c', lines[i+1] = 'inc hl'. Return new region lines or None."""
    out = []
    a_src = None          # register last loaded into A ('l' / 'h')
    l_tested = False      # a `cp` on L in this unit (since the last label) passed or is pending
    last_cmp = None       # register of the last compare
    j = i + 2
    while j < len(lines) and j < i + 400:
        raw = lines[j]; c = code(raw)
        if not c:
            out.append(raw); j += 1; continue
        if c == "ld a, l":
            a_src = 'l'; out.append(raw.replace("ld a, l", "ld a, c", 1)); j += 1; continue
        if c == "ld a, h":
            a_src = 'h'; out.append(raw); j += 1; continue
        m = CMP.match(c)
        if m:
            if a_src is None:
                return None
            v = int(m.group(1), 16)
            if a_src == 'l':
                if v == 0:
                    return None
                ind = raw[:len(raw) - len(raw.lstrip())]
                cmt = raw.split(";", 1)[1] if ";" in raw else None
                out.append("%scp $%02X ; raw PC-1 (rts_raw_guard_compare.py)%s\n" % (ind, (v - 1) & 0xFF, (" ;" + cmt.rstrip("\n")) if cmt else ""))
            else:
                out.append(raw)
            last_cmp = a_src; j += 1; continue
        if c in (":",) or re.match(r"^\.[\w]+:$", c):
            l_tested = False; out.append(raw); j += 1; continue
        if re.match(r"^jr nz, (:\+|\.\w+)$", c):
            if last_cmp == 'l':
                l_tested = True
            out.append(raw); j += 1; continue
        if re.match(r"^j[pr] z, nes_\w+$", c):
            if not (last_cmp == 'l' or l_tested):
                return None
            out.append(raw); j += 1; continue
        if re.match(r"^ld a, BANK\(nes_\w+\)$", c) or re.match(r"^ld hl, nes_\w+$", c):
            if not l_tested:
                return None
            out.append(raw); j += 1; continue
        if c == "jp nes_dispatch_hl":
            ind = raw[:len(raw) - len(raw.lstrip())]
            out.append(ind + "ld l, c ; RTS fallback: HL = raw PC-1 + 1\n")
            out.append(ind + "inc hl\n")
            out.append(raw)
            return out, j
        m2 = re.match(r"^jp (nes_\w+)$", c)
        if m2:
            if not l_tested:
                return None
            out.append(raw); j += 1; continue
        if c in ("IF DEF(NES2GBC_PROFILE_TRACE)", "ENDC"):
            return None
        return None
    return None


def main():
    if os.environ.get("NES2GBC_RTS_RAW_GUARD", "1") == "0":
        print("rts-raw-guard: disabled"); return 0
    p = sys.argv[1]
    lines = open(p).read().splitlines(keepends=True)
    out = []; i = 0; n = 0; skipped = 0
    while i < len(lines):
        if code(lines[i]) == "ld l, c" and i + 1 < len(lines) and code(lines[i + 1]) == "inc hl":
            # require the RTS pop just before (allowing comment lines)
            k = i - 1
            while k >= 0 and not code(lines[k]):
                k -= 1
            if k >= 0 and code(lines[k]) == "ld h, [hl]":
                r = rewrite(lines, i)
                if r:
                    region, end = r
                    ind = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
                    out.append(ind + "; RTS guards compare raw PC-1 (rts_raw_guard_compare.py)\n")
                    out.extend(region); i = end + 1; n += 1; continue
                skipped += 1
        out.append(lines[i]); i += 1
    open(p, "w").write("".join(out))
    print("rts-raw-guard: rewrote %d RTS guard chains (%d left)" % (n, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
