#!/usr/bin/env python3
"""Record per-RTS-site return-edge counts used to order guarded RTS compare chains.

    tools/bench/rts_edge_profile.py ROM.nes [--out profiles/<rom>.rtsedge] [--nmis 1000]

Builds ROM normally (unless --skip-build), parses the guarded RTS return chains
of runtime/generated.asm (tools/rts_chain.py) and runs the image headless with
the bench's NMI-indexed input schedule, counting for every chain how often each
candidate jump is taken, how often a compare-first hit (tools/rts_compare_first.py)
returns to its first candidate, and which raw return PCs reach the unmatched
generic-dispatch fallback. Output lines: "<returns per NES frame> <chain key>
<return PC hex>". tools/rts_chain_reorder.py reorders each chain by these
counts; guards stay exact, so a stale profile only affects speed.
"""
import argparse, collections, os, re, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from bench import PyBoy, W, load_sym, buttons_for, BTN  # noqa: E402
import rts_chain  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--out", default=None)
    ap.add_argument("--nmis", type=int, default=1000)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--scenario", default="std", help="bench.py input scenario (merge several with merge_profiles.py)")
    a = ap.parse_args()
    import bench as _bench
    _bench.SCHEDULE[:] = _bench.SCENARIOS[a.scenario]["schedule"]
    out = a.out or os.path.join(ROOT, "profiles", os.path.splitext(os.path.basename(a.rom))[0] + ".rtsedge")
    if not a.skip_build:
        subprocess.run(["make", "generate", f"ROM={a.rom}"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["make", "-B", "-C", "runtime"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    gbc = os.path.join(ROOT, "runtime", "build", "runtime.gbc")
    syms = load_sym(gbc[:-4] + ".sym")
    lines = open(os.path.join(ROOT, "runtime", "generated.asm")).read().splitlines(keepends=True)
    chains = rts_chain.parse(lines)
    codes = [rts_chain.code(l) for l in lines]

    prg = open(a.rom, "rb").read()
    nb = prg[4]
    vec = prg[16 + nb * 16384 - 6: 16 + nb * 16384 - 4]
    nmi_label = "nes_%04X" % (vec[0] | vec[1] << 8)
    pb = PyBoy(gbc, window="null", cgb=True, sound_emulated=False)
    reg = pb.register_file
    st = {"nmi": 0, "p": set()}
    cnt = collections.Counter()

    def on_nmi(_):
        st["nmi"] += 1

    def on_latch(_):
        want = buttons_for(st["nmi"])
        for b in want - st["p"]:
            pb.button_event_now(W(BTN[b][0]))
        for b in st["p"] - want:
            pb.button_event_now(W(BTN[b][1]))
        st["p"] = want

    for s, f in ((nmi_label, on_nmi), ("nes_controller_latch", on_latch)):
        b, ad = syms[s]
        pb.hook_register(b, ad, f, None)

    hooked = set()

    def hook(label, back, fn):
        if label not in syms:
            return
        b, ad = syms[label]
        key = (b, ad - back)
        if key in hooked:
            return
        hooked.add(key)
        pb.hook_register(b, ad - back, fn, None)

    starts = sorted((c.start, c) for c in chains)
    for c in chains:
        for cd in c.cands:
            k = (c.key, cd["target"])
            hook(cd["next_label"], 3 if len(cd["jump"]) == 1 else 8,
                 lambda _, k=k: cnt.__setitem__(k, cnt[k] + 1))
        hook(c.fallback_label, 0,
             lambda _, key=c.key: cnt.__setitem__((key, (reg.HL + 1) & 0xFFFF), cnt[(key, (reg.HL + 1) & 0xFFFF)] + 1))
    # compare-first hit paths: the jump right before nes_rcf_miss_N belongs to the next chain
    for i, c in enumerate(codes):
        m = re.fullmatch(r"(nes_rcf_miss_\d+):", c)
        if not m:
            continue
        nxt = next((ch for s, ch in starts if s > i), None)
        if nxt is None:
            continue
        j = i - 1
        while not codes[j]:
            j -= 1
        mj = re.fullmatch(r"jp nes_([0-9A-F]{4})", codes[j])
        if mj:
            t, back = int(mj.group(1), 16), 3
        elif codes[j] == "jp nes_jump_known_hl_a_8bit":
            q = j - 1
            while not codes[q].startswith("ld hl, nes_"):
                q -= 1
            t, back = int(codes[q][-4:], 16), 8
        else:
            continue
        k = (nxt.key, t)
        hook(m.group(1), back, lambda _, k=k: cnt.__setitem__(k, cnt[k] + 1))

    while st["nmi"] < a.nmis:
        pb.tick(1, False)
    pb.stop(save=False)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(f"# nes2gbc RTS return-edge profile: {os.path.basename(a.rom)}, {a.nmis} NMIs, returns per NES frame\n")
        for (key, pc), n in sorted(cnt.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1])):
            if n / a.nmis >= 0.001:
                f.write(f"{n / a.nmis:.3f} {key} {pc:04X}\n")
    print(f"rts-edge-profile: {len(chains)} chains, {sum(cnt.values()) / a.nmis:.1f} chain returns/frame -> {out}")


if __name__ == "__main__":
    main()
