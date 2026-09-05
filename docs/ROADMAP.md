# Roadmap

## Milestone 0 — cartridge front end

- [x] iNES 1.0 parsing
- [x] basic NES 2.0 parsing
- [x] mapper/submapper extraction
- [x] PRG/CHR slicing
- [x] mirroring/trainer/battery metadata
- [ ] richer NES 2.0 ROM-size encodings

## Milestone 1 — 6502 analysis

- [x] official 6502 opcode decoder
- [x] address-mode representation
- [x] RESET/NMI/IRQ vector discovery
- [x] recursive basic-block discovery (fixed-PRG mappers 0/3)
- [ ] distinguish code from data conservatively
- [ ] indirect-jump target annotations
- [ ] optional Mesen CDL import

## Milestone 2 — native LR35902 code generation

- [x] NES semantic IR (initial reset-path subset)
- [x] canonical A/X/Y/SP/P state with shadow C/Z/I/D/V/N flags
- [x] direct zero-page / internal RAM mapping into GBC WRAM
- [x] virtual 6502 stack with PHA/PLA/PHP/PLP and JSR/RTS return handling
- [x] branches / JSR / RTS / absolute JMP with explicit fallthrough and PC dispatcher
- [ ] block dispatcher for indirect control flow
- [ ] flag liveness optimization (correct shadow flags implemented first)

## Milestone 3 — GBC runtime

- [x] RGBDS boot skeleton
- [x] CGB double-speed initialization
- [x] initial PPUSTATUS vblank polling from GBC LY
- [ ] controller translation
- [x] initial $2000/$2001/$2002 virtual register shims
- [ ] nametable -> BG tile map synchronization
- [ ] NES attribute -> CGB palette attributes
- [ ] virtual OAM -> GBC shadow OAM

## Milestone 4 — graphics/assets

- [ ] CHR bitplane interleave converter
- [ ] CHR ROM upload
- [ ] CHR RAM dirty-tile updates
- [ ] NES palette -> RGB555 conversion
- [ ] 32x30 virtual screen with 20x18 viewport

## Milestone 5 — first games

- [ ] NROM test/homebrew ROM
- [ ] Mapper 3 / CNROM
- [ ] Donkey Kong Classics as first commercial compatibility target

## Later

- APU translation
- UxROM / MMC1 / MMC3
- camera heuristics
- raster effects
- aggressive cross-block optimization
- differential execution traces against Mesen/SameBoy


### CPU coverage added after initial reset slice

- indexed zero-page / absolute addressing
- indirect indexed address formation
- INX/INY/DEX/DEY and register transfers
- AND/ORA/EOR
- ADC/SBC with 6502 C/V/N/Z semantics
- canonical RAM mirroring and generic CPU read/write shims
