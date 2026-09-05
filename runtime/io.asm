; NES PPU/APU virtual state.

SECTION "NES virtual IO state", WRAM0[$C810]
nes_ppu_status: ds 1
nes_ppuctrl:    ds 1
nes_ppumask:    ds 1
nes_dac:        ds 1
