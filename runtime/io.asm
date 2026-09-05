; NES mapper / PPU / APU virtual state.

SECTION "NES cartridge state", WRAM0[$C810]
nes_mapper:           ds 1
nes_mirroring:        ds 1
nes_prg_16k_mirror:   ds 1
nes_chr_bank_mask:    ds 1
nes_chr_bank:         ds 1
nes_chr_gbc_bank_base: ds 1

SECTION "NES virtual IO state", WRAM0[$C818]
nes_ppu_status:       ds 1
nes_ppuctrl:          ds 1
nes_ppumask:          ds 1
nes_oamaddr:          ds 1
nes_ppu_scroll_x:     ds 1
nes_ppu_scroll_y:     ds 1
nes_ppu_addr_hi:      ds 1
nes_ppu_addr_lo:      ds 1
nes_ppu_latch:        ds 1
nes_ppu_read_buffer:  ds 1
nes_dac:              ds 1
nes_saved_lcdc:       ds 1
nes_palette_sync_color: ds 1
nes_sprite_attr_tmp:  ds 1
nes_sprite_bank_tmp:  ds 1
nes_controller_strobe: ds 1
nes_controller_shift:  ds 1
nes_nmi_vblank_seen:   ds 1

SECTION "NES palette RAM", WRAM0[$C830]
nes_palette_ram: ds 32

SECTION "NES virtual OAM", WRAM0[$C900]
nes_oam_ram: ds 256

; Two physical NES nametables. Mirroring maps the four logical tables here.
SECTION "NES nametable RAM", WRAMX[$D000], BANK[1]
nes_nametable_ram: ds $800
