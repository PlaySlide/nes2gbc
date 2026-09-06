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

SECTION "NES hot sprite state", HRAM[$FF88]
nes_view_x:                ds 1
nes_view_y:                ds 1
nes_view_coord_tmp:        ds 1
nes_view_sprite_tile_tmp:  ds 1
nes_sprite_attr_tmp:       ds 1
nes_sprite_bank_tmp:       ds 1
nes_oam_ppuctrl_tmp:       ds 1

SECTION "Host native stack reserve", WRAM0[$CB00]
; LR35902 CALL/PUSH/interrupt stack. SP starts at $D000 and grows downward.
; Keep this disjoint from NES RAM/runtime state so HRAM can hold hot CPU bytes.
nes_host_stack_reserve: ds $0500

SECTION "NES palette RAM", WRAM0[$C830]
nes_palette_ram: ds 32

SECTION "NES virtual OAM", WRAM0[$C900]
nes_oam_ram: ds 256

; Two physical NES nametables. Mirroring maps the four logical tables here.
SECTION "NES nametable RAM", WRAMX[$D000], BANK[1]
nes_nametable_ram: ds $800
