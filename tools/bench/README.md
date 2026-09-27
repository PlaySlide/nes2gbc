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
