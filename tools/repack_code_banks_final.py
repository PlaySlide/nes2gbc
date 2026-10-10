#!/usr/bin/env python3
"""Merge sparsely filled translated-code banks and turn now-local bank jumps direct.

The Rust emitter assigns translated sections to banks with a very
conservative size estimate (64 + 96 bytes per NES instruction), and the
post-passes shrink the code further, so SMB ends up spread over ~110 ROMX
banks averaging ~1.8 KB of 16 KB. Every CFG edge between banks then pays a
`ld a,bank / ld hl,target / jp nes_jump_known_hl_a_8bit` trampoline.

This final pass (run after all pattern passes):
* computes each translated section's exact byte size for the requested
  runtime build flavour (--profile / --profile-trace add PROFILE_INC and
  IF DEF(NES2GBC_PROFILE_TRACE) bodies; the Makefile passes its PROFILE and
  PROFILE_TRACE variables, so `make gbc PROFILE=1` packs for that build),
* merges consecutive old code banks, whole-bank at a time, into new banks up
  to LIMIT bytes. Whole-bank merging keeps every block together with the
  "Hot PRG mirrors bNN" section its mirrored table reads refer to and the
  "Native leaf XXXX bank NN" sections its blocks `call` directly,
* rewrites every cross-bank transfer to use a symbolic `BANK(target)`, and
  when source and target now share a bank replaces the trampoline with
  `xor a` (the helper's host-flag normalization) + direct `jp target`.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

import os as _os
LIMIT = int(_os.environ.get("NES2GBC_REPACK_LIMIT", str(0x4000 - 0x200)), 0)
SEC_RE = re.compile(r'^(\s*SECTION\s+"(NES block [0-9A-Fa-f]{4}|NES mapper2 b[0-9A-Fa-f]{2} block [0-9A-Fa-f]{4}|NES mapper2 dispatch stub [0-9A-Fa-f]{4}|NES overlay block [0-9A-Fa-f]{4}|NES canonical superblock entry [0-9A-Fa-f]{4}|Hot PRG mirrors b\d+|Native leaf [0-9A-Fa-f]{4} bank \d+)",\s*ROMX,\s*BANK\[)(\d+)(\].*)$')
ANY_SEC = re.compile(r"^\s*SECTION\b")
LABEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):{1,2}$")
REG8 = {"a", "b", "c", "d", "e", "h", "l", "[hl]"}
IND = {"[hl]", "[bc]", "[de]", "[hli]", "[hld]", "[hl+]", "[hl-]", "[c]", "[$ff00+c]"}
ONE = {"nop", "rla", "rra", "rlca", "rrca", "cpl", "scf", "ccf", "daa", "di", "ei", "halt",
       "ret", "reti", "inc", "dec", "push", "pop", "rst"}
CB = {"rl", "rr", "rlc", "rrc", "sla", "sra", "srl", "swap", "bit", "set", "res"}
ALU = {"and", "or", "xor", "cp", "add", "adc", "sub", "sbc"}


def insn_size(c: str) -> int:
    op, _, rest = c.partition(" ")
    ops = [x.strip().lower() for x in rest.split(",")] if rest.strip() else []
    if op == "db":
        return len(ops)
    if op == "dw":
        return 2 * len(ops)
    if op == "ds":
        v = ops[0]
        return int(v[1:], 16) if v.startswith("$") else int(v)
    if op == "PROFILE_INC":
        return 26 if COUNT_PROFILE else 0
    if op in ONE:
        return 1
    if op in CB:
        return 2
    if op in ("ldh", "jr", "stop"):
        return 2
    if op == "call":
        return 3
    if op == "jp":
        return 1 if ops == ["hl"] else 3
    if op in ALU:
        src = ops[-1]
        if len(ops) == 2 and ops[0] in ("hl",):
            return 1
        if len(ops) == 2 and ops[0] == "sp":
            return 2
        return 1 if src in REG8 else 2
    if op == "ld":
        d, s = ops[0], ops[1]
        if d in ("bc", "de", "hl", "sp"):
            if d == "sp" and s == "hl":
                return 1
            if d == "hl" and s.startswith("sp"):
                return 2
            return 3
        if d.startswith("[") and d not in IND:
            return 3
        if s.startswith("[") and s not in IND:
            return 3
        if s in REG8 or s in IND:
            return 1
        return 2
    return 3


COUNT_PROFILE = False
COUNT_TRACE = False


TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_trampoline_ld(codes, i):
    return codes[i].startswith("ld hl, ") and i + 1 < len(codes) and \
        codes[i + 1] in ("jp nes_jump_known_hl_a_8bit", "jp nes_jump_known_hl_a")


# Profiling builds (tools/bench/bank_profile.py): keep every inter-section
# trampoline so the jump profile sees same-bank edges too.
KEEP_TRAMPOLINES = _os.environ.get("NES2GBC_REPACK_KEEP_TRAMPOLINES") == "1"
DEFINED = set()
BANKED = False


def _legacy_size(codes, s, e, mirror):
    size = 256 if mirror else 0
    skip = 0
    for i in range(s + 1, e):
        c = codes[i]
        if not c:
            continue
        if c.startswith("IF "):
            if skip or c == "IF 0" or not (COUNT_TRACE and c.startswith("IF DEF(")):
                skip += 1
            continue
        if c == "ENDC":
            if skip:
                skip -= 1
            continue
        if c == "ELSE" or skip:
            continue
        if LABEL_RE.match(c) or c == ":" or c.startswith("."):
            continue
        size += insn_size(c)
    return size

UNDEFINED_KNOWN = {"NES2GBC_RAM_INTERP", "NES2GBC_DEBUG_TRACE"}


def section_sizes(codes, sections):
    sizes = {}
    for idx, (s, e, b, mirror) in enumerate(sections):
        if b is None:
            continue
        if not BANKED:
            sizes[idx] = _legacy_size(codes, s, e, mirror)
            continue
        size = 256 if mirror else 0
        # Conditional nesting: stack of "counting" flags. IF DEF(X) counts its
        # body when X is defined for this build (NES2GBC_RAM_INTERP on banked
        # builds; trace/profile symbols with --profile-trace), else its ELSE.
        stack = []
        def active():
            return all(stack)
        for i in range(s + 1, e):
            c = codes[i]
            if not c:
                continue
            if c.startswith("IF "):
                if c == "IF 0":
                    on = False
                elif c.startswith("IF DEF("):
                    sym = c[7:].rstrip(")")
                    on = (sym in DEFINED) or (COUNT_TRACE and sym not in UNDEFINED_KNOWN)
                else:
                    on = False
                stack.append(on)
                continue
            if c == "ENDC":
                if stack:
                    stack.pop()
                continue
            if c == "ELSE":
                if stack:
                    stack[-1] = not stack[-1]
                continue
            if not active():
                continue
            if LABEL_RE.match(c) or c == ":" or c.startswith("."):
                continue
            size += insn_size(c)
        sizes[idx] = size
    return sizes


def main_profiled(p, lines, codes, sections, sec_of_line, old_banks):
    """Section-granular, profile-weighted packing.

    Units are the connected components of translated sections under every
    reference that needs the same bank (direct jp/jr/call, `ld hl, mirror`,
    fall-through-free shared labels, ...). Only trampolines (`ld a,BANK(x)` /
    `ld hl,x` / `jp nes_jump_known_hl_a*`) and BANK()-symbolic data may cross
    banks, exactly as in the whole-bank pass, so blocks stay with their Hot
    PRG mirrors and native leaf helpers (bdd6dbc). Units are clustered by the
    dynamic cross-bank jump counts from tools/bench/bank_profile.py (static
    trampoline sites as a small tie-breaker), then first-fit into banks.
    """
    code_idx = [i for i, sec in enumerate(sections) if sec[2] is not None]
    label_sec = {}
    for idx in code_idx:
        s, e = sections[idx][0], sections[idx][1]
        for i in range(s, e):
            m = LABEL_RE.match(codes[i])
            if m:
                label_sec[m.group(1)] = idx
    par = {i: i for i in code_idx}

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    tramp = []  # (src section, target label)
    for idx in code_idx:
        s, e = sections[idx][0], sections[idx][1]
        for i in range(s + 1, e):
            c = codes[i]
            if not c or LABEL_RE.match(c):
                continue
            if is_trampoline_ld(codes, i):
                tramp.append((idx, c[len("ld hl, "):].strip()))
                continue
            if "BANK(" in c:
                continue
            for t in TOKEN_RE.findall(c):
                ts = label_sec.get(t)
                if ts is not None and ts != idx:
                    par[find(ts)] = find(idx)
    sizes = section_sizes(codes, sections)
    unit_members = {}
    for idx in code_idx:
        unit_members.setdefault(find(idx), []).append(idx)
    usize = {u: sum(sizes[i] for i in m) for u, m in unit_members.items()}
    # Dynamic profile: "<count> <source section name> <target label>".
    name_idx = {}
    for idx in code_idx:
        m = SEC_RE.match(lines[sections[idx][0]].rstrip("\n"))
        name_idx[m.group(2)] = idx
    dyn = {}
    for raw in Path(BANK_PROFILE).read_text().splitlines():
        raw = raw.split("#", 1)[0].strip()
        if not raw:
            continue
        cnt, rest = raw.split(None, 1)
        src, tgt = rest.rsplit(None, 1)
        if src in name_idx and tgt in label_sec:
            dyn[(name_idx[src], tgt)] = dyn.get((name_idx[src], tgt), 0) + float(cnt)
    weight = {}
    for src, tgt in tramp:
        if tgt not in label_sec:
            continue
        a, b = find(src), find(label_sec[tgt])
        if a == b:
            continue
        key = (min(a, b), max(a, b))
        weight[key] = weight.get(key, 0.0) + 1.0
    used = 0
    for (src, tgt), cnt in dyn.items():
        a, b = find(src), find(label_sec[tgt])
        if a == b:
            continue
        key = (min(a, b), max(a, b))
        weight[key] = weight.get(key, 0.0) + 1000.0 * cnt
        used += 1
    # Agglomerative clustering on units.
    cluster = {u: u for u in unit_members}
    members = {u: [u] for u in unit_members}
    csize = dict(usize)
    adj = {}
    for (a, b), w in weight.items():
        adj.setdefault(a, {})[b] = adj.setdefault(a, {}).get(b, 0.0) + w
        adj.setdefault(b, {})[a] = adj.setdefault(b, {}).get(a, 0.0) + w
    import heapq
    heap = [(-w, a, b) for (a, b), w in weight.items()]
    heapq.heapify(heap)
    while heap:
        w, ca, cb = heapq.heappop(heap)
        # Stale unless both are still cluster representatives with this weight.
        if cluster[ca] != ca or cluster[cb] != cb or adj.get(ca, {}).get(cb) != -w:
            continue
        if csize[ca] + csize[cb] > LIMIT:
            continue
        # merge cb into ca
        for u in members[cb]:
            cluster[u] = ca
        members[ca] += members.pop(cb)
        csize[ca] += csize.pop(cb)
        for x, wx in adj.pop(cb, {}).items():
            adj[x].pop(cb, None)
            if x == ca:
                continue
            nw = adj[ca].get(x, 0.0) + wx
            adj[ca][x] = nw
            adj[x][ca] = nw
            heapq.heappush(heap, (-nw, min(ca, x), max(ca, x)))
        adj[ca].pop(ca, None)
    # First-fit decreasing by size (hot clusters are large after merging).
    bins = []
    for ca in sorted(members, key=lambda c: (-csize[c], c)):
        for bn in bins:
            if bn[0] + csize[ca] <= LIMIT:
                bn[0] += csize[ca]
                bn[1].append(ca)
                break
        else:
            bins.append([csize[ca], [ca]])
    sec_bank = {}
    for k, (_, cas) in enumerate(bins):
        for ca in cas:
            for u in members[ca]:
                for idx in unit_members[u]:
                    sec_bank[idx] = old_banks[0] + k
    print(f"repack-banks: profile {BANK_PROFILE}: {len(dyn)} edges ({used} cross-unit), "
          f"{len(unit_members)} units")
    return finish(p, lines, codes, sections, sec_of_line, None, old_banks, sec_bank)


def main(path: str) -> None:
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    codes = [l.split(";", 1)[0].strip() for l in lines]
    n = len(lines)
    global BANKED
    if any(l.startswith("; Generated by nes2gbc mapper-2 banked emitter") for l in lines):
        BANKED = True
        DEFINED.add("NES2GBC_RAM_INTERP")
        UNDEFINED_KNOWN.discard("NES2GBC_RAM_INTERP")
    # Section ranges.
    sec_starts = [i for i, c in enumerate(codes) if ANY_SEC.match(c)]
    sec_of_line = [None] * n
    sections = []  # (start, end, old_bank or None, is_mirror)
    for k, s in enumerate(sec_starts):
        e = sec_starts[k + 1] if k + 1 < len(sec_starts) else n
        m = SEC_RE.match(lines[s].rstrip("\n"))
        sections.append([s, e, int(m.group(3)) if m else None, bool(m and "Hot PRG" in m.group(2))])
        for i in range(s, e):
            sec_of_line[i] = len(sections) - 1
    bank_size = {}
    for s, e, b, mirror in sections:
        if b is None:
            continue
        size = 256 if mirror else 0
        depth0 = 0
        skip = 0
        for i in range(s + 1, e):
            c = codes[i]
            if not c:
                continue
            if c.startswith("IF "):
                if skip or c == "IF 0" or not (COUNT_TRACE and c.startswith("IF DEF(")):
                    skip += 1
                continue
            if c == "ENDC":
                if skip:
                    skip -= 1
                continue
            if c == "ELSE" or skip:
                continue
            if LABEL_RE.match(c) or c == ":" or c.startswith("."):
                continue
            size += insn_size(c)
        bank_size[b] = bank_size.get(b, 0) + size
    old_banks = sorted(bank_size)
    # Static cross-bank edge weights between old banks.
    old_label_bank = {}
    for s, e, b, mirror in sections:
        if b is None:
            continue
        for i in range(s, e):
            m = LABEL_RE.match(codes[i])
            if m:
                old_label_bank[m.group(1)] = b
    weight = {}
    for i, c in enumerate(codes):
        if c in ("jp nes_jump_known_hl_a_8bit", "jp nes_jump_known_hl_a"):
            mh = re.match(r"^ld hl, ([A-Za-z_][A-Za-z0-9_]*)$", codes[i - 1])
            sidx = sec_of_line[i]
            if mh and sidx is not None and sections[sidx][2] is not None and mh.group(1) in old_label_bank:
                a, b = sections[sidx][2], old_label_bank[mh.group(1)]
                if a != b:
                    key = (min(a, b), max(a, b))
                    weight[key] = weight.get(key, 0) + 1
    if IDENTITY:
        # Profiling layout (tools/bench/bank_profile.py): no merging, so the
        # current bank at every nes_jump_known_hl_a_8bit identifies the
        # source's old bank exactly.
        return finish(p, lines, codes, sections, sec_of_line, {b: b for b in old_banks}, old_banks)
    if BANK_PROFILE:
        return main_profiled(p, lines, codes, sections, sec_of_line, old_banks)
    # Agglomerative clustering: merge the heaviest-connected pair that fits.
    cluster = {b: b for b in old_banks}
    members = {b: [b] for b in old_banks}
    csize = dict(bank_size)
    while True:
        cw = {}
        for (a, b), w in weight.items():
            ca, cb = cluster[a], cluster[b]
            if ca != cb:
                k = (min(ca, cb), max(ca, cb))
                cw[k] = cw.get(k, 0) + w
        best = None
        for (ca, cb), w in sorted(cw.items(), key=lambda kv: (-kv[1], kv[0])):
            if csize[ca] + csize[cb] <= LIMIT:
                best = (ca, cb)
                break
        if best is None:
            break
        ca, cb = best
        for b in members[cb]:
            cluster[b] = ca
        members[ca] += members.pop(cb)
        csize[ca] += csize.pop(cb)
    # First-fit the resulting clusters (in address order) into banks.
    bins = []
    for ca in sorted(members):
        for bn in bins:
            if bn[0] + csize[ca] <= LIMIT:
                bn[0] += csize[ca]
                bn[1].append(ca)
                break
        else:
            bins.append([csize[ca], [ca]])
    mapping = {}
    for k, (_, cas) in enumerate(bins):
        for ca in cas:
            for b in members[ca]:
                mapping[b] = old_banks[0] + k
    return finish(p, lines, codes, sections, sec_of_line, mapping, old_banks)


def finish(p, lines, codes, sections, sec_of_line, mapping, old_banks, sec_bank=None):
    """Apply a bank assignment. mapping: old bank -> new bank (whole-bank
    moves); sec_bank, when given, overrides per section index."""
    if sec_bank is None:
        sec_bank = {idx: mapping[sec[2]] for idx, sec in enumerate(sections) if sec[2] is not None}
    # Label -> new bank.
    label_bank = {}
    for idx, (s, e, b, mirror) in enumerate(sections):
        if b is None:
            continue
        for i in range(s, e):
            m = LABEL_RE.match(codes[i])
            if m:
                label_bank[m.group(1)] = sec_bank[idx]
    out = list(lines)
    for idx, (s, e, b, mirror) in enumerate(sections):
        if b is not None:
            m = SEC_RE.match(lines[s].rstrip("\n"))
            out[s] = f"{m.group(1)}{sec_bank[idx]}{m.group(4)}\n"
    direct = sym = 0
    for i, c in enumerate(codes):
        if c not in ("jp nes_jump_known_hl_a_8bit", "jp nes_jump_known_hl_a"):
            continue
        mh = re.match(r"^ld hl, ([A-Za-z_][A-Za-z0-9_]*)$", codes[i - 1])
        ma = re.match(r"^ld a, (\$[0-9A-Fa-f]{2}|BANK\(([A-Za-z_][A-Za-z0-9_]*)\))$", codes[i - 2])
        if not (mh and ma):
            continue
        tgt = mh.group(1)
        if ma.group(2) and ma.group(2) != tgt:
            continue
        sidx = sec_of_line[i]
        src_old = sections[sidx][2] if sidx is not None else None
        if tgt in label_bank and src_old is not None and not sections[sidx][3] and \
                sec_bank[sidx] == label_bank[tgt] and not KEEP_TRAMPOLINES:
            # Block entries never assume A/flags (dispatch_hl enters them with
            # arbitrary values), so the helper's A=0 normalization is dropped.
            out[i - 2] = ""
            out[i - 1] = ""
            out[i] = f"    jp {tgt} ; repacked same-bank direct transfer\n"
            direct += 1
        else:
            out[i - 2] = f"    ld a, BANK({tgt})\n"
            sym += 1
    p.write_text("".join(out))
    print(f"repack-banks: {len(old_banks)} code banks -> {len(set(sec_bank.values()))} "
          f"(limit {LIMIT} est. bytes); {direct} bank jumps made direct, {sym} kept (symbolic BANK)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("asm")
    ap.add_argument("--profile", default="0")
    ap.add_argument("--profile-trace", default="0")
    ap.add_argument("--identity", default="0", help="1 = keep every old bank (bank profiling build)")
    ap.add_argument("--bank-profile", default="", help="cross-bank jump profile for section-level packing")
    a = ap.parse_args()
    IDENTITY = a.identity == "1"
    BANK_PROFILE = a.bank_profile if a.bank_profile and Path(a.bank_profile).is_file() else ""
    COUNT_TRACE = a.profile_trace == "1"
    COUNT_PROFILE = COUNT_TRACE or a.profile == "1"
    main(a.asm)
