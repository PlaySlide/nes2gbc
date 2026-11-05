#!/usr/bin/env python3
"""Merge CRC-keyed extra return PCs into RTS tables/guards (opt-out NES2GBC_NO_RTS_EXTRA=1).

1) Extends `; m2 RTS table ...` markers (hashed tables).
2) For bare/guard RTS ending in `jp nes_dispatch_hl`, injects compare guards for
   extra returns when the RTS PC matches (label above or `*` wildcard).
"""
import os, re, sys

DB = os.path.join(os.path.dirname(__file__), "..", "data", "rts_extra_returns.txt")
MARK = re.compile(r"^(\s*; m2 RTS table )(.*)$")
LBL = re.compile(r"^(nes_([0-9A-Fa-f]{4})):")
M2LBL = re.compile(r"^nes_m2_b[0-9A-Fa-f]{2}_([0-9A-Fa-f]{4}):")
CODE = lambda l: l.split(";", 1)[0].strip()


def load_db():
    out = {}
    if not os.path.isfile(DB):
        return out
    for line in open(DB):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        crc = int(parts[0], 16)
        rts = None if parts[1] == "*" else int(parts[1], 16)
        rets = [int(x, 16) for x in parts[2:]]
        out.setdefault(crc, {}).setdefault(rts, set()).update(rets)
    return out


def payload_crc_from_asm(path):
    env = os.environ.get("NES2GBC_PAYLOAD_CRC")
    if env:
        return int(env, 16)
    for cand in (
        path.replace("generated.asm", "generated.cfg.txt"),
        os.path.join(os.path.dirname(path), "generated.cfg.txt"),
        os.path.join(os.path.dirname(path), "generated.crc.txt"),
    ):
        if os.path.isfile(cand):
            for line in open(cand):
                m = re.search(r"([0-9A-Fa-f]{8})", line)
                if m:
                    return int(m.group(1), 16)
    stamp = os.path.join(os.path.dirname(path), "generated.crc.txt")
    if os.path.isfile(stamp):
        return int(open(stamp).read().strip(), 16)
    return None


def guard_snip(pc):
    """Raw PC-1 compare then jump to nes_XXXX (same shape as emitter guards)."""
    raw = (pc - 1) & 0xFFFF
    tgt = f"nes_{pc:04X}"
    return [
        f"    ld a, c ; m2 RTS guard ${pc:04X} (rts_extra_returns)",
        f"    cp ${raw & 0xFF:02X} ; raw PC-1 (rts_raw_guard_compare.py)",
        "    jr nz, :+",
        "    ld a, h",
        f"    cp ${raw >> 8:02X}",
        "    jr nz, :+",
        f"    ld a, BANK({tgt})",
        f"    ld hl, {tgt}",
        f"    jp nes_jump_known_hl_a_8bit ; 8-bit translated-code bank switch",
        ":",
    ]


def main(path):
    if os.environ.get("NES2GBC_NO_RTS_EXTRA", "0") == "1":
        return
    db = load_db()
    if not db:
        return
    crc = payload_crc_from_asm(path)
    stamp = os.path.join(os.path.dirname(path), "generated.crc.txt")
    if crc is None and os.path.isfile(stamp):
        crc = int(open(stamp).read().strip(), 16)
    if crc is None or crc not in db:
        return
    extras = db[crc]
    lines = open(path).read().split("\n")
    labels = {}
    last_pc = None
    label_at = {}
    for i, l in enumerate(lines):
        m = LBL.match(l)
        if m:
            pc = int(m.group(2), 16)
        else:
            m = M2LBL.match(l)
            if m:
                pc = int(m.group(1), 16)
            else:
                if l.startswith("SECTION"):
                    last_pc = None
                continue
        last_pc = pc
        label_at[i] = pc
        labels[pc] = True

    # --- 1) extend hashed table markers ---
    changed_tables = 0
    for i, l in enumerate(lines):
        m = MARK.match(l)
        if not m:
            continue
        pc = None
        for j in range(i, -1, -1):
            if j in label_at:
                pc = label_at[j]
                break
        if pc is None:
            continue
        add = set(extras.get(pc) or extras.get(pc + 1) or ())
        add |= set(extras.get(None) or ())
        add = {p for p in add if p in labels}
        if not add:
            continue
        have = {int(x[1:], 16) for x in m.group(2).split() if x.startswith("$")}
        new = sorted(have | add)
        if len(new) == len(have):
            continue
        lines[i] = m.group(1) + " ".join(f"${p:04X}" for p in new)
        changed_tables += 1

    # --- 2) inject guards on RTS -> jp nes_dispatch_hl ---
    # Find RTS pop blocks: PROFILE_INC nes_profile_rts_pop ... jp nes_dispatch_hl
    injected = 0
    out = []
    i = 0
    while i < len(lines):
        l = lines[i]
        out.append(l)
        if "nes_profile_rts_pop" in l:
            # look ahead for jp nes_dispatch_hl before next SECTION/label-ish
            j = i + 1
            jp_at = None
            while j < len(lines) and j < i + 80:
                if lines[j].startswith("SECTION") or (
                    LBL.match(lines[j]) or M2LBL.match(lines[j])
                ):
                    break
                if CODE(lines[j]) == "jp nes_dispatch_hl":
                    jp_at = j
                    break
                j += 1
            if jp_at is not None:
                # RTS PC = nearest label above the profile line
                pc = None
                for k in range(i, -1, -1):
                    if k in label_at:
                        pc = label_at[k]
                        break
                add = set()
                if pc is not None:
                    add |= set(extras.get(pc) or extras.get(pc + 1) or ())
                add |= set(extras.get(None) or ())
                add = {p for p in add if p in labels}
                window = "\n".join(lines[i:jp_at])
                add = {p for p in add if f"${p:04X}" not in window and f"nes_{p:04X}" not in window}
                if add:
                    # Insert before `ld l, c` fallback (guards use raw C/H), else before jp.
                    ins_at = jp_at
                    for k in range(i + 1, jp_at + 1):
                        if CODE(lines[k]) == "ld l, c":
                            ins_at = k
                            break
                    for k in range(i + 1, ins_at):
                        out.append(lines[k])
                    for p in sorted(add):
                        out.extend(guard_snip(p))
                        injected += 1
                    for k in range(ins_at, jp_at + 1):
                        out.append(lines[k])
                    i = jp_at + 1
                    continue
        i += 1

    if changed_tables or injected:
        open(path, "w").write("\n".join(out))
        print(
            f"rts_extra_returns: {changed_tables} table(s) extended, "
            f"{injected} guard(s) injected (crc={crc:08X})"
        )


if __name__ == "__main__":
    main(sys.argv[1])
