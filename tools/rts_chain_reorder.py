#!/usr/bin/env python3
"""Order guarded RTS return compare chains by measured return edges (late pass).

profiles/<rom>.rtsedge (tools/bench/rts_edge_profile.py) gives, per chain
(tools/rts_chain.py key) and return PC, the returns per frame. Each chain is
re-emitted with its hi-byte groups sorted by total returns and the candidates
of a group by their own returns (ties keep the original order); the low byte
stays in A across a group's compares, so only one `ld a, l` per group is
emitted. The candidate set, every exact guard and the unmatched fallback are
unchanged, so a stale or missing profile only affects speed.
"""
import argparse, collections, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rts_chain  # noqa: E402


def load(path):
    d = collections.defaultdict(dict)
    try:
        for l in open(path):
            if l.startswith("#") or not l.strip():
                continue
            v, key, pc = l.split()[:3]
            d[key][int(pc, 16)] = float(v)
    except OSError:
        pass
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("asm")
    ap.add_argument("--edge-profile", default="")
    a = ap.parse_args()
    prof = load(a.edge_profile) if a.edge_profile else {}
    if not prof:
        print("rts-chain-reorder: no edge profile, skipped")
        return
    p = Path(a.asm)
    lines = p.read_text().splitlines(keepends=True)
    chains = rts_chain.parse(lines)
    refs = collections.Counter()
    word = re.compile(r"\bnes_rts_[\w]+")
    for l in lines:
        c = rts_chain.code(l)
        if c and not c.endswith(":"):
            for m in word.finditer(c):
                refs[m.group(0)] += 1
    edits = []
    moved = 0
    for n, ch in enumerate(chains):
        w = prof.get(ch.key)
        if not w:
            continue
        # region-internal references only (each label is the target of jr's inside)
        inside = collections.Counter()
        for l in lines[ch.start:ch.end]:
            c = rts_chain.code(l)
            if c and not c.endswith(":"):
                for m in word.finditer(c):
                    inside[m.group(0)] += 1
        if any(refs[lb] != inside[lb] for lb in ch.labels):
            continue
        seen = set()
        cands = []
        for i, c in enumerate(ch.cands):
            if (c["hi"], c["lo"]) in seen:
                continue
            seen.add((c["hi"], c["lo"]))
            cands.append((i, c))
        groups = collections.OrderedDict()
        for i, c in cands:
            groups.setdefault(c["hi"], []).append((i, c))
        cw = lambda ic: w.get(ic[1]["target"], 0.0)
        order = sorted(groups.items(), key=lambda kv: (-sum(cw(x) for x in kv[1]), kv[1][0][0]))
        order = [(hi, [c for _i, c in sorted(cs, key=lambda x: (-cw(x), x[0]))]) for hi, cs in order]
        new_seq = [c["target"] for _hi, cs in order for c in cs]
        old_seq = [c["target"] for _i, c in cands]
        if new_seq != old_seq:
            moved += 1
        edits.append((ch.start, ch.end, rts_chain.emit(ch, order, f"{n}")))
    for s, e, txt in sorted(edits, reverse=True):
        lines[s:e] = txt
    p.write_text("".join(lines))
    print(f"rts-chain-reorder: {len(edits)} chain(s) re-emitted, {moved} reordered")


if __name__ == "__main__":
    main()
