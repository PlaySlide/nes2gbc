#!/usr/bin/env python3
"""Final local dead-store elimination for canonical 6502 state bytes in HRAM.

Runs last, on host-level assembly, after every pattern-matching pass. A store
`ldh [S], a` (S in nes_a/x/y and the lazy Z/N/C shadows) is deleted when, on
straight-line forward execution, S is stored again before any possible read:

* any other mention of S is a read (keep);
* conditional/unconditional jumps are only followed when they target an
  anonymous forward label (`:+`, `:++`...) that lies before the killing store;
* calls are only allowed to whitelisted runtime helpers that never read
  these bytes and always return (NMI poll, BRK, RTI, dispatch are barriers);
* labels are transparent (they only add incoming paths);
* anything else (ret, jp hl, data, section/conditional-assembly directives
  other than `IF 0` blocks, unknown macros) is a barrier.

No runtime helper or ISR reads nes_a/x/y or the Z/N/C shadows except
nes_materialize_p (via nes_poll_nmi_hl / nes_brk_hl) and the ADC/rotate
helpers, none of which are whitelisted.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

STATE = ("nes_a", "nes_x", "nes_y", "nes_z_shadow", "nes_n_shadow", "nes_c_shadow")
STORE_RE = re.compile(r"^\s*ldh \[(nes_(?:a|x|y|z_shadow|n_shadow|c_shadow))\], a\s*(;.*)?$")
SAFE_CALLS = {
    "nes_cpu_read", "nes_cpu_write", "nes_ppu_write_data", "nes_ppu_read_data",
    "nes_controller_write", "nes_profile_trace_pc",
    "nes_ppu_cpu_write.addr", "nes_ppu_cpu_write.ctrl", "nes_ppu_cpu_write.scroll",
    "nes_ppu_cpu_write.mask", "nes_ppu_cpu_write.oamaddr",
}
NEUTRAL = {"ld", "ldh", "and", "or", "xor", "add", "adc", "sub", "sbc", "cp", "inc", "dec",
           "push", "pop", "cpl", "rl", "rla", "rlc", "rlca", "rr", "rra", "rrc", "rrca",
           "srl", "sla", "sra", "swap", "bit", "set", "res", "scf", "ccf", "nop", "daa",
           "PROFILE_INC"}
ANON_TARGET = re.compile(r"^(?:jr|jp)\s+(?:(?:nz|z|nc|c)\s*,\s*)?:(\++)$")
LABEL_RE = re.compile(r"^(?:[A-Za-z_.][A-Za-z0-9_.@]*:{1,2}|:)$")
WINDOW = 120


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def main(path: str) -> None:
    p = Path(path)
    lines = p.read_text().splitlines(keepends=True)
    codes = [code(l) for l in lines]
    n = len(lines)
    # anonymous label positions, for resolving :+ targets
    anon = [i for i, c in enumerate(codes) if c == ":"]
    import bisect
    # IF 0 ... ENDC ranges to skip
    skip = [False] * n
    i = 0
    while i < n:
        if codes[i] == "IF 0":
            depth = 0
            j = i
            while j < n:
                c = codes[j]
                if c.startswith("IF "):
                    depth += 1
                elif c == "ENDC":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            for k in range(i, j + 1):
                skip[k] = True
            i = j + 1
        else:
            i += 1
    remove = set()
    stats = {s: 0 for s in STATE}
    for i in range(n):
        if skip[i]:
            continue
        m = STORE_RE.match(lines[i])
        if not m:
            continue
        s = m.group(1)
        word = re.compile(r"\b" + re.escape(s) + r"\b")
        pending = -1  # furthest anon target index that must precede the killer
        depth = 0
        j = i + 1
        dead = False
        while j < n and j - i < WINDOW:
            if skip[j] or not codes[j]:
                j += 1
                continue
            c = codes[j]
            m2 = STORE_RE.match(lines[j])
            if m2 and m2.group(1) == s and j not in remove:
                dead = j > pending
                break
            if word.search(c):
                break
            if LABEL_RE.match(c):
                j += 1
                continue
            op = c.split()[0]
            if op == "IF":
                if not c.startswith("IF DEF("):
                    break
                depth += 1
                j += 1
                continue
            if op == "ENDC":
                if depth == 0:
                    break
                depth -= 1
                j += 1
                continue
            if op in NEUTRAL:
                j += 1
                continue
            if op in ("jr", "jp"):
                ma = ANON_TARGET.match(c)
                if not ma:
                    break
                k = bisect.bisect_right(anon, j) - 1 + len(ma.group(1))
                if k >= len(anon):
                    break
                pending = max(pending, anon[k])
                j += 1
                continue
            if op == "call":
                tgt = c.split(None, 1)[1].strip()
                if "," in tgt or tgt not in SAFE_CALLS:
                    break
                j += 1
                continue
            break
        if dead:
            remove.add(i)
            stats[s] += 1
    out = [l for k, l in enumerate(lines) if k not in remove]
    p.write_text("".join(out))
    print("dead-hram-state: removed " + ", ".join(f"{v} {k}" for k, v in stats.items() if v) +
          f" ({len(remove)} stores)")


if __name__ == "__main__":
    main(sys.argv[1])
