# Architecture

nes2gbc is a static NES-to-Game-Boy-Color recompiler. It does not interpret 6502 opcodes at runtime.

## Current execution model

1. Parse iNES and discover fixed-PRG control flow.
2. Lower official 6502 instructions into semantic IR.
3. Emit native LR35902 basic blocks.
4. Keep canonical NES A/X/Y/SP/P state in GBC WRAM.
5. Route CPU memory accesses through direct WRAM mappings or small semantic bus helpers.
6. Poll NES NMI at translated basic-block boundaries.
7. Translate PPU register intent into CGB tile maps, palettes, scrolling, VRAM banks, and OAM.

The canonical-state model is intentionally correctness-first. Later optimization passes can keep live NES registers/flags in LR35902 registers when dataflow proves that doing so is safe.

## GBC memory layout

### WRAM0

- `$C000-$C7FF`: NES 2 KiB internal RAM (including mirrors mapped by the bus)
- `$C800+`: canonical CPU/cartridge/PPU runtime state
- `$C830+`: NES palette RAM
- `$C900-$C9FF`: virtual NES OAM

### WRAMX bank 1

- `$D000-$D7FF`: two physical NES nametables

### ROM

- ROM0: runtime plus the current development slice of translated code
- ROMX banks 1-2: original NES PRG bytes for data reads
- ROMX banks 3+: raw 8 KiB NES CHR banks for PPU data reads
- following ROMX banks: host-converted GBC tile data

The current fixed bank allocation targets NROM/CNROM. Generated-code banking will replace the ROM0-only development slice later.

## CHR translation

NES and Game Boy tiles are both 8x8, 2bpp, 16 bytes per tile. NES stores all eight rows of bitplane 0 followed by all eight rows of bitplane 1; Game Boy stores the two bitplane bytes interleaved per row. Host conversion therefore only rearranges the 16 bytes of each tile.

For an 8 KiB NES CHR bank:

- NES pattern table `$0000-$0FFF` -> CGB VRAM bank 0 `$8000-$8FFF`
- NES pattern table `$1000-$1FFF` -> CGB VRAM bank 1 `$8000-$8FFF`

CGB tile attributes select the VRAM bank, allowing PPUCTRL's NES pattern-table choice to be represented without changing tile IDs.

## PPU bridge

Implemented semantic pieces:

- `$2000` PPUCTRL
- `$2001` PPUMASK
- `$2002` PPUSTATUS / VBlank polling
- `$2003/$2004` OAM address/data
- `$2005` scroll -> CGB SCX/SCY
- `$2006` PPU address latch
- `$2007` buffered reads and writes
- horizontal/vertical nametable mirroring
- attribute-byte expansion into CGB per-tile palette attributes
- NES background and sprite palette conversion to RGB555
- `$4014` OAM DMA
- CNROM CHR bank switching

The GBC's 160x144 screen is currently a hardware viewport into the NES-style 256x240 tile world. Camera heuristics are a later layer.

## Interrupt model

NMI is checked at translated basic-block boundaries rather than cycle-by-cycle. When a VBlank NMI fires, the runtime pushes a 6502-compatible interrupt frame onto the virtual NES stack and dispatches the translated NMI vector. RTI restores the virtual status/PC and re-enters the generated-PC dispatcher.

This is intentionally approximate timing but preserves the program-level interrupt structure used by conventional NES games.
