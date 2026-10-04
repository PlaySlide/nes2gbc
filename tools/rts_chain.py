#!/usr/bin/env python3
"""Parse / re-emit guarded RTS return compare chains in generated.asm.

A chain follows the inline RTS pop (HL = raw stacked PC-1) and is a run of
candidate units ending in the unmatched fallback `inc hl / jp nes_dispatch_hl`:

    ld a, h / cp HI / jr nz, G
      (ld a, l / cp LO / jr nz, C / <jump T> / C:)+     ; C == G for leaf units
    G:

<jump T> is `jp nes_T` or `ld a, BANK(nes_T) / ld hl, nes_T / jp
nes_jump_known_hl_a_8bit`. Each chain is keyed by its enclosing translated
block label plus an ordinal (stable across reorderings), so a return-edge
profile (tools/bench/rts_edge_profile.py) can name its candidates.
"""
from __future__ import annotations
import re

BLOCK = re.compile(r"^(nes_[0-9A-F]{4}(?:_trace)?):")
LBL = re.compile(r"^([A-Za-z_][\w]*):\s*(;.*)?$")
CP = re.compile(r"cp \$([0-9A-F]{2})$")
JRNZ = re.compile(r"jr nz, ([\w.]+)$")
JP = re.compile(r"jp nes_([0-9A-F]{4})$")
BANKLD = re.compile(r"ld a, BANK\(nes_([0-9A-F]{4})\)$")


def code(l):
    return l.split(";", 1)[0].strip()


class Chain:
    def __init__(self, key, start, end, cands, labels, fallback_label):
        self.key = key          # "nes_E42B#0"
        self.start = start      # first line index (ld a, h)
        self.end = end          # line index of `inc hl` (exclusive end of units)
        self.cands = cands      # list of dict(hi, lo, target, jump=[lines], next_label)
        self.labels = labels    # labels defined inside [start, end)
        self.fallback_label = fallback_label  # label right before `inc hl`


def _next_code(lines, codes, i):
    while i < len(lines) and not codes[i]:
        i += 1
    return i


def parse(lines):
    codes = [code(l) for l in lines]
    chains = []
    block = None
    ordinal = {}
    i = 0
    n = len(lines)
    while i < n:
        m = BLOCK.match(lines[i])
        if m:
            block = m.group(1)
        if codes[i] != "ld a, h" or block is None:
            i += 1
            continue
        j = _next_code(lines, codes, i + 1)
        if j >= n or not CP.fullmatch(codes[j]):
            i += 1
            continue
        k = _next_code(lines, codes, j + 1)
        mj = JRNZ.fullmatch(codes[k]) if k < n else None
        if not mj or not mj.group(1).startswith("nes_rts_"):
            i += 1
            continue
        res = _parse_chain(lines, codes, i)
        if res is None:
            i += 1
            continue
        end, cands, labels, fb = res
        o = ordinal.get(block, 0)
        ordinal[block] = o + 1
        chains.append(Chain(f"{block}#{o}", i, end, cands, labels, fb))
        i = end + 1
    return chains


def _parse_chain(lines, codes, i):
    n = len(lines)
    nc = lambda q: _next_code(lines, codes, q)
    cands, labels, last = [], [], None
    p = i
    while True:
        p = nc(p)
        if p >= n:
            return None
        c = codes[p]
        if c.endswith(":"):
            labels.append(c[:-1]); last = c[:-1]; p += 1
            continue
        if c == "inc hl":
            q = nc(p + 1)
            if q < n and codes[q] == "jp nes_dispatch_hl" and cands and last:
                return p, cands, labels, last
            return None
        if c != "ld a, h":
            return None
        p = nc(p + 1)
        m = CP.fullmatch(codes[p]) if p < n else None
        if not m:
            return None
        hi = int(m.group(1), 16)
        p = nc(p + 1)
        m = JRNZ.fullmatch(codes[p]) if p < n else None
        if not m:
            return None
        g = m.group(1)
        p += 1
        while True:
            p = nc(p)
            if p >= n:
                return None
            if codes[p] == g + ":":
                break
            if codes[p] != "ld a, l":
                return None
            p = nc(p + 1)
            m = CP.fullmatch(codes[p]) if p < n else None
            if not m:
                return None
            lo = int(m.group(1), 16)
            p = nc(p + 1)
            m = JRNZ.fullmatch(codes[p]) if p < n else None
            if not m:
                return None
            cl = m.group(1)
            p = nc(p + 1)
            mjp = JP.fullmatch(codes[p]) if p < n else None
            if mjp:
                jump = [lines[p]]; tgt = int(mjp.group(1), 16); p += 1
            else:
                mb = BANKLD.fullmatch(codes[p]) if p < n else None
                if not mb:
                    return None
                t = mb.group(1)
                q1 = nc(p + 1)
                q2 = nc(q1 + 1)
                if codes[q1] != f"ld hl, nes_{t}" or codes[q2] != "jp nes_jump_known_hl_a_8bit":
                    return None
                jump = [lines[p], lines[q1], lines[q2]]; tgt = int(t, 16); p = q2 + 1
            p = nc(p)
            if p >= n or codes[p] != cl + ":":
                return None
            cands.append(dict(hi=hi, lo=lo, target=tgt, jump=jump, next_label=cl))
            if cl == g:
                break
            labels.append(cl); last = cl; p += 1


def emit(chain, order_groups, tag):
    """order_groups: list of (hi, [cand,...]) in emission order."""
    out = [f"    ; RTS return chain ordered by return-edge profile (tools/rts_chain_reorder.py, {chain.key})\n"]
    for gi, (hi, cs) in enumerate(order_groups):
        g = f"nes_rco_{tag}_g{gi}"
        out += ["    ld a, h\n", f"    cp ${hi:02X}\n", f"    jr nz, {g}\n", "    ld a, l\n"]
        for ci, c in enumerate(cs):
            nl = f"nes_rco_{tag}_g{gi}_{ci}"
            out += [f"    cp ${c['lo']:02X} ; raw stacked RTS PC-1 for nes_{c['target']:04X}\n",
                    f"    jr nz, {nl}\n"] + c["jump"] + [f"{nl}:\n"]
        out += [f"{g}:\n"]
    return out
