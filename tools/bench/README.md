# Headless SMB speed benchmark

`tools/bench/setup.sh` once (builds a patched PyBoy 2.7.0 with a per-(bank,PC)
cycle histogram), then `tools/bench/run.sh` builds SMB and prints:

- `%native`: NES frames (translated NMI entries, `nes_8082`) per GBC host
  frame over a window, x100. 100% = 60 NES fps.
- `work`: GBC CPU cycles (double speed, 140448 per host frame) from NMI entry
  until SMB's idle loop `nes_8057` is reached again, averaged per NES frame.
  Includes VBlank/STAT ISR time. This is the unquantized cost; `%native`
  only moves when work crosses a whole host frame.
- `check`: hash of NES RAM at fixed NMI indices plus framebuffer after fixed
  NMIs. Input (Start, then Right + periodic A) is applied at the `$4016`
  latch keyed by NMI index, so SMB game logic is deterministic across
  builds; `check` must stay `ed2ed09fc6b4` for an output-preserving change.

Windows: `title` = NMIs 30-100 (title screen), `play` = NMIs 360-850 (1-1).

Profiling: `run.sh --profile /tmp/p.npy [--profwin title]` then
`python3 tools/bench/profreport.py /tmp/p.npy runtime/build/runtime.sym` and
`python3 tools/bench/opstats.py /tmp/p.npy runtime/build/runtime.gbc runtime/build/runtime.sym`.

`crosscheck.py game.nes build.gbc` gives a generic NMI-rate/RAM-hash run for
other games (DK/IC/BF RAM hashes are timing-sensitive, so only use them to
detect crashes/hangs, not exact equality).

## Heavy-action scenario

`bench.py --scenario heavy` (or `tools/bench/run.sh --scenario heavy`) runs a
second frozen input schedule: Mario runs through 1-1 hopping over goombas and
is held at the tall pipe with 2-3 enemies active. Windows: `play` (360-640,
mostly 1 enemy) and `heavy` (640-1100, 2-3 enemies). For non-std scenarios the
per-frame work distribution (p50/p90/max, frames over the 140,448-cycle host
frame budget) is printed. Gate reference: `ref_smb_heavy.json` (5323060 PACING=0;
P0 `check=df2281c43a8c`).
