#!/usr/bin/env python3
"""Global (host-CFG) dead-store elimination for canonical 6502 state in HRAM.

Final pass over generated.asm, after every pattern-matching pass. Builds a
line-level host control-flow graph over translated code sections ("NES block"
and "canonical superblock entry") and solves backward liveness for
nes_a, nes_x, nes_y and the lazy Z/N/C shadows. A store `ldh [S], a` is
removed when S is dead afterwards on every host path.

Conservative model:
* edges follow fallthrough, jp/jr to labels defined inside translated code
  sections, and `ld hl, <label>` + `jp nes_jump_known_hl_a[_8bit]` bank
  transfers;
* the generic `jp nes_dispatch_hl` fallback of a translated 6502 RTS gets
  edges to that RTS's static return continuations when tools/rts_return_sets.py
  can prove them from the recompiler's CFG dump (`<stem>.cfg.txt`); RTS sites
  it cannot prove keep the conservative all-live exit;
* every other exit (ret/reti/jp hl, dispatch, NMI entry, RTS helpers, data,
  SECTION boundaries, unresolved targets) treats all six bytes as live;
* calls are transparent only for whitelisted helpers that never read these
  bytes; any other call reads everything (nes_poll_nmi_hl materializes P);
* `IF 0` blocks are ignored; `IF DEF(...)` trace blocks must contain only
  ld/push/pop/call nes_profile_trace_pc, otherwise they read everything.
Any other mention of a tracked byte is a read.
"""
from __future__ import annotations
import bisect, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rts_return_sets import return_sets  # noqa: E402

INSN_RE = re.compile(r"; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ")

VARS = ["nes_a", "nes_x", "nes_y", "nes_z_shadow", "nes_n_shadow", "nes_c_shadow"]
BIT = {v: 1 << i for i, v in enumerate(VARS)}
ALL = (1 << len(VARS)) - 1
STORE_RE = re.compile(r"^ldh \[(nes_(?:a|x|y|z_shadow|n_shadow|c_shadow))\], a$")
MENTION_RE = re.compile(r"\b(nes_(?:a|x|y|z_shadow|n_shadow|c_shadow))\b")
SAFE_CALLS = {
    "nes_cpu_read", "nes_cpu_write", "nes_ppu_write_data", "nes_ppu_read_data",
    "nes_controller_write", "nes_profile_trace_pc", "nes_cpu_read_joy_hl",
    "nes_controller_read",
    "nes_ppu_cpu_write.addr", "nes_ppu_cpu_write.ctrl", "nes_ppu_cpu_write.scroll",
    "nes_ppu_cpu_write.mask", "nes_ppu_cpu_write.oamaddr",
    # Pure PRG/bus read helpers (subsets of nes_cpu_read; no HRAM CPU state).
    "nes_cpu_read_hi", "nes_cpu_read_hi32", "nes_prg_read_lo_a", "nes_prg_read_hi_a",
    "nes_prg_read_lo_dyn", "nes_prg_read_hi_dyn", "nes_mapper2_conflict_read_hl",
}
BANK_JUMPS = {"nes_jump_known_hl_a_8bit", "nes_jump_known_hl_a"}
NEUTRAL = {"ld", "ldh", "and", "or", "xor", "add", "adc", "sub", "sbc", "cp", "inc", "dec",
           "push", "pop", "cpl", "rl", "rla", "rlc", "rlca", "rr", "rra", "rrc", "rrca",
           "srl", "sla", "sra", "swap", "bit", "set", "res", "scf", "ccf", "nop", "daa",
           "di", "ei", "PROFILE_INC"}
COND = {"nz", "z", "nc", "c"}
LABEL_RE = re.compile(r"^([A-Za-z_.][A-Za-z0-9_.@]*):{1,2}$")
TRACE_OK = re.compile(r"^(ld hl, \$[0-9A-Fa-f]+|push af|pop af|call nes_profile_trace_pc)$")


def main(path: str) -> None:
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    n = len(lines)
    codes = [l.split(";", 1)[0].strip() for l in lines]
    cfg = p.with_name(p.stem + ".cfg.txt")
    rsets = return_sets(cfg)[0] if cfg.exists() else {}
    # 6502 instruction whose translation a line belongs to (last comment above it in the section)
    insn_of = [None] * n
    last = None
    for i, l in enumerate(lines):
        if l.startswith("SECTION"):
            last = None
        m = INSN_RE.search(l)
        if m:
            last = (int(m.group(1), 16), m.group(3))
        insn_of[i] = last
    rts_edges = 0
    # Where each RTS translation starts, and every label reference, so a fallback
    # that other code jumps into (a shared tail) keeps the conservative exit.
    insn_start = [None] * n
    for i in range(n):
        if insn_of[i] is not None:
            insn_start[i] = insn_start[i - 1] if i and insn_of[i - 1] == insn_of[i] and insn_start[i - 1] is not None else i
    refs = {}
    for i, c in enumerate(codes):
        for t in re.findall(r"(?:^|[\s,])([A-Za-z_.][A-Za-z0-9_.@]*)$", c) if c.split(" ", 1)[0] in ("jp", "jr", "call") else []:
            refs.setdefault(t, []).append(i)

    def private_tail(i):
        lo = insn_start[i]
        for k in range(lo, i + 1):
            m = LABEL_RE.match(codes[k])
            if m:
                nm = m.group(1)
                for r in refs.get(nm, []) + (refs.get("." + nm.split(".")[-1], []) if "." in nm[1:] else []):
                    if not (lo <= r <= i) and not (insn_of[r] == insn_of[i] and insn_start[r] == lo):
                        return False
        return True

    # Section classification and IF handling.
    in_code = [False] * n
    ignore = [False] * n      # IF 0 bodies and simple trace blocks: no effect
    barrier = [False] * n     # treated as "reads everything, no fallthrough info"
    code_sec = False
    banked_sec = False
    banked = [False] * n      # banked-view sections: no CFG-dump RTS return sets
    i = 0
    while i < n:
        c = codes[i]
        if c.startswith("SECTION"):
            code_sec = ('"NES block ' in c) or ('"NES canonical superblock entry' in c) or ('"NES mapper' in c)
            banked_sec = '"NES mapper' in c
            in_code[i] = False
            i += 1
            continue
        in_code[i] = code_sec
        banked[i] = banked_sec
        if c.startswith("IF "):
            depth, j, has_else = 0, i, False
            while j < n:
                cj = codes[j]
                if cj.startswith("IF "):
                    depth += 1
                elif cj == "ENDC":
                    depth -= 1
                    if depth == 0:
                        break
                elif cj == "ELSE" and depth == 1:
                    has_else = True
                j += 1
            body = [codes[k] for k in range(i + 1, j) if codes[k]]
            simple = c == "IF 0" or (c.startswith("IF DEF(") and not has_else and
                                     all(TRACE_OK.match(b) for b in body))
            for k in range(i, j + 1):
                in_code[k] = code_sec
                ignore[k] = simple
            if not simple:
                barrier[i] = True
                for k in range(i + 1, j + 1):
                    ignore[k] = True
            i = j + 1
            continue
        i += 1

    # Labels.
    glob_at = {}
    local_at = {}
    anon = []
    scope = None
    scope_of = [None] * n
    for i, c in enumerate(codes):
        if c == ":":
            anon.append(i)
        else:
            m = LABEL_RE.match(c)
            if m:
                name = m.group(1)
                if name.startswith("."):
                    local_at[(scope, name)] = i
                else:
                    scope = name.split(".")[0]
                    if "." in name:
                        local_at[(scope, name[len(scope):])] = i
                    glob_at[name] = i
        scope_of[i] = scope

    def resolve(tgt: str, i: int):
        if tgt.startswith(":"):
            k = bisect.bisect_right(anon, i) - 1
            if tgt.startswith(":+"):
                k += len(tgt) - 1
            else:
                k -= len(tgt) - 2
                if k < 0 or anon[k] == i:
                    pass
            if 0 <= k < len(anon):
                t = anon[k]
                return t if in_code[t] else None
            return None
        if tgt.startswith("."):
            t = local_at.get((scope_of[i], tgt))
        else:
            t = glob_at.get(tgt)
        if t is None or not in_code[t]:
            return None
        return t

    succ = [()] * n
    use = [0] * n
    kill = [0] * n
    store_var = [None] * n
    for i, c in enumerate(codes):
        if not in_code[i]:
            use[i] = ALL
            continue
        if ignore[i] or not c or c == ":" or LABEL_RE.match(c):
            succ[i] = (i + 1,)
            continue
        if barrier[i]:
            use[i] = ALL
            continue
        m = STORE_RE.match(c)
        if m:
            store_var[i] = m.group(1)
            kill[i] = BIT[m.group(1)]
            succ[i] = (i + 1,)
            continue
        for v in MENTION_RE.findall(c):
            use[i] |= BIT[v]
        op, _, rest = c.partition(" ")
        rest = rest.strip()
        if op in NEUTRAL:
            succ[i] = (i + 1,)
            continue
        if op in ("jp", "jr"):
            parts = [x.strip() for x in rest.split(",")]
            cond = len(parts) == 2 and parts[0] in COND
            tgt = parts[-1]
            if tgt in BANK_JUMPS and not cond:
                t = None
                for k in range(i - 1, max(-1, i - 6), -1):
                    ck = codes[k]
                    mk = re.match(r"^ld hl, ([A-Za-z_][A-Za-z0-9_]*)$", ck)
                    if mk:
                        t = resolve(mk.group(1), i)
                        break
                    if not (ck == "" or re.match(r"^ld a, ", ck)):
                        break
                if t is None:
                    use[i] = ALL
                else:
                    succ[i] = (t,)
                continue
            t = resolve(tgt, i) if tgt != "hl" else None
            if t is None and tgt == "nes_dispatch_hl" and not cond and not banked[i] and insn_of[i] and insn_of[i][1] == "Rts":
                conts = rsets.get(insn_of[i][0])
                ts = [resolve("nes_%04X" % c, i) for c in sorted(conts)] if conts else None
                if ts and all(x is not None for x in ts) and private_tail(i):
                    succ[i] = tuple(ts)
                    rts_edges += 1
                    continue
            if t is None:
                use[i] = ALL
                succ[i] = (i + 1,) if cond else ()
            else:
                succ[i] = (t, i + 1) if cond else (t,)
            continue
        if op == "call":
            if "," not in rest and rest in SAFE_CALLS:
                succ[i] = (i + 1,)
            else:
                use[i] = ALL
                succ[i] = (i + 1,)
            continue
        # ret/reti/rst/halt/stop/data/unknown macro
        use[i] = ALL
        succ[i] = ()

    # Backward liveness: live_in = use | (live_out & ~kill)
    preds = [[] for _ in range(n + 1)]
    for i in range(n):
        for s in succ[i]:
            if s < n:
                preds[s].append(i)
    live_in = [0] * n
    live_out = [0] * n
    work = list(range(n - 1, -1, -1))
    inq = [True] * n
    while work:
        i = work.pop()
        inq[i] = False
        out = 0
        for s in succ[i]:
            out |= live_in[s] if s < n else ALL
        live_out[i] = out
        new_in = use[i] | (out & ~kill[i])
        if new_in != live_in[i]:
            live_in[i] = new_in
            for q in preds[i]:
                if not inq[q]:
                    inq[q] = True
                    work.append(q)
    remove = set()
    stats = {v: 0 for v in VARS}
    for i in range(n):
        v = store_var[i]
        if v and not (live_out[i] & BIT[v]):
            remove.add(i)
            stats[v] += 1
    p.write_text("".join(l for k, l in enumerate(lines) if k not in remove))
    print("dead-hram-global: removed " + ", ".join(f"{c} {v}" for v, c in stats.items() if c) +
          f" ({len(remove)} stores; {rts_edges} RTS fallbacks with proven return sets)")


if __name__ == "__main__":
    main(sys.argv[1])
