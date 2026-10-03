#!/usr/bin/env python3
"""Headless nes2gbc SMB benchmark (patched PyBoy with PC-cycle profiler).

Metric definitions
  NMI           = entry of translated NES NMI handler (nes_8082 for SMB).
  host frame    = one GBC LCD frame (70224 dots = 140448 CPU cycles in double speed).
  %native       = NES frames (NMIs) per host frame over a window * 100.
                  Native NES speed = 60 NMIs / 60 host frames = 100%.
  work cycles   = GBC CPU cycles from NMI entry until the SMB idle loop
                  (nes_8057) is next reached (includes VBlank/STAT ISR time).
Input is scheduled per NES NMI index (applied exactly at the $4016 latch), so
game logic is deterministic across builds; NES RAM ($C000-$C7FF) hashes at fixed
NMI indices and framebuffer hashes after fixed NMIs verify output is unchanged. The framebuffer for
check NMI N is the first emulator frame in which NMI N+1 starts; since 2026-10 it is
also taken when NMIs N+1 and N+2 start in the same emulator frame (previously that
screenshot was silently dropped, changing `check` without any output change).
Reference: check=ed2ed09fc6b4 for SMB with both PACING=0 and PACING=1.
"""
import argparse, hashlib, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("PYBOY_PROF_DIR", os.path.join(HERE, ".pyboy", "pyboy-2.7.0")))
import logging
logging.disable(logging.WARNING)
import numpy as np
from pyboy import PyBoy
from pyboy.utils import WindowEvent as W

HOST_FRAME_CYCLES = 140448
PROFWIN = "play"

def load_sym(path):
    syms = {}
    for line in open(path):
        line = line.split(";")[0].strip()
        if not line or " " not in line:
            continue
        loc, name = line.split(None, 1)
        b, a = loc.split(":")
        syms[name] = (int(b, 16), int(a, 16))
    return syms

# NMI-index keyed input schedule: list of (start, end_exclusive, buttons)
SCHEDULE = [
    (100, 106, {"start"}),
    (300, 2000, {"right"}),
    # periodic jumps while walking
] + [(s, s + 25, {"right", "a"}) for s in range(330, 2000, 70)]
BTN = {"start": (W.PRESS_BUTTON_START, W.RELEASE_BUTTON_START),
       "right": (W.PRESS_ARROW_RIGHT, W.RELEASE_ARROW_RIGHT),
       "a": (W.PRESS_BUTTON_A, W.RELEASE_BUTTON_A)}

def buttons_for(n):
    s = set()
    for a, b, bt in SCHEDULE:
        if a <= n < b:
            s |= bt
    return s

CHECK_NMIS = [60, 99, 150, 250, 320, 400, 500, 600, 700, 800, 900, 1000]
WINDOWS = {"title": (30, 100), "play": (360, 850)}

def run(rom, sym, nmis, profile=None, shots=None):
    syms = load_sym(sym)
    pb = PyBoy(rom, window="null", cgb=True, sound_emulated=False)
    pb.set_emulation_speed(0)
    st = {"nmi": 0, "pressed": set(), "nmi_cyc": [], "idle_cyc": [], "waiting": False,
          "nmi_tick": [], "ram": {}, "latches": 0, "vmem": {}}

    def screen_mem():
        # GBC screen memory: VRAM banks 0+1 (tiles, maps, attributes) and OAM.
        v = b"".join(bytes(pb.memory[k, 0x8000:0x9FFF]) + bytes([pb.memory[k, 0x9FFF]]) for k in (0, 1))
        return hashlib.sha1(v + bytes(pb.memory[0xFE00:0xFEA0])).hexdigest()[:12]

    def on_nmi(_):
        c = pb._cycles()
        st["nmi"] += 1
        st["nmi_cyc"].append(c)
        st["nmi_tick"].append(st["tick"])
        st["waiting"] = True
        if (st["nmi"] - 1) in CHECK_NMIS:
            # state produced by NES frame N, sampled when NMI N+1 starts; independent of
            # how many NMIs fall in one emulator frame (unlike the LCD screenshot)
            st["vmem"][st["nmi"] - 1] = screen_mem()
        if st["nmi"] in CHECK_NMIS:
            st["ram"][st["nmi"]] = hashlib.sha1(bytes(pb.memory[0xC000:0xC800])).hexdigest()[:12]

    def on_idle(_):
        if st["waiting"]:
            st["waiting"] = False
            st["idle_cyc"].append((st["nmi"], pb._cycles()))

    def on_latch(_):
        st["latches"] += 1
        want = buttons_for(st["nmi"])
        for b in want - st["pressed"]:
            pb.button_event_now(W(BTN[b][0]))
        for b in st["pressed"] - want:
            pb.button_event_now(W(BTN[b][1]))
        st["pressed"] = want

    b, a = syms["nes_8082"]; pb.hook_register(b, a, on_nmi, None)
    b, a = syms["nes_8057"]; pb.hook_register(b, a, on_idle, None)
    b, a = syms["nes_controller_latch"]; pb.hook_register(b, a, on_latch, None)

    prof = None
    screens = {}
    st["tick"] = 0
    t0 = time.time()
    last = 0
    maxticks = nmis * 6 + 600
    while st["nmi"] < nmis and st["tick"] < maxticks:
        if profile and prof is None and st["nmi"] >= WINDOWS[PROFWIN][0]:
            prof = np.zeros(0x410000, dtype=np.uint64)
            pb.prof_attach(prof)
            prof_start = (st["nmi"], pb._cycles())
        if prof is not None and st["nmi"] >= WINDOWS[PROFWIN][1] and "prof_end" not in st:
            pb.prof_detach(); st["prof_end"] = 1
        before = st["nmi"]
        pb.tick(1, True)
        st["tick"] += 1
        # screenshot for check NMI N = first emulator frame in which NMI N+1 started
        # (also when NMIs N+1 and N+2 share that frame, which used to drop the shot)
        for n in range(before, st["nmi"]):
            if n in CHECK_NMIS and n not in screens:
                arr = pb.screen.ndarray
                screens[n] = hashlib.sha1(arr.tobytes()).hexdigest()[:12]
                if shots:
                    os.makedirs(shots, exist_ok=True)
                    pb.screen.image.save(os.path.join(shots, "nmi%04d.png" % n))
    wall = time.time() - t0
    if prof is not None:
        pb.prof_detach()
        np.save(profile, prof)
    res = {"nmis": st["nmi"], "ticks": st["tick"], "wall_s": round(wall, 1)}
    for name, (lo, hi) in WINDOWS.items():
        if st["nmi"] <= hi:
            continue
        ticks = st["nmi_tick"][hi - 1] - st["nmi_tick"][lo - 1]
        cyc = st["nmi_cyc"][hi - 1] - st["nmi_cyc"][lo - 1]
        work = [c - st["nmi_cyc"][n - 1] for n, c in st["idle_cyc"] if lo <= n < hi]
        res[name] = {
            "pct_native": round(100.0 * (hi - lo) / ticks, 2),
            "host_frames_per_nes_frame": round(ticks / (hi - lo), 3),
            "gbc_cycles_per_nes_frame": int(cyc / (hi - lo)),
            "work_cycles_per_nes_frame": int(sum(work) / max(1, len(work))),
            "work_pct_of_host_frame": round(100.0 * sum(work) / max(1, len(work)) / HOST_FRAME_CYCLES, 1),
            "max_work_cycles": max(work) if work else 0,
        }
    res["ram_hash"] = st["ram"]
    res["screen_hash"] = screens
    res["vmem_hash"] = st["vmem"]
    # smem: NES RAM + GBC VRAM/OAM sampled at NMI N+1 entry. Stricter than the LCD shot and
    # timing-independent with PACING=0; with PACING=1 OAM/attribute publish timing leaks in,
    # so it is a diagnostic there, not a gate.
    res["smem"] = hashlib.sha1(json.dumps([st["ram"], st["vmem"]], sort_keys=True).encode()).hexdigest()[:12]
    # check (the gate): NES RAM + LCD screenshot hashes at CHECK_NMIS
    res["check"] = hashlib.sha1(json.dumps([st["ram"], screens], sort_keys=True).encode()).hexdigest()[:12]
    pb.stop(save=False)
    return res

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rom", default=os.path.join(HERE, "..", "..", "runtime", "build", "runtime.gbc"))
    ap.add_argument("--sym", default=None)
    ap.add_argument("--nmis", type=int, default=1001)
    ap.add_argument("--profile", default=None, help="save per-(bank,pc) cycle histogram .npy for play window")
    ap.add_argument("--profwin", default="play", choices=["title", "play"])
    ap.add_argument("--shots", default=None, help="dir for PNG screenshots at check NMIs")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    PROFWIN = a.profwin
    sym = a.sym or os.path.splitext(a.rom)[0] + ".sym"
    r = run(a.rom, sym, a.nmis, a.profile, a.shots)
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        for w in WINDOWS:
            if w in r:
                d = r[w]
                print(f"{w:6s} {d['pct_native']:6.2f}% native  {d['host_frames_per_nes_frame']:.3f} hostfr/NESfr  "
                      f"work {d['work_cycles_per_nes_frame']:7d} cyc/NESfr ({d['work_pct_of_host_frame']}% of host frame, max {d['max_work_cycles']})")
        print(f"check={r['check']}  smem={r['smem']}  nmis={r['nmis']} ticks={r['ticks']} wall={r['wall_s']}s")
        print("ram  ", r["ram_hash"])
        print("screen", r["screen_hash"])
        print("vmem ", r["vmem_hash"])
