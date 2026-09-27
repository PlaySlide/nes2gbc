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
  "Hot PRG mirrors bNN" section its mirrored table reads refer to,
* rewrites every cross-bank transfer to use a symbolic `BANK(target)`, and
  when source and target now share a bank replaces the trampoline with
  `xor a` (the helper's host-flag normalization) + direct `jp target`.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

LIMIT = 0x4000 - 0x200
SEC_RE = re.compile(r'^(\s*SECTION\s+"(NES block [0-9A-Fa-f]{4}|NES canonical superblock entry [0-9A-Fa-f]{4}|Hot PRG mirrors b\d+)",\s*ROMX,\s*BANK\[)(\d+)(\].*)$')
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


def main(path: str) -> None:
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    codes = [l.split(";", 1)[0].strip() for l in lines]
    n = len(lines)
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
    # Label -> new bank.
    label_bank = {}
    for idx, (s, e, b, mirror) in enumerate(sections):
        if b is None:
            continue
        for i in range(s, e):
            m = LABEL_RE.match(codes[i])
            if m:
                label_bank[m.group(1)] = mapping[b]
    out = list(lines)
    for s, e, b, mirror in sections:
        if b is not None:
            m = SEC_RE.match(lines[s].rstrip("\n"))
            out[s] = f"{m.group(1)}{mapping[b]}{m.group(4)}\n"
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
                mapping[src_old] == label_bank[tgt]:
            out[i - 2] = "    xor a ; same-bank after repack: keep helper flag normalization\n"
            out[i - 1] = ""
            out[i] = f"    jp {tgt} ; repacked same-bank direct transfer\n"
            direct += 1
        else:
            out[i - 2] = f"    ld a, BANK({tgt})\n"
            sym += 1
    p.write_text("".join(out))
    print(f"repack-banks: {len(old_banks)} code banks -> {len(set(mapping.values()))} "
          f"(limit {LIMIT} est. bytes); {direct} bank jumps made direct, {sym} kept (symbolic BANK)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("asm")
    ap.add_argument("--profile", default="0")
    ap.add_argument("--profile-trace", default="0")
    a = ap.parse_args()
    COUNT_TRACE = a.profile_trace == "1"
    COUNT_PROFILE = COUNT_TRACE or a.profile == "1"
    main(a.asm)
