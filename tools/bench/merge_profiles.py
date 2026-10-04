#!/usr/bin/env python3
"""Average rate profiles ("<per-frame rate> <key...>" lines, '#' comments).

    tools/bench/merge_profiles.py OUT IN1[:W1] IN2[:W2] ...

Writes OUT with sum(w_i * rate_i) / sum(w_i) per key, sorted by rate. Used to
combine the std and harsh scenario runs of rts_profile.py, rts_edge_profile.py
and bank_profile.py so code layout and RTS guard order reflect both calm play
and enemy-heavy scrolling.
"""
import collections, sys

out, ins = sys.argv[1], sys.argv[2:]
acc = collections.defaultdict(float)
wt = 0.0
heads = []
for spec in ins:
    path, _, w = spec.partition(":")
    w = float(w or 1)
    wt += w
    for line in open(path):
        if line.startswith("#"):
            heads.append(line.strip("# \n"))
            continue
        f = line.split(None, 1)
        if len(f) == 2:
            acc[f[1].rstrip("\n")] += w * float(f[0])
with open(out, "w") as fh:
    fh.write("# merged (weights " + ", ".join(ins) + "): " + " | ".join(heads) + "\n")
    for k, v in sorted(acc.items(), key=lambda kv: (-kv[1], kv[0])):
        if v / wt >= 0.001:
            fh.write(f"{v / wt:.3f} {k}\n")
print(f"merge-profiles: {len(acc)} keys -> {out}")
