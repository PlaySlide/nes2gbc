#!/usr/bin/env python3
"""Static return-target sets for 6502 RTS instructions.

Input: the sidecar CFG dump written next to generated.asm by the recompiler
(`<stem>.cfg.txt`: vectors, blocks, instructions and typed edges).

For every subroutine entry E (a static JSR target) we walk E's body along
intra-procedural edges (fallthrough, branches, JMP abs, JSR->return
continuation, resolved indirect-jump targets; never into callees) and track
the 6502 stack depth relative to E's entry. An RTS reached at depth 0 returns
to one of E's JSR continuations.

Indirect jumps are tail jumps: a handler T reached from E's body at depth 0
returns to E's continuations. For a non-returning inline JSR dispatcher (SMB
JumpEngine: `JSR D` followed by a word table; D pops that JSR frame and does
JMP (ptr)), the CFG puts the table targets on the calling block, so the
handler is walked from the caller's region at the caller's depth; the
dispatcher's own JMP (ptr), reached at depth -2 inside D, is not followed
again when every one of its targets is covered by such a calling block.

`return_sets()` maps each RTS PC to the set of continuation PCs it can return
to, or to None (unknown, i.e. treat every return as reaching anything) when:
* the RTS is reachable from a reset/NMI/IRQ vector body, whose return
  context is unknown;
* the RTS is reached at a non-zero or inconsistent stack depth, or its body
  contains TXS/RTI/BRK;
* the RTS is not reachable from any entry.
If any RTS anywhere is reached with bytes pushed (depth > 0: push-and-RTS
dispatch to a computed address) or the CFG has an unresolved indirect jump,
dynamic targets cannot be enumerated and every RTS maps to None.
"""
from __future__ import annotations
import collections, sys
from pathlib import Path

PUSH = {"Pha": 1, "Php": 1, "Pla": -1, "Plp": -1}
BAD = {"Txs", "Rti", "Brk"}
INTRA = {"fall", "branch", "jump", "ret", "ind"}


def load(path):
    vectors, blocks = [], {}
    cur = None
    for line in Path(path).read_text().splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "V":
            vectors = [int(x, 16) for x in f[1:]]
        elif f[0] == "B":
            cur = {"start": int(f[1], 16), "ins": [], "edges": []}
            blocks[cur["start"]] = cur
        elif f[0] == "I":
            cur["ins"].append((int(f[1], 16), f[2]))
        elif f[0] == "E":
            cur["edges"].append((f[1], None if f[2] == "-" else int(f[2], 16)))
    return vectors, blocks


def return_sets(path):
    vectors, blocks = load(path)
    conts = collections.defaultdict(set)   # entry -> JSR continuations
    dynamic = set(vectors)
    unresolved = False
    inline_targets = set()                 # handlers listed on an inline-dispatch calling block
    for b in blocks.values():
        call = [t for k, t in b["edges"] if k == "call"]
        ret = [t for k, t in b["edges"] if k == "ret"]
        for t in call:
            conts[t]  # entry exists even without a continuation (non-returning dispatcher)
            for r in ret:
                conts[t].add(r)
        for k, t in b["edges"]:
            if k == "ind":
                if t is None:
                    unresolved = True
                elif call:
                    inline_targets.add(t)
    rts_blocks = {s for s, b in blocks.items() if b["ins"] and b["ins"][-1][1] == "Rts"}
    ctx = collections.defaultdict(set)      # rts block -> entries
    unknown = set()
    pushed = False
    for e in set(conts) | dynamic:
        if e not in blocks:
            continue
        depth = {e: 0}
        work = [e]
        bad = e in dynamic
        while work:
            s = work.pop()
            b = blocks[s]
            d = depth[s]
            for _, m in b["ins"]:
                if m in BAD:
                    bad = True
                d += PUSH.get(m, 0)
            if s in rts_blocks:
                d_at_rts = d
                ctx[s].add(e)
                if d_at_rts != 0:
                    unknown.add(s)
                    if d_at_rts > 0:
                        pushed = True
            for k, t in b["edges"]:
                if k not in INTRA or t is None or t not in blocks:
                    continue
                if k == "ind" and d < 0:
                    if d == -2 and t in inline_targets:
                        continue   # dispatcher internals; handled at the calling block
                    bad = True
                    continue
                if t in depth:
                    if depth[t] != d:
                        bad = True
                    continue
                depth[t] = d
                work.append(t)
        if bad:
            for s in depth:
                if s in rts_blocks:
                    unknown.add(s)
    out = {}
    for s in rts_blocks:
        pc = blocks[s]["ins"][-1][0]
        if unresolved or pushed or s in unknown or not ctx[s] or (ctx[s] & dynamic):  # dynamic == vectors
            res = None
        else:
            res = set()
            for e in ctx[s]:
                res |= conts[e]
        if pc in out and out[pc] != res:
            if out[pc] is None or res is None:
                res = None
            else:
                res = out[pc] | res
        out[pc] = res
    return out, {"unresolved": unresolved, "pushed": pushed}


if __name__ == "__main__":
    out, info = return_sets(sys.argv[1])
    known = [p for p, s in out.items() if s is not None]
    print(info, "rts", len(out), "known", len(known))
    if len(sys.argv) > 2:
        for p in sorted(out):
            print("%04X" % p, "?" if out[p] is None else " ".join("%04X" % c for c in sorted(out[p])))
