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



def load_views(path):
    """Parse <stem>.cfgviews.txt: one section per PRG view ('W bb') plus the
    PRG-RAM overlay ('W RAM'). Nodes are (kind, pc): ('F', pc) for the fixed
    bank $C000-$FFFF (merged across views), (bank, pc) for $8000-$BFFF and
    ('R', pc) for overlay code below $8000."""
    vectors, nodes, view = [], {}, None
    cur = None
    for line in Path(path).read_text().splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "V":
            vectors = [int(x, 16) for x in f[1:]]
        elif f[0] == "W":
            view = "R" if f[1] == "RAM" else int(f[1], 16)
        elif f[0] == "B":
            pc = int(f[1], 16)
            key = ("F", pc) if pc >= 0xC000 else ("R", pc) if view == "R" else (view, pc)
            if key[0] == "F" and key in nodes:
                cur = {"start": pc, "ins": [], "edges": [], "dup": True, "view": view}
            else:
                cur = {"start": pc, "ins": [], "edges": [], "view": view}
                nodes[key] = cur
        elif f[0] == "I":
            if not cur.get("dup"):
                cur["ins"].append((int(f[1], 16), f[2], f[3], int(f[4], 16) if len(f) > 4 else None))
        elif f[0] == "E":
            e = (f[1], None if f[2] == "-" else int(f[2], 16))
            tgt = cur if not cur.get("dup") else nodes[("F", cur["start"])]
            if e not in tgt["edges"]:
                tgt["edges"].append(e)
    return vectors, nodes


STORES = {"Sta", "Stx", "Sty"}


def _may_switch(b):
    """A store that may hit the mapper ($8000+ or not statically known)."""
    for _, m, mode, op in b["ins"]:
        if m in STORES and (op is None or op >= 0x8000) and mode not in ("ZeroPage", "ZeroPageX", "ZeroPageY"):
            if op is None or mode in ("Absolute", "AbsoluteX", "AbsoluteY") and op >= 0x8000 or op is None:
                return True
    return False


def return_sets_banked(path):
    """Return sets for banked builds over every PRG view and the RAM overlay.

    Bank switching: a low-window ($8000-$BFFF) successor stays in the source
    bank only for fall/branch/jump out of a block that cannot write the
    mapper; from fixed/RAM code, after a mapper store, and for JSR targets
    and return continuations (the callee may switch) it is that PC in every
    bank with code there.

    Dynamic transfers are over-approximated instead of giving up:
    * an unresolved JMP (ind) reached at depth 0 of entry E, a push-RTS
      dispatch (RTS reached at depth exactly 2 of E) and a JSR whose target
      has no analysed block (interpreted) let control reach an unknown block
      start S at E's depth 0; any RTS reachable from S at relative depth 0
      then returns to conts(E). These conts form DYN and are added to every
      RTS that some block start reaches at relative depth 0 (all of them).
    * any RTS reachable from some block start at a non-zero relative depth,
      or a dynamic transfer at any other depth, is unknown (None).
    Residual assumption: code run by the 6502 interpreter does not leave the
    stack unbalanced when it hands control to translated code."""
    vectors, nodes = load_views(path)
    banks = sorted({k for k, _ in nodes if k not in ("F", "R")})

    def resolve(src_key, src, kind, t):
        if t >= 0xC000:
            k = ("F", t)
            return [k] if k in nodes else None
        if t < 0x8000:
            k = ("R", t)
            return [k] if k in nodes else None
        if src_key[0] not in ("F", "R") and kind in ("fall", "branch", "jump") and not _may_switch(src):
            k = (src_key[0], t)
            if k in nodes:
                return [k]
        ks = [(b, t) for b in banks if (b, t) in nodes]
        return ks or None

    conts = collections.defaultdict(set)
    allconts = set()
    dyn = set()
    inline_targets = set()
    for key, b in nodes.items():
        call = [t for k, t in b["edges"] if k == "call"]
        ret = [t for k, t in b["edges"] if k == "ret"]
        allconts.update(ret)
        for t in call:
            ks = resolve(key, b, "call", t)
            if ks is None:
                dyn.update(ret)            # interpreted callee
                continue
            for k in ks:
                conts[k].update(ret)
        for k, t in b["edges"]:
            if k == "ind" and t is not None and call:
                for kk in resolve(key, b, "ind", t) or []:
                    inline_targets.add(kk)
    dynamic = {("F", v) for v in vectors if ("F", v) in nodes}
    rts = {k for k, b in nodes.items() if b["ins"] and b["ins"][-1][1] == "Rts"}

    def walk(e):
        """Yield (rts_key, depth) and record dynamic transfers; returns bad flag."""
        depth = {e: 0}
        work = [e]
        bad = False
        hits = []
        dyn_d = []
        while work:
            s_ = work.pop()
            b = nodes[s_]
            d = depth[s_]
            for _, m, _, _ in b["ins"]:
                if m in BAD:
                    bad = True
                d += PUSH.get(m, 0)
            if s_ in rts:
                hits.append((s_, d))
            for k, t in b["edges"]:
                if k not in INTRA:
                    continue
                if t is None:
                    dyn_d.append(d)          # unresolved indirect jump
                    continue
                ks = resolve(s_, b, k, t)
                if ks is None:
                    dyn_d.append(d)          # untranslated target: interpreter
                    continue
                for tk in ks:
                    if k == "ind" and d < 0:
                        if d == -2 and tk in inline_targets:
                            continue
                        bad = True
                        continue
                    if tk in depth:
                        if depth[tk] != d:
                            bad = True
                        continue
                    depth[tk] = d
                    work.append(tk)
        return hits, dyn_d, bad

    ctx = collections.defaultdict(set)
    unknown = set()
    global_none = False
    for e in set(conts) | dynamic:
        hits, dyn_d, bad = walk(e)
        for s_, d in hits:
            ctx[s_].add(e)
            if d == 2:
                dyn.update(conts[e])        # push-RTS dispatch
            if d != 0 or bad or e in dynamic:
                unknown.add(s_)
        for d in dyn_d:
            if d == 0:
                dyn.update(conts[e])
            else:
                global_none = True
    # Unknown handler starts: every node at relative depth 0.
    for st in nodes:
        hits, dyn_d, bad = walk(st)
        for s_, d in hits:
            if d != 0 or bad:
                unknown.add(s_)
        if any(d != 0 for d in dyn_d):
            # a handler that pushes/pops before another dynamic transfer
            global_none = True
    out = {}
    for s_ in rts:
        pc = nodes[s_]["ins"][-1][0]
        if global_none or s_ in unknown:
            res = None
        else:
            res = set(dyn)
            for e in ctx[s_]:
                res |= conts[e]
        if pc in out:
            res = None if (out[pc] is None or res is None) else (out[pc] | res)
        out[pc] = res
    info = {"global_none": global_none, "dyn": len(dyn), "allconts": len(allconts),
            "complete": not global_none}
    return out, info
