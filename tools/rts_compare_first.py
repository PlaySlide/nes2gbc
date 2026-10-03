#!/usr/bin/env python3
"""Compare-first RTS return for sites with a dominant continuation (late pass).

The inline RTS pop materialises HL = stacked PC-1 (and C = its low byte) and
then walks a guarded hi/lo compare chain. When one continuation T0 takes
almost every return (profile: profiles/<rom>.rtsprof block entries, T0's
share >= --threshold), compare the stacked bytes straight from page $01 and
jump; only a miss rebuilds the original register state (HL = PC-1, C = low
byte, nes_sp already advanced) and falls into the untouched chain:

    ldh a,[nes_sp] / ld l,a / add 2 / ldh [nes_sp],a / ld h,$C1 / inc l
    ld a,[hl] / inc l / cp LO0 / jr nz,miss / ld a,[hl] / cp HI0 / jr nz,miss
    <jump T0>
  miss: ld a,[hl] / dec l / ld c,[hl] / ld l,c / ld h,a / <original chain>

INC L keeps the page-$01 wrap of the original. Hit path 29 M vs 31 M; a miss
costs ~9 M extra, hence the high threshold. T0 is a translated block entry
(no live registers/flags), so differing A/C/flags on the hit path are dead.
"""
import argparse, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gbregs import code, if_depth  # noqa: E402

POP = ["ldh a, [nes_sp]", "inc a", "ld l, a", "ld h, $C1", "ld c, [hl]", "inc l", "ld a, l",
       "ldh [nes_sp], a", "ld h, [hl]", "ld l, c", "ld a, h"]
CAND = re.compile(r"raw stacked RTS PC-1 for nes_([0-9A-F]{4})\b")


def load_prof(path):
    d = {}
    try:
        for l in open(path):
            if l.startswith("#") or not l.strip():
                continue
            v, a = l.split()[:2]
            d[a.upper()] = float(v)
    except OSError:
        pass
    return d


KEY = re.compile(r"\((?:tools/)?rts_chain_reorder\.py, (nes_[0-9A-F]{4}(?:_trace)?#\d+)\)")
JPT = re.compile(r"jp nes_([0-9A-F]{4})$")


def pair_site(lines, codes, pop0, k, edges, thr, pmin):
    """Two dominant continuations by per-site return-edge profile: returns
    ((lo, hi, jump lines, share) x2) or None. k = chain's first `ld a, h`."""
    key = None
    for x in range(pop0, k + 1):
        m = KEY.search(lines[x])
        if m:
            key = m.group(1)
    if key is None or key not in edges:
        return None
    # walk the chain: groups `ld a, h / cp HI / jr nz` then units
    # `[ld a, l] / cp LO ; ...for nes_T / jr nz / <jump>`
    units = {}
    hi = None
    x = k
    while x < len(lines) and codes[x] != "jp nes_dispatch_hl" and not lines[x].startswith("SECTION"):
        c = codes[x]
        if c == "ld a, h":
            y = x + 1
            while not codes[y]:
                y += 1
            mh = re.fullmatch(r"cp \$([0-9A-F]{2})", codes[y])
            if not mh:
                return None
            hi = int(mh.group(1), 16)
            x = y + 1
            continue
        mc = CAND.search(lines[x])
        if mc and hi is not None:
            ml = re.fullmatch(r"cp \$([0-9A-F]{2})", c)
            if not ml:
                return None
            t = mc.group(1)
            y = x + 1
            while not codes[y]:
                y += 1
            if not codes[y].startswith("jr nz,"):
                return None
            y += 1
            while not codes[y]:
                y += 1
            if JPT.fullmatch(codes[y]) and JPT.fullmatch(codes[y]).group(1) == t:
                jl = [lines[y]]
            elif codes[y] == f"ld a, BANK(nes_{t})" and codes[y + 1] == f"ld hl, nes_{t}" and codes[y + 2] == "jp nes_jump_known_hl_a_8bit":
                jl = lines[y:y + 3]
            else:
                return None
            units.setdefault(t, (int(ml.group(1), 16), hi, jl))
        x += 1
    e = edges[key]
    tot = sum(e.values())
    if tot <= 0:
        return None
    top = sorted(((v, t) for t, v in e.items() if t in units), reverse=True)[:2]
    if len(top) < 2:
        return None
    (v0, t0), (v1, t1) = top
    if (v0 + v1) / tot < thr or v1 / tot < pmin:
        return None
    lo0, hi0, j0 = units[t0]
    lo1, hi1, j1 = units[t1]
    if lo0 == lo1:
        return None
    return (lo0, hi0, j0, v0 / tot), (lo1, hi1, j1, v1 / tot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asm"); ap.add_argument("--rts-profile", default="")
    ap.add_argument("--threshold", type=float, default=0.9)
    ap.add_argument("--edge-profile", default="")
    ap.add_argument("--pair-threshold", type=float, default=0.9)
    ap.add_argument("--pair-min", type=float, default=0.15)
    a = ap.parse_args()
    edges = {}
    if a.edge_profile:
        try:
            for l in open(a.edge_profile):
                if l.startswith("#") or not l.strip():
                    continue
                v, key, t = l.split()[:3]
                edges.setdefault(key, {})[t.upper()] = float(v)
        except OSError:
            pass
    n2 = 0
    prof = load_prof(a.rts_profile) if a.rts_profile else {}
    if not prof:
        print("rts-compare-first: no profile, skipped"); return
    p = Path(a.asm)
    lines = p.read_text().splitlines(keepends=True)
    codes = [code(l) for l in lines]
    depth = if_depth(codes)
    out = []; i = 0; n = 0; seen = 0
    while i < len(lines):
        if codes[i] != POP[0] or depth[i]:
            out.append(lines[i]); i += 1; continue
        # match the pop + chain head on code lines only (comments/blanks allowed)
        idx = []; j = i
        for want in POP:
            while j < len(lines) and not codes[j] and not lines[j].lstrip().startswith(("SECTION",)):
                j += 1
            if j >= len(lines) or codes[j] != want:
                break
            idx.append(j); j += 1
        if len(idx) != len(POP):
            out.append(lines[i]); i += 1; continue
        k = idx[-1]  # "ld a, h": chain starts here
        head = [x for x in range(k, min(k + 12, len(lines))) if codes[x]]
        c = [codes[x] for x in head]
        m_hi = re.fullmatch(r"cp (\$[0-9A-F]{2})", c[1]) if len(c) > 6 else None
        m_lo = re.fullmatch(r"cp (\$[0-9A-F]{2})", c[4]) if len(c) > 6 else None
        if not (m_hi and m_lo and c[2].startswith("jr nz,") and c[3] == "ld a, l" and c[5].startswith("jr nz,")):
            out.append(lines[i]); i += 1; continue
        jmp_start = head[6]
        mt = CAND.search(lines[head[4]])
        if not mt:
            out.append(lines[i]); i += 1; continue
        t0 = mt.group(1)
        if c[6] == f"jp nes_{t0}":
            jmp = [lines[jmp_start]]
        elif c[6] == f"ld a, BANK(nes_{t0})" and len(c) > 8 and c[7] == f"ld hl, nes_{t0}" and c[8] == "jp nes_jump_known_hl_a_8bit":
            jmp = [lines[head[6]], lines[head[7]], lines[head[8]]]
        else:
            out.append(lines[i]); i += 1; continue
        # all candidates of this chain (until the dispatcher fallback)
        cands = []
        for x in range(k, min(k + 400, len(lines))):
            mc = CAND.search(lines[x])
            if mc:
                cands.append(mc.group(1))
            if codes[x] == "jp nes_dispatch_hl" or lines[x].startswith("SECTION"):
                break
        seen += 1
        tot = sum(prof.get(t, 0.0) for t in cands)
        if tot <= 0 or prof.get(t0, 0.0) / tot < a.threshold:
            pair = pair_site(lines, codes, idx[0], k, edges, a.pair_threshold, a.pair_min)
            if pair is None:
                out.append(lines[i]); i += 1; continue
            (lo0, hi0, j0, s0), (lo1, hi1, j1, s1) = pair
            ind = "    "
            lbl = f"nes_rcf_miss_{n}"
            l1 = f"nes_rcf_t1_{n}"
            out.extend([f"{ind}; two-candidate compare-first RTS return (tools/rts_compare_first.py, edge shares {s0:.2f}/{s1:.2f})\n",
                        f"{ind}ldh a, [nes_sp]\n", f"{ind}ld l, a\n", f"{ind}add 2\n", f"{ind}ldh [nes_sp], a\n",
                        f"{ind}ld h, $C1\n", f"{ind}inc l\n", f"{ind}ld a, [hl]\n", f"{ind}inc l\n",
                        f"{ind}cp ${lo0:02X}\n", f"{ind}jr nz, {l1}\n", f"{ind}ld a, [hl]\n",
                        f"{ind}cp ${hi0:02X}\n", f"{ind}jr nz, {lbl}\n"] + j0 + [
                        f"{l1}:\n", f"{ind}cp ${lo1:02X}\n", f"{ind}jr nz, {lbl}\n", f"{ind}ld a, [hl]\n",
                        f"{ind}cp ${hi1:02X}\n", f"{ind}jr nz, {lbl}\n"] + j1 + [
                        f"{lbl}:\n", f"{ind}ld a, [hl]\n", f"{ind}dec l\n", f"{ind}ld c, [hl]\n",
                        f"{ind}ld l, c\n", f"{ind}ld h, a\n"])
            i = k
            n += 1
            n2 += 1
            continue
        ind = "    "
        lbl = f"nes_rcf_miss_{n}"
        new = [f"{ind}; compare-first RTS return (tools/rts_compare_first.py, T0 share {prof.get(t0,0)/tot:.2f})\n",
               f"{ind}ldh a, [nes_sp]\n", f"{ind}ld l, a\n", f"{ind}add 2\n", f"{ind}ldh [nes_sp], a\n",
               f"{ind}ld h, $C1\n", f"{ind}inc l\n", f"{ind}ld a, [hl]\n", f"{ind}inc l\n",
               f"{ind}cp {m_lo.group(1)}\n", f"{ind}jr nz, {lbl}\n", f"{ind}ld a, [hl]\n",
               f"{ind}cp {m_hi.group(1)}\n", f"{ind}jr nz, {lbl}\n"] + jmp + [
               f"{lbl}:\n", f"{ind}ld a, [hl]\n", f"{ind}dec l\n", f"{ind}ld c, [hl]\n",
               f"{ind}ld l, c\n", f"{ind}ld h, a\n"]
        out.extend(new)
        # keep the original chain from "ld a, h" (comments between pop lines dropped)
        i = k
        n += 1
    p.write_text("".join(out))
    print(f"rts-compare-first: {n} of {seen} RTS sites rewritten ({n2} two-candidate)")


if __name__ == "__main__":
    main()
