# nes2gbc

Experimental static recompiler targeting **Game Boy Color** from **NES** ROMs.

This is deliberately **not** an NES emulator running on a Game Boy Color. The goal is to analyze 6502 code ahead of time, translate it into native LR35902 code, and translate NES PPU/APU intent into GBC hardware operations.

## Initial scope

- Host compiler: Rust
- Target assembler/linker: RGBDS
- Target hardware: Game Boy Color (CGB)
- Output mapper: MBC5 once generated code outgrows ROM0
- First input mapper: NROM (0), then CNROM (3)
- First real compatibility target: Donkey Kong Classics (Mapper 3)
- No copyrighted ROMs belong in this repository

## Pipeline

```text
.nes
  -> iNES parser
  -> 6502 decoder
  -> control-flow graph
  -> NES semantic IR
  -> LR35902 code generator
  -> GBC runtime (PPU/APU/input/mapper shims)
  -> RGBDS
  -> .gbc
```

## Current status

The first slice parses iNES/NES 2.0 headers, decodes official 6502 opcodes, discovers RESET/NMI/IRQ vectors, recursively builds a conservative control-flow graph for fixed-PRG mappers 0/3 (recording suspicious decode paths as diagnostics rather than aborting), and contains an RGBDS CGB-runtime skeleton. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Build the host tool

```sh
cargo build
cargo test
cargo run -- path/to/game.nes
cargo run -- path/to/game.nes --emit-asm runtime/generated.asm --max-blocks 64
```

Expected output for the initial CNROM target begins like:

```text
Mapper: 3
PRG ROM: 32 KiB
CHR ROM: 16 KiB
Mirroring: Vertical
```

## Build the runtime skeleton

Requires RGBDS (`rgbasm`, `rgblink`, `rgbfix`):

```sh
make -C runtime
```

That produces `runtime/build/runtime.gbc`.

## Sound (APU=1)

NES APU emulation (`runtime/apu.asm`, see `docs/APU.md`) is behind a build
flag and is **off by default** so performance builds stay silent and pay no
APU cost:

```sh
make gbc ROM=game.nes          # APU=0 (default): silent, no APU code
make gbc ROM=game.nes APU=1    # NES sound via CGB channels
```

`APU` must be given to `make generate`/`make gbc`; it is recorded in
`runtime/generated_config.inc`, which the runtime assembly follows. With
`APU=1` the recompiler emits `call nes_apu_write` for fixed `$4000-$4017`
writes and `nes_apu_read_status` for `$4015` reads, the generic bus routes
the same registers to the APU, and VBlank ticks the APU frame sequencer.
Cost on SMB (bench, unpaced): about +8.7% work per NES frame in play
(166.6k -> 181.1k cycles; paced play 74.1% -> 67.5% of native speed).
`APU_TEST_SPEED=2` or `4` (with `APU=1`) compensates audio timing when testing
under emulator fast-forward; it has no effect with `APU=0`.

## ROM policy

ROM images stay local. `*.nes`, `*.gb`, and `*.gbc` are ignored by Git. The recompiler takes a user's local ROM as input; this repository contains only original project code and tests.
