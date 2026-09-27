#!/usr/bin/env python3
"""Drop 6502 V-flag computation when the program can never observe V.

V is only observable through BVC/BVS or by reading a PHP-pushed byte; CLV
and PLP only write it. If there is no BVC/BVS and every PHP is a local
save/restore closed by a PLP, the per-ADC/SBC overflow sequences are dead:

    ldh a,[nes_p] / and $BF / (ldh [nes_p],a | ld r,a) / <V math> /
    and $80 / jr z,:+ / <set $40> / : [/ ld a,r / ldh [nes_p],a]

Each is replaced by its bare anonymous label (anon-label targets stay the
same). It only applies when the next instruction writes A from another
source, so the pre-sequence A is never observed. The runtime's NMI push
of P still happens; its V bit is just stale, which nothing reads.
"""
import re, sys
from pathlib import Path

MATH = {"ld a, d", "xor e", "cpl", "ld h, a", "xor l", "xor c", "xor b", "and h"}
INS = re.compile(r";\s*\$([0-9A-F]{4}): \$[0-9A-F]{2} ([A-Z][a-z]{2})\b")
STACKPEEK = {"Pla", "Tsx", "Txs", "Rts", "Rti", "Plp"}


def v_observable(text):
    ins = {}
    for m in INS.finditer(text):
        ins.setdefault(int(m.group(1), 16), m.group(2))
    if any(v in ("Bvs", "Bvc") for v in ins.values()):
        return True
    # PHP is harmless when it is a local save/restore: a PLP follows within
    # 48 bytes with nothing that could read the pushed byte in between.
    for a, v in ins.items():
        if v != "Php":
            continue
        ok = False
        for b in range(a + 1, a + 49):
            w = ins.get(b)
            if w == "Plp":
                ok = True
                break
            if w in STACKPEEK:
                break
        if not ok:
            return True
    return False


def code(l):
    return l.split(";", 1)[0].strip()


def main(path):
    p = Path(path)
    text = p.read_text()
    if v_observable(text):
        print("dead-overflow: V observable (BVC/BVS or unpaired PHP); skipped")
        return
    L = text.splitlines(keepends=True)
    idx = [i for i, l in enumerate(L) if code(l)]
    pos = {i: k for k, i in enumerate(idx)}
    C = [code(L[i]) for i in idx]
    out_rm = set()
    n = 0
    k = 0
    while k < len(C) - 20:
        if C[k] != "ldh a, [nes_p]" or C[k + 1] != "and $BF":
            k += 1
            continue
        j = k + 2
        if C[j] == "ldh [nes_p], a":
            reg = None
        elif re.fullmatch(r"ld [bc], a", C[j]):
            reg = C[j][3]
        else:
            k += 1; continue
        j += 1
        while C[j] in MATH:
            j += 1
        if C[j] != "and $80" or C[j + 1] != "jr z, :+":
            k += 1; continue
        j += 2
        if reg is None:
            tail = ["ldh a, [nes_p]", "or $40", "ldh [nes_p], a", ":"]
        else:
            tail = [f"ld a, {reg}", "or $40", f"ld {reg}, a", ":", f"ld a, {reg}", "ldh [nes_p], a"]
        if C[j:j + len(tail)] != tail:
            k += 1; continue
        end = j + len(tail)  # first instruction after the sequence
        nxt = C[end]
        m = re.fullmatch(r"(ld|ldh) a, (.+)", nxt)
        if not m or m.group(2) == "a":
            k += 1; continue
        # keep the anonymous label line, drop everything else in [k, end)
        lab = j + 3
        for q in range(k, end):
            if q != lab:
                out_rm.add(idx[q])
        n += 1
        k = end
    for i in out_rm:
        L[i] = ""
    p.write_text("".join(L))
    print(f"dead-overflow: {n} V sequences removed")


if __name__ == "__main__":
    main(sys.argv[1])
