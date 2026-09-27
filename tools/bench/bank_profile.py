#!/usr/bin/env python3
"""Record the dynamic cross-bank jump profile used by repack_code_banks_final.py.

    tools/bench/bank_profile.py ROM.nes [--out profiles/<rom>.bankprof] [--nmis 1000]

Builds ROM with REPACK_IDENTITY=1 (every Rust-emitter code bank kept separate,
so every inter-bank CFG edge is a nes_jump_known_hl_a_8bit trampoline and the
current bank at the helper names the source's emitter bank), runs it headless
with the bench's NMI-indexed input schedule (inputs applied at the $4016
latch, so the path is deterministic), and counts helper entries per
(source section, target label). Output lines: "<jumps per NES frame> <source
section name> <target label>". Section and label names are stable across
builds; entries that no longer resolve are ignored by the packer.
Rebuild normally afterwards (make generate ROM=...) to pick the profile up.
"""
import argparse, collections, os, re, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import bench  # noqa: E402  (sets up the PyBoy import path)
from bench import PyBoy, W, load_sym, buttons_for, BTN  # noqa: E402

SEC_RE = re.compile(r'^\s*SECTION\s+"([^"]+)",\s*ROMX,\s*BANK\[(\d+)\]')
LABEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):{1,2}$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("--out", default=None)
    ap.add_argument("--nmis", type=int, default=1000)
    ap.add_argument("--skip-build", action="store_true")
    a = ap.parse_args()
    out = a.out or os.path.join(ROOT, "profiles", os.path.splitext(os.path.basename(a.rom))[0] + ".bankprof")
    if not a.skip_build:
        subprocess.run(["make", "generate", f"ROM={a.rom}", "REPACK_IDENTITY=1", "BANK_PROFILE="],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["make", "-B", "-C", "runtime"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    asm = open(os.path.join(ROOT, "runtime", "generated.asm")).read().splitlines()
    codes = [l.split(";", 1)[0].strip() for l in asm]
    # (bank, target label) -> source sections holding a trampoline to it.
    sites = collections.defaultdict(list)
    sec = bank = None
    for i, c in enumerate(codes):
        m = SEC_RE.match(c)
        if m:
            sec, bank = m.group(1), int(m.group(2))
            continue
        if c.startswith("SECTION"):
            sec = bank = None
        if sec and c in ("jp nes_jump_known_hl_a_8bit", "jp nes_jump_known_hl_a") and codes[i - 1].startswith("ld hl, "):
            sites[(bank, codes[i - 1][7:].strip())].append(sec)
    gbc = os.path.join(ROOT, "runtime", "build", "runtime.gbc")
    syms = load_sym(gbc[:-4] + ".sym")
    addr_label = {}
    for name, (b, ad) in syms.items():
        if "." not in name:
            addr_label.setdefault((b, ad), name)
    prg = open(a.rom, "rb").read()
    nb = prg[4]
    vec = prg[16 + nb * 16384 - 6: 16 + nb * 16384 - 4]
    nmi_label = "nes_%04X" % (vec[0] | vec[1] << 8)
    pb = PyBoy(gbc, window="null", cgb=True, sound_emulated=False)
    reg = pb.register_file
    st = {"nmi": 0, "p": set()}
    cnt = collections.Counter()
    cur = syms["nes_current_code_bank"][1]

    def on_nmi(_):
        st["nmi"] += 1

    def on_latch(_):
        want = buttons_for(st["nmi"])
        for b in want - st["p"]:
            pb.button_event_now(W(BTN[b][0]))
        for b in st["p"] - want:
            pb.button_event_now(W(BTN[b][1]))
        st["p"] = want

    def on_jump(_):
        cnt[(pb.memory[cur], reg.A, reg.HL)] += 1
    for s, f in ((nmi_label, on_nmi), ("nes_controller_latch", on_latch), ("nes_jump_known_hl_a_8bit", on_jump)):
        b, ad = syms[s]
        pb.hook_register(b, ad, f, None)
    while st["nmi"] < a.nmis:
        pb.tick(1, False)
    pb.stop(save=False)
    agg = collections.Counter()
    unresolved = 0
    for (src_bank, tb, hl), n in cnt.items():
        tgt = addr_label.get((tb, hl))
        cands = sites.get((src_bank, tgt)) if tgt else None
        if not cands:
            unresolved += n
            continue
        for s in cands:
            agg[(s, tgt)] += n / len(cands)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(f"# nes2gbc cross-bank jump profile: {os.path.basename(a.rom)}, {a.nmis} NMIs, "
                "jumps per NES frame\n")
        for (s, t), n in sorted(agg.items(), key=lambda kv: (-kv[1], kv[0])):
            if n / a.nmis >= 0.001:
                f.write(f"{n / a.nmis:.3f} {s} {t}\n")
    tot = sum(cnt.values())
    print(f"bank-profile: {tot / a.nmis:.1f} cross-bank jumps/frame, {len(agg)} edges, "
          f"{unresolved / max(tot, 1) * 100:.1f}% unresolved -> {out}")


if __name__ == "__main__":
    main()
