#!/usr/bin/env python3
"""Record translated-block entry counts used to order guarded RTS returns.

    tools/bench/rts_profile.py ROM.nes [--out profiles/<rom>.rtsprof] [--nmis 1000]

Builds ROM normally (unless --skip-build), runs it headless with the bench's
NMI-indexed input schedule and counts entries into every translated block
label nes_XXXX. fast_subroutine_rts_dispatch.py / fast_leaf_rts_dispatch.py
use these counts (entries per NES frame of each return-target block) to pick
and order the exact return guards of an RTS; the static JSR-site weight only
breaks ties. Output lines: "<entries per NES frame> <NES PC hex>". Guards stay
exact, so a stale profile only affects speed, never behavior.
"""
import argparse, collections, os, re, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import bench  # noqa: E402
from bench import PyBoy, W, load_sym, buttons_for, BTN  # noqa: E402

BLOCK = re.compile(r"^nes_([0-9A-F]{4})$")


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
    out = a.out or os.path.join(ROOT, "profiles", os.path.splitext(os.path.basename(a.rom))[0] + ".rtsprof")
    if not a.skip_build:
        subprocess.run(["make", "generate", f"ROM={a.rom}"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["make", "-B", "-C", "runtime"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    gbc = os.path.join(ROOT, "runtime", "build", "runtime.gbc")
    syms = load_sym(gbc[:-4] + ".sym")
    prg = open(a.rom, "rb").read()
    nb = prg[4]
    vec = prg[16 + nb * 16384 - 6: 16 + nb * 16384 - 4]
    nmi_label = "nes_%04X" % (vec[0] | vec[1] << 8)
    pb = PyBoy(gbc, window="null", cgb=True, sound_emulated=False)
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
    seen = set()
    for name, (b, ad) in syms.items():
        m = BLOCK.match(name)
        if not m or name == nmi_label or (b, ad) in seen:
            continue
        seen.add((b, ad))
        pc = int(m.group(1), 16)
        pb.hook_register(b, ad, lambda _, pc=pc: cnt.__setitem__(pc, cnt[pc] + 1), None)
    while st["nmi"] < a.nmis:
        pb.tick(1, False)
    pb.stop(save=False)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(f"# nes2gbc block entry profile: {os.path.basename(a.rom)}, {a.nmis} NMIs, entries per NES frame\n")
        for pc, n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])):
            if n / a.nmis >= 0.001:
                f.write(f"{n / a.nmis:.3f} {pc:04X}\n")
    print(f"rts-profile: {len(cnt)} blocks entered, {sum(cnt.values()) / a.nmis:.0f} entries/frame -> {out}")


if __name__ == "__main__":
    main()
