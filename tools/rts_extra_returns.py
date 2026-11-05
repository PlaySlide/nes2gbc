#!/usr/bin/env python3
"""Merge CRC-keyed extra return PCs into `; m2 RTS table` markers (opt-out NES2GBC_NO_RTS_EXTRA=1)."""
import os, re, sys

DB = os.path.join(os.path.dirname(__file__), "..", "data", "rts_extra_returns.txt")
MARK = re.compile(r"^(\s*; m2 RTS table )(.*)$")
LBL = re.compile(r"^(nes_([0-9A-Fa-f]{4})):")


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
        rts = int(parts[1], 16)
        rets = [int(x, 16) for x in parts[2:]]
        out.setdefault(crc, {}).setdefault(rts, set()).update(rets)
    return out


def payload_crc_from_asm(path):
    # generated.asm doesn't carry CRC; sibling cfg or env
    env = os.environ.get("NES2GBC_PAYLOAD_CRC")
    if env:
        return int(env, 16)
    for cand in (
        path.replace("generated.asm", "generated.cfg.txt"),
        os.path.join(os.path.dirname(path), "generated.cfg.txt"),
    ):
        if os.path.isfile(cand):
            for line in open(cand):
                if "crc" in line.lower() or "CRC" in line:
                    m = re.search(r"([0-9A-Fa-f]{8})", line)
                    if m:
                        return int(m.group(1), 16)
    return None


def main(path):
    if os.environ.get("NES2GBC_NO_RTS_EXTRA", "0") == "1":
        return
    db = load_db()
    if not db:
        return
    crc = payload_crc_from_asm(path)
    # Also try reading from a stamp file written by generate
    stamp = os.path.join(os.path.dirname(path), "generated.crc.txt")
    if crc is None and os.path.isfile(stamp):
        crc = int(open(stamp).read().strip(), 16)
    if crc is None or crc not in db:
        # Fall back: apply all entries keyed by nearby RTS labels (unique sets)
        # Only when a single ROM's markers match — skip if ambiguous.
        return
    extras = db[crc]
    lines = open(path).read().split("\n")
    # Map line index -> nearest preceding nes_XXXX label PC
    last_pc = None
    label_at = {}
    for i, l in enumerate(lines):
        m = LBL.match(l)
        if m:
            last_pc = int(m.group(2), 16)
            label_at[i] = last_pc
    changed = 0
    for i, l in enumerate(lines):
        m = MARK.match(l)
        if not m:
            continue
        # find nearest label above
        pc = None
        for j in range(i, -1, -1):
            if j in label_at:
                pc = label_at[j]
                break
        if pc is None:
            continue
        # Match RTS pc or block start (EF8A for RTS EF8B)
        add = extras.get(pc) or extras.get(pc + 1)  # block start vs RTS addr
        if not add:
            # also try any listed RTS whose block contains this mark's existing set
            continue
        have = {int(x[1:], 16) for x in m.group(2).split() if x.startswith("$")}
        # Only keep returns that already have a translated label in this asm.
        labels = {int(LBL.match(l).group(2), 16) for l in lines if LBL.match(l)}
        add = {p for p in add if p in labels}
        new = sorted(have | add)
        if len(new) == len(have):
            continue
        lines[i] = m.group(1) + " ".join(f"${p:04X}" for p in new)
        changed += 1
    if changed:
        open(path, "w").write("\n".join(lines))
        print(f"rts_extra_returns: {changed} table(s) extended (crc={crc:08X})")


if __name__ == "__main__":
    main(sys.argv[1])
