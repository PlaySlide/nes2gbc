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



_LAST_MAPPER = None


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
        elif f[0] == "M":
            global _LAST_MAPPER
            _LAST_MAPPER = int(f[1])
        elif f[0] == "O":
            nodes[("O", int(f[1], 16))] = {"start": int(f[1], 16), "ins": [], "edges": [],
                                           "img": bytes.fromhex(f[2]) if len(f) > 2 else b""}
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


# Official 6502 opcodes (the only ones the recompiler and the RAM interpreter
# accept; anything else faults: nes_interp_illegal / decode error) -> length.
_LEN = {"imp": 1, "acc": 1, "imm": 2, "zp": 2, "zpx": 2, "zpy": 2, "izx": 2, "izy": 2,
        "rel": 2, "abs": 3, "abx": 3, "aby": 3, "ind": 3}
_OPS = {}
for _m in __import__("re").finditer(r"([0-9A-F]{2}) ([A-Z]{3}) ([a-z]+)",
                                     (Path(__file__).with_name("gen_ram_interp.py")).read_text().split('table = """', 1)[1].split('"""', 1)[0]):
    _OPS[int(_m.group(1), 16)] = (_m.group(2), _LEN[_m.group(3)])
_STOP = {"BPL", "BMI", "BVC", "BVS", "BCC", "BCS", "BNE", "BEQ", "JMP", "JSR", "RTS", "RTI", "BRK"}


def _traps(read, pc):
    """True when straight-line execution from pc reaches an opcode neither
    the recompiler nor the interpreter implements (the run faults) before any
    control transfer. read(pc) -> byte or None (unknown)."""
    for _ in range(64):
        b = read(pc)
        if b is None:
            return False
        if b not in _OPS:
            return True
        mn, ln = _OPS[b]
        if mn in _STOP:
            return False
        pc = (pc + ln) & 0xFFFF
    return False


_MODES = {"imp": "Implied", "acc": "Accumulator", "imm": "Immediate", "zp": "ZeroPage",
          "zpx": "ZeroPageX", "zpy": "ZeroPageY", "izx": "IndexedIndirect", "izy": "IndirectIndexed",
          "rel": "Relative", "abs": "Absolute", "abx": "AbsoluteX", "aby": "AbsoluteY", "ind": "Indirect"}
_OPMODE = {}
for _m in __import__("re").finditer(r"([0-9A-F]{2}) ([A-Z]{3}) ([a-z]+)",
                                     (Path(__file__).with_name("gen_ram_interp.py")).read_text().split('table = """', 1)[1].split('"""', 1)[0]):
    _OPMODE[int(_m.group(1), 16)] = _MODES[_m.group(3)]


def _decode_block(rd, st, stop_at):
    """Straight-line decode from st (official opcodes only) -> (ins, edges),
    or None when it faults (unimplemented opcode) or runs into unknown bytes."""
    pc, ins, edges = st, [], []
    for _ in range(256):
        if pc != st and stop_at(pc):
            return ins, [("fall", pc)]
        b = rd(pc)
        if b is None or b not in _OPS:
            return None
        mn, ln = _OPS[b]
        ops = [rd((pc + i) & 0xFFFF) for i in range(1, ln)]
        if any(x is None for x in ops):
            return None
        mode = _OPMODE[b]
        opnd = (ops[0] | ops[1] << 8) if ln == 3 else None
        ins.append((pc, mn.capitalize(), mode, opnd))
        nxt = (pc + ln) & 0xFFFF
        if mn in ("BPL", "BMI", "BVC", "BVS", "BCC", "BCS", "BNE", "BEQ"):
            t = (nxt + (ops[0] - 256 if ops[0] >= 128 else ops[0])) & 0xFFFF
            return ins, [("branch", t), ("fall", nxt)]
        if mn == "JSR":
            return ins, [("call", opnd), ("ret", nxt)]
        if mn == "JMP":
            return ins, [("jump", opnd) if mode == "Absolute" else ("ind", None)]
        if mn in ("RTS", "RTI", "BRK"):
            return ins, []
        pc = nxt
    return None


def synth_nodes(path, nodes):
    """Decode code the recompiler left untranslated (self-modifying PRG-RAM
    blocks, ROM targets it never discovered) so walks through code run by the
    interpreter see where it can go. Their RTSs are interpreted, so their own
    sets are never used; only reachability matters. Also resolves JMP (ind)
    through a pointer in PRG ROM (a constant). Returns the set of PCs whose
    execution faults in every mapping (no successor)."""
    prgp = Path(str(path).replace(".cfgviews.txt", ".prg.bin"))
    prg = prgp.read_bytes() if prgp.exists() else None
    nb = len(prg) // 0x4000 if prg else 0
    ov = [(k[1], v["img"]) for k, v in nodes.items() if k[0] == "O"]

    def rd_ram(a):
        for dst, img in ov:
            if dst <= a < dst + len(img):
                return img[a - dst]
        return None

    def rd_bank(bk):
        def rd(a):
            if prg is None:
                return None
            if 0x8000 <= a < 0xC000:
                return prg[bk * 0x4000 + a - 0x8000]
            if a >= 0xC000:
                return prg[(nb - 1) * 0x4000 + a - 0xC000]
            return None
        return rd
    # JMP (ind) through a ROM pointer.
    for key, b in list(nodes.items()):
        if key[0] == "O" or not b["ins"] or prg is None:
            continue
        _, m, mode, op = b["ins"][-1]
        if m != "Jmp" or mode != "Indirect" or op is None or op < 0x8000:
            continue
        if not any(t is None for k, t in b["edges"] if k == "ind"):
            continue
        hi = (op & 0xFF00) | ((op + 1) & 0xFF)
        bks = [nb - 1] if op >= 0xC000 else ([key[0]] if key[0] not in ("F", "R") else range(nb))
        tg = {rd_bank(bk)(op) | rd_bank(bk)(hi) << 8 for bk in bks}
        b["edges"] = [e for e in b["edges"] if e != ("ind", None)] + [("ind", t) for t in sorted(tg)]
    dead = set()
    work = [(k, t) for k, b in list(nodes.items()) if k[0] != "O" for kk, t in b["edges"]
            if t is not None and kk in INTRA | {"call"}]
    done = set()
    while work:
        src, t = work.pop()
        if (src[0], t) in done:
            continue
        done.add((src[0], t))
        if t < 0x8000:
            cands = [("R", t, rd_ram)] if rd_ram(t) is not None else []
        elif t >= 0xC000:
            cands = [("F", t, rd_bank(nb - 1))] if prg else []
        elif src[0] not in ("F", "R") and not _may_switch(nodes[src]) and any(
                k3 in ("fall", "branch", "jump") and t3 == t for k3, t3 in nodes[src]["edges"]):
            cands = [(src[0], t, rd_bank(src[0]))] if prg else []
        else:
            cands = [(bk, t, rd_bank(bk)) for bk in range(nb)] if prg else []
        if not cands or any((c[0], t) in nodes for c in cands):
            continue
        made = 0
        for view, pc, rd in cands:
            r = _decode_block(rd, pc, lambda q, v=view: (v, q) in nodes)
            if r is None:
                continue
            ins, edges = r
            nodes[(view, pc)] = {"start": pc, "ins": ins, "edges": edges, "view": view, "synth": True}
            made += 1
            for k2, t2 in edges:
                if t2 is not None:
                    work.append(((view, pc), t2))
        if not made:
            dead.add(t)
    return dead


def trap_conts(path, nodes, banks):
    """Return continuations whose straight-line execution faults in
    every PRG bank that can be mapped there (or in the PRG-RAM image)."""
    prgp = Path(str(path).replace(".cfgviews.txt", ".prg.bin"))
    prg = prgp.read_bytes() if prgp.exists() else None
    ov = [(k[1], v["img"]) for k, v in nodes.items() if k[0] == "O"]
    nb = len(prg) // 0x4000 if prg else 0

    def rd_bank(bk):
        def rd(a):
            if 0x8000 <= a < 0xC000:
                return prg[bk * 0x4000 + a - 0x8000]
            if a >= 0xC000:
                return prg[(nb - 1) * 0x4000 + a - 0xC000]
            return None
        return rd

    def rd_ram(a):
        for dst, img in ov:
            if dst <= a < dst + len(img):
                return img[a - dst]
        return None
    out = set()
    rets = {t for k, b in nodes.items() for kk, t in b["edges"] if kk == "ret" and t is not None}
    for c in rets:
        if c < 0x8000:
            if _traps(rd_ram, c):
                out.add(c)
            continue
        if prg is None:
            continue
        if c >= 0xC000:
            if _traps(rd_bank(nb - 1), c):
                out.add(c)
            continue
        if all(_traps(rd_bank(b), c) for b in range(nb)):
            out.add(c)
    return out


def return_sets_banked(path, track=True):
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
    Untranslated code (self-modifying PRG-RAM blocks, ROM targets the
    recompiler never discovered) is decoded here from the PRG image / the
    dumped overlay image so walks go through it instead of treating it as a
    dynamic transfer; JMP (ind) through a PRG-ROM pointer is resolved; a
    target or return continuation that hits an unimplemented opcode in every
    mapping faults when run and gets no successor.
    Residual assumptions: code run by the 6502 interpreter does not leave the
    stack unbalanced when it hands control to translated code; no code runs
    from $2000-$5FFF (register space / open bus; NES2GBC_RTS_IO_CALLS=1
    turns that off)."""
    global _LAST_MAPPER
    _LAST_MAPPER = None
    vectors, nodes = load_views(path)
    mapper = _LAST_MAPPER
    banks = sorted({k for k, _ in nodes if k not in ("F", "R", "O")})
    # Continuations that fault when entered: a JSR whose return lands there
    # never resumes translated or interpreted code, so it contributes no
    # return target (and no dynamic one).
    dead = synth_nodes(path, nodes)
    io_code = __import__("os").environ.get("NES2GBC_RTS_IO_CALLS") == "1"
    if not io_code:
        # Code in PPU/APU/IO register space or unmapped open bus ($2000-$5FFF
        # on these mappers) would execute register reads as opcodes; no game
        # does this, such transfers come from decoded data. No successor.
        dead |= {t for b in nodes.values() for _, t in b["edges"] if t is not None and 0x2000 <= t < 0x6000}
    traps = trap_conts(path, nodes, banks)
    for b in nodes.values():
        b["edges"] = [e for e in b["edges"] if not (e[0] == "ret" and e[1] in traps)]

    def resolve(src_key, src, kind, t):
        if t >= 0xC000:
            k = ("F", t)
            return [k] if k in nodes else None
        if t < 0x8000:
            k = ("R", t)
            return [k] if k in nodes else None
        if src_key[0] not in ("F", "R") and not _may_switch(src) and (
                kind in ("fall", "branch", "jump", "call")
                or kind in ("ret", "ind") and _keeps_bank(src_key, src)):
            k = (src_key[0], t)
            return [k] if k in nodes else None
        ks = [(b, t) for b in banks if (b, t) in nodes]
        return ks or None

    # Callees that never change the PRG bank (no mapper store anywhere in
    # their body or transitive callees, no dynamic transfer): a JSR to them
    # from banked code returns to the same bank. Iterated to a fixpoint since
    # callee bodies are resolved with the current summary.
    switching = set()
    sw_why = {}
    disp = {t for b in nodes.values() if any(k == "ind" for k, _ in b["edges"])
            for k, t in b["edges"] if k == "call"}

    def _keeps_bank(src_key, src):
        for k, t in src["edges"]:
            if k != "call":
                continue
            ks = resolve(src_key, src, "call", t)
            if not ks or any(x in switching for x in ks):
                return False
        return True

    def _switches(e):
        seen = {e}
        work = [e]
        while work:
            s_ = work.pop()
            b = nodes[s_]
            if _may_switch(b) or any(m in BAD for _, m, _, _ in b["ins"]):
                return (sw_why.__setitem__(e, (s_, 7)) or True)
            for k, t in b["edges"]:
                if k == "call":
                    ks = resolve(s_, b, k, t)
                    if not ks:
                        if t in dead or (0x2000 <= t < 0x6000 and not io_code):
                            continue
                        return (sw_why.__setitem__(e, (s_, 14)) or True)
                    if any(x in switching for x in ks):
                        return (sw_why.__setitem__(e, (s_, 16)) or True)
                    continue
                if k not in INTRA:
                    continue
                if k == "ind" and e[1] in disp and any(m in ("Pla",) for _, m, _, _ in nodes[e]["ins"] + b["ins"]):
                    continue          # inline dispatcher: targets are on the calling block
                if t is None:
                    return (sw_why.__setitem__(e, (s_, 23)) or True)
                if t in dead:
                    continue
                ks = resolve(s_, b, k, t)
                if ks is None:
                    if 0x2000 <= t < 0x6000 and not io_code:
                        continue
                    return (sw_why.__setitem__(e, (s_, 30)) or True)
                for x in ks:
                    if x not in seen:
                        seen.add(x)
                        work.append(x)
        return False
    callees = set()
    for key, b in nodes.items():
        for k, t in b["edges"]:
            if k == "call":
                callees.update(resolve(key, b, "call", t) or [])
    while True:
        new = {e for e in callees if e not in switching and _switches(e)}
        if not new:
            break
        switching |= new

    conts = collections.defaultdict(set)
    allconts = set()
    dyn = set()
    dsrc = []
    gn = []
    inline_targets = set()
    _prgp = Path(str(path).replace(".cfgviews.txt", ".prg.bin"))
    _prg = _prgp.read_bytes() if _prgp.exists() else None

    def _rd_view(bk):
        nb_ = len(_prg) // 0x4000

        def rd(a):
            if 0x8000 <= a < 0xC000:
                return _prg[bk * 0x4000 + a - 0x8000]
            if a >= 0xC000:
                return _prg[(nb_ - 1) * 0x4000 + a - 0xC000]
            return None
        return rd
    view_traps = 0
    for key, b in nodes.items():
        call = [t for k, t in b["edges"] if k == "call"]
        ret = [t for k, t in b["edges"] if k == "ret"]
        if (ret and call and _prg is not None and isinstance(key[0], int)
                and __import__("os").environ.get("NES2GBC_RTS_VIEW_TRAPS", "0") != "0"
                and not _may_switch(b)):
            # A JSR fetched from PRG bank key[0] whose callees can never
            # switch banks returns into that same bank: a continuation whose
            # straight-line code there faults never resumes, so it is no
            # return target (trap_conts only drops it when it faults in
            # every bank).
            kss = [resolve(key, b, "call", t) for t in call]
            if all(ks and not any(x in switching for x in ks) for ks in kss):
                rd = _rd_view(key[0])
                keep = [r for r in ret if not (0x8000 <= r < 0xC000 and _traps(rd, r))]
                view_traps += len(ret) - len(keep)
                ret = keep
        allconts.update(ret)
        for t in call:
            ks = resolve(key, b, "call", t)
            if ks is None:
                if t in dead:
                    continue
                if 0x2000 <= t < 0x6000 and not io_code:
                    # JSR into PPU/APU/IO registers or unmapped open bus:
                    # register reads would be executed as opcodes. No game
                    # does this; such call sites are decoded data. Treated
                    # as non-returning (NES2GBC_RTS_IO_CALLS=1 disables).
                    continue
                dyn.update(ret)            # interpreted callee
                dsrc.append(("call", key, t, tuple(ret)))
                continue
            for k in ks:
                conts[k].update(ret)
        for k, t in b["edges"]:
            if k == "ind" and t is not None and call:
                for kk in resolve(key, b, "ind", t) or []:
                    inline_targets.add(kk)
    dynamic = {("F", v) for v in vectors if ("F", v) in nodes}
    rts = {k for k, b in nodes.items() if b["ins"] and b["ins"][-1][1] == "Rts"}

    # PRG bank tracking through fixed-bank / RAM code: a walk state is
    # (node, bank) where bank is the switchable bank known to be mapped at
    # $8000 (None: unknown). Banked nodes always run in their own bank.
    # Known after `LDA #k; JSR <MMC1 serial PRG write of A>` (five
    # STA $E000-$FFFF / LSR A, RTS) and, on UxROM, `STA abs>=$8000` of a
    # constant register. Any other mapper store, or a callee that may
    # switch, makes it unknown.
    nbanks = len(banks)
    bmask = (1 << max(0, (max(banks) if banks else 0).bit_length())) - 1

    def _serial(k):
        b = nodes.get(k)
        if not b or b["edges"] or not b["ins"] or b["ins"][-1][1] != "Rts":
            return False
        body = b["ins"][:-1]
        sts = [x for x in body if x[1] == "Sta"]
        return (mapper == 1 and len(sts) == 5 and all(x[2] == "Absolute" and x[3] is not None and x[3] >= 0xE000 for x in sts)
                and all(x[1] == "Sta" or (x[1] == "Lsr" and x[2] == "Accumulator") for x in body))
    serial = {k for k in nodes if k[0] == "F" and _serial(k)}

    def bank_after(key, b, bank):
        if not track:
            return (key[0] if key[0] not in ("F", "R") else None), None
        if key[0] not in ("F", "R"):
            bank = key[0]
        regs = {"a": None, "x": None, "y": None}
        for _, m, mode, op in b["ins"]:
            if m in ("Lda", "Ldx", "Ldy"):
                r = m[2].lower()
                regs[r] = ("imm", key, _) if mode == "Immediate" else None
            elif m in STORES and (op is None or op >= 0x8000) and mode not in ("ZeroPage", "ZeroPageX", "ZeroPageY"):
                bank = None
                if mapper == 2 and mode == "Absolute" and op is not None and regs[m[2].lower()] is not None:
                    v = _imm(regs[m[2].lower()])
                    if v is not None:
                        bank = v & bmask
            elif m in ("Tax", "Tay"):
                regs[m[2].lower()] = regs["a"]
            elif m in ("Txa", "Tya"):
                regs["a"] = regs[m[1].lower()]
            elif m in ("Pla", "Adc", "Sbc", "And", "Ora", "Eor") or (m in ("Asl", "Lsr", "Rol", "Ror") and mode == "Accumulator"):
                regs["a"] = None
            elif m in ("Inx", "Dex", "Tsx"):
                regs["x"] = None
            elif m in ("Iny", "Dey"):
                regs["y"] = None
        return bank, regs["a"]

    def _imm(tag):
        _, key, pc = tag
        prgp = imm_src
        return prgp(key, pc)
    _prg = Path(str(path).replace(".cfgviews.txt", ".prg.bin"))
    _prgb = _prg.read_bytes() if _prg.exists() else None

    def imm_src(key, pc):
        if _prgb is None:
            return None
        nb = len(_prgb) // 0x4000
        if pc >= 0xC000:
            return _prgb[(nb - 1) * 0x4000 + pc - 0xC000 + 1]
        if pc >= 0x8000 and key[0] not in ("F", "R"):
            return _prgb[key[0] * 0x4000 + pc - 0x8000 + 1]
        img = [v["img"] for k, v in nodes.items() if k[0] == "O" and k[1] <= pc + 1 < k[1] + len(v["img"])]
        return img[0][pc + 1 - [k for k in nodes if k[0] == "O" and nodes[k]["img"] is img[0]][0][1]] if img else None

    def succ_states(key, b, bank_in):
        """[(kind, target, [(node, bank)] or None)] for INTRA edges."""
        bank, a = bank_after(key, b, bank_in)
        calls = [t for k, t in b["edges"] if k == "call"]
        after_call = bank
        for t in calls:
            ks = resolve2(key, b, "call", t, bank)
            if ks and all(k in serial for k in ks) and a is not None and _imm(a) is not None:
                after_call = _imm(a) & bmask
            elif not ks or any(k in switching for k in ks):
                after_call = None
        out = []
        for k, t in b["edges"]:
            if k not in INTRA:
                continue
            if t is None:
                out.append((k, t, None))
                continue
            bk = after_call if k in ("ret", "ind") else bank
            ks = resolve2(key, b, k, t, bk)
            out.append((k, t, None if ks is None else [(x, x[0] if x[0] not in ("F", "R") else bk) for x in ks]))
        return out

    lazy_dead = set()

    def resolve2(key, b, kind, t, bank):
        if 0x8000 <= t < 0xC000 and bank is None and key[0] not in ("F", "R") and not _may_switch(b) and kind in ("fall", "branch", "jump", "call"):
            bank = key[0]
        if 0x8000 <= t < 0xC000 and bank is not None:
            k = (bank, t)
            if track and k not in nodes and k not in lazy_dead and _prgb is not None and bank * 0x4000 < len(_prgb):
                # Untranslated code in a known bank: decode it for the walk.
                def rd(a, bk=bank):
                    if 0x8000 <= a < 0xC000:
                        return _prgb[bk * 0x4000 + a - 0x8000]
                    if a >= 0xC000:
                        return _prgb[len(_prgb) - 0x4000 + a - 0xC000]
                    return None
                r = _decode_block(rd, t, lambda q: (bank, q) in nodes)
                if r is None:
                    lazy_dead.add(k)
                else:
                    nodes[k] = {"start": t, "ins": r[0], "edges": r[1], "view": bank, "synth": True}
                    if r[0] and r[0][-1][1] == "Rts":
                        rts.add(k)
            if k in lazy_dead:
                return []
            return [k] if k in nodes else None
        return resolve(key, b, kind, t)

    def walk(e):
        """Yield (rts_key, depth) and record dynamic transfers; returns bad flag."""
        st0 = (e, e[0] if e[0] not in ("F", "R") else None)
        depth = {e: 0}
        seen = {st0}
        work = [st0]
        bad = False
        hits = []
        dyn_d = []
        while work:
            s0 = work.pop()
            s_, bank_in = s0
            b = nodes[s_]
            d = depth[s_]
            for _, m, _, _ in b["ins"]:
                if m in BAD:
                    bad = True
                d += PUSH.get(m, 0)
            if s_ in rts:
                hits.append((s_, d))
            for k, t, sts in succ_states(s_, b, bank_in):
                if t is not None and t in dead:
                    continue                 # faults when executed
                if t is None:
                    dyn_d.append((d, s_, "ind"))          # unresolved indirect jump
                    continue
                if sts is None:
                    dyn_d.append((d, s_, (k, t)))          # untranslated target: interpreter
                    continue
                for st in sts:
                    tk = st[0]
                    if k == "ind" and d < 0:
                        if d == -2 and tk in inline_targets:
                            continue
                        bad = True
                        continue
                    if tk in depth and depth[tk] != d:
                        bad = True
                        continue
                    depth[tk] = d
                    if st not in seen:
                        seen.add(st)
                        work.append(st)
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
                dsrc.append(("pushrts", e, s_, tuple(conts[e])))
            if d != 0 or bad or e in dynamic:
                unknown.add(s_)
        for d, site, why in dyn_d:
            if d == 0:
                dyn.update(conts[e])
                if conts[e]:
                    dsrc.append((why, e, site, tuple(conts[e])))
            else:
                global_none = True
    # Unknown handler starts: every node at relative depth 0.
    for st in list(nodes):
        hits, dyn_d, bad = walk(st)
        for s_, d in hits:
            if d != 0 or bad:
                unknown.add(s_)
        if any(d != 0 for d, _, _ in dyn_d):
            # a handler that pushes/pops before another dynamic transfer
            global_none = True
            gn.append((st, [x for x in dyn_d if x[0] != 0][:2]))
    if global_none and track:
        # Bank tracking reaches more (decoded) code; when that makes the
        # analysis incomplete, fall back to the untracked over-approximation.
        return return_sets_banked(path, track=False)
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
    info = {"traps": len(traps), "view_traps": view_traps, "global_none": global_none, "dyn": len(dyn), "allconts": len(allconts),
            "complete": not global_none, "dyn_src": dsrc, "gn": gn, "sw_why": sw_why, "switching": switching, "_resolve": resolve, "_nodes": nodes}
    return out, info
