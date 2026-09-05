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
- [ ] A/X/Y and flag model
- [x] direct zero-page / internal RAM mapping into GBC WRAM
- [ ] stack semantics
- [x] initial branches / JSR / RTS / absolute JMP emission
- [ ] block dispatcher for indirect control flow
- [ ] flag liveness optimization

## Milestone 3 — GBC runtime

- [x] RGBDS boot skeleton
- [x] CGB double-speed initialization
- [ ] VBlank synchronization
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
