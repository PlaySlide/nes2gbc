#!/usr/bin/env python3
"""MMC1: fuse the canonical 5-write serial register load into one native call.

MMC1 games load a register with

    STA reg / LSR A / STA reg / LSR A / STA reg / LSR A / STA reg / LSR A / STA reg

(Zelda's bank switch routine runs this ~10 times per frame). Translated, that
is five nes_cpu_write calls (address decode + mapper dispatch each) and four
LSR translations. When one straight-line host region holds exactly that 6502
sequence to one constant register address >= $8000 (checked through the
translator's `; $PC: $op Mnemonic Mode` comments; no labels, jumps or other
calls inside), the five writes become `call nes_mmc1_serial_write5`, which
performs the same five serial writes in order through nes_cpu_write's mapper-1
path and returns the final 6502 A (value >> 4) and carry (bit 3). The region's
6502 end state is then published exactly as an LSR leaves it: nes_a, Z/N
shadows = value >> 4, C shadow = bit 3 (0/1), E = last written value.

Only for mapper 1 builds (NES2GBC_RAM_INTERP helper); others are untouched.
"""
import re, sys

INS = re.compile(r";\s*\$([0-9A-F]{4}): \$([0-9A-F]{2}) ([A-Z][a-z]{2}) (\w+)")
SAFE_OPS = {"ld", "ldh", "srl", "rl", "rla", "push", "pop", "and", "or", "xor"}


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    text = open(path).read()
    m = re.search(r"nes_generated_init:\n\s+ld a, \$([0-9A-F]{2})\n\s+ld \[nes_mapper\], a", text)
    if not m or int(m.group(1), 16) != 1:
        print("mmc1-serial-fuse: not MMC1, skipped")
        return
    L = text.splitlines(keepends=True)
    C = [code(l) for l in L]
    n = len(L)

    def store_addr(i):
        """call at i preceded by `ld hl, $XXXX` / `pop af`: return XXXX."""
        j = i - 1
        while j >= 0 and not C[j]:
            j -= 1
        if C[j] != "pop af":
            return None
        k = j - 1
        while k >= 0 and not C[k]:
            k -= 1
        mm = re.fullmatch(r"ld hl, \$([0-9A-F]{4})", C[k])
        return int(mm.group(1), 16) if mm else None

    calls = [i for i, c in enumerate(C) if c == "call nes_cpu_write"]
    fused = 0
    edits = {}
    used = set()
    for idx, first in enumerate(calls):
        if first in used or idx + 4 >= len(calls):
            continue
        addr = store_addr(first)
        if addr is None or addr < 0x8000:
            continue
        group = calls[idx:idx + 5]
        ok = all(store_addr(c) == addr for c in group)
        # Each gap must be: LSR A then STA abs, straight-line, safe ops only.
        for a, b in zip(group, group[1:]):
            if not ok:
                break
            ins = []
            for q in range(a + 1, b):
                mi = INS.search(L[q])
                if mi:
                    ins.append((mi.group(3), mi.group(4)))
                c = C[q]
                if not c:
                    continue
                if c.endswith(":") or c.startswith(("IF", "ENDC", "ELSE", "PROFILE")):
                    ok = False
                    break
                if c.split(" ", 1)[0] not in SAFE_OPS:
                    ok = False
                    break
            # The emitter may print the next instruction's comment (an RTS
            # whose code follows the call) ahead of the last store.
            last_gap = b == group[-1]
            if ins != [("Lsr", "Accumulator"), ("Sta", "Absolute")] and not (
                    last_gap and ins == [("Lsr", "Accumulator"), ("Sta", "Absolute"), ("Rts", "Implied")]):
                ok = False
        # The first call must belong to an STA abs.
        if ok:
            q = first
            while q >= 0 and not INS.search(L[q]):
                q -= 1
            mi = INS.search(L[q])
            ok = bool(mi) and (mi.group(3), mi.group(4)) == ("Sta", "Absolute")
        if not ok:
            continue
        ind = "    "
        edits[first] = [
            f"{ind}call nes_mmc1_serial_write5 ; fused STA ${addr:04X} / 4x (LSR A / STA ${addr:04X})\n",
            f"{ind}ld e, a ; last written value, as nes_cpu_write leaves it\n",
            f"{ind}ld a, $00\n",
            f"{ind}rl a ; carry of the last LSR (value bit 3)\n",
            f"{ind}ldh [nes_c_shadow], a\n",
            f"{ind}ld a, e\n",
            f"{ind}ldh [nes_a], a\n",
            f"{ind}ldh [nes_z_shadow], a\n",
            f"{ind}ldh [nes_n_shadow], a\n",
        ]
        for q in range(first + 1, group[-1] + 1):
            if C[q] and not INS.search(L[q]):
                edits[q] = []
            elif INS.search(L[q]):
                edits[q] = [L[q]]  # keep the 6502 comments
        used.update(group)
        fused += 1
    out = []
    for i, l in enumerate(L):
        if i in edits:
            out.extend(edits[i])
        else:
            out.append(l)
    open(path, "w").write("".join(out))
    print(f"mmc1-serial-fuse: fused {fused} five-write serial load(s)")


if __name__ == "__main__":
    main(sys.argv[1])
