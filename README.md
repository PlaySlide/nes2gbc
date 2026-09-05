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

## ROM policy

ROM images stay local. `*.nes`, `*.gb`, and `*.gbc` are ignored by Git. The recompiler takes a user's local ROM as input; this repository contains only original project code and tests.

