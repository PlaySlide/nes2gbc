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
nes_sprite_attr_tmp_wram_pad: ds 1
nes_sprite_bank_tmp_wram_pad: ds 1
nes_controller_strobe: ds 1
nes_controller_shift:  ds 1
nes_host_vblank_pending: ds 1
nes_current_code_bank: ds 1

; Debug breadcrumbs. These live in the gap before palette RAM so they do not
; disturb the existing fixed WRAM layout.
nes_debug_pc_hi:       ds 1 ; $C82B - last requested NES dispatch PC, high byte
nes_debug_pc_lo:       ds 1 ; $C82C - last requested NES dispatch PC, low byte
nes_debug_fault:       ds 1 ; $C82D - $FF if nes_unimplemented was reached
nes_nmi_active:        ds 1 ; $C82E - nonzero while translated NMI handler is active
nes_oam_dirty:         ds 1 ; $C82F - virtual OAM changed; flush on host VBlank

SECTION "NES dispatch cache", WRAM0[$C850]
nes_dispatch_cache_valid:   ds 1
nes_dispatch_cache_pc_hi:   ds 1
nes_dispatch_cache_pc_lo:   ds 1
nes_dispatch_cache_bank:    ds 1
nes_dispatch_cache_addr_hi: ds 1
nes_dispatch_cache_addr_lo: ds 1
nes_debug_bus_hi:           ds 1 ; last generic NES CPU bus-read address
nes_debug_bus_lo:           ds 1
nes_debug_bus_value:        ds 1 ; last PRG byte returned by generic CPU read

SECTION "NES debug viewport", WRAM0[$C860]
nes_view_mode:              ds 1 ; 0 TL, 1 TR, 2 BL, 3 BR, 4 center
nes_view_x_wram_pad:        ds 1
nes_view_y_wram_pad:        ds 1
nes_view_select_prev:       ds 1
nes_view_coord_tmp_wram_pad: ds 1
nes_view_sprite_tile_tmp_wram_pad: ds 1
nes_reset_count_wram_pad:    ds 1 ; preserves legacy WRAM layout

SECTION "NES hot sprite state", HRAM[$FF88]
nes_view_x:                ds 1
nes_view_y:                ds 1
nes_view_coord_tmp:        ds 1
nes_view_sprite_tile_tmp:  ds 1
nes_sprite_attr_tmp:       ds 1
nes_sprite_bank_tmp:       ds 1
nes_oam_ppuctrl_tmp:       ds 1
nes_oam_emit_count:        ds 1
nes_oam_proj_y_tmp:        ds 1
nes_oam_proj_x_tmp:        ds 1
nes_reset_count:           ds 1 ; $FF92
nes_fault_hram:            ds 1 ; $FF93, $FF if nes_unimplemented is reached
nes_last_indirect_lo:      ds 1 ; $FF94
nes_last_indirect_hi:      ds 1 ; $FF95
nes_oam_shadow_ready:      ds 1 ; $FF96, projected GBC OAM matches virtual NES OAM
nes_gbc_palette_shadow:   ds $40 ; $FF97-$FFD6, 32 BG bytes + 32 OBJ bytes
nes_palette_dirty:        ds 1   ; $FFD7
nes_scroll_dirty:         ds 1   ; $FFD8, commit SCX/SCY at host VBlank
nes_ctrl_dirty:           ds 1   ; $FFD9, commit LCDC scroll/sprite mode at VBlank
nes_scroll_pair_count:    ds 1   ; $FFDA, complete $2005 pairs seen in current NES NMI
nes_split_active:         ds 1   ; $FFDB, two distinct raster scroll states captured
nes_split_top_x:          ds 1   ; $FFDC
nes_split_top_y:          ds 1   ; $FFDD
nes_split_bottom_x:       ds 1   ; $FFDE
nes_split_bottom_y:       ds 1   ; $FFDF
nes_split_line:           ds 1   ; $FFE0, host scanline for one raster split

SECTION "Projected GBC OAM shadow", WRAM0[$CB00]
nes_gbc_oam_shadow: ds $00A0

SECTION "Host native stack reserve", WRAM0[$CBA0]
; LR35902 CALL/PUSH/interrupt stack. SP starts at $D000 and grows downward.
; $CBA0-$CFFF leaves 1120 bytes of native stack below the OAM shadow.
nes_host_stack_reserve: ds $0460

SECTION "NES palette RAM", WRAM0[$C830]
nes_palette_ram: ds 32

SECTION "NES virtual OAM", WRAM0[$C900]
nes_oam_ram: ds 256

; Two physical NES nametables. Mirroring maps the four logical tables here.
SECTION "NES nametable RAM", WRAMX[$D000], BANK[1]
nes_nametable_ram: ds $800
