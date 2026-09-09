; NES mapper / PPU / APU virtual state.

; Four-frame renderer diagnostics.  The 16-byte ring at C800 keeps four
; 4-byte snapshots, oldest/newest determined by nes_diag_ring_index.
; Event bits describe work that occurred during the host frame just completed.
DEF NES_DIAG_EVENT_COMMIT          EQU $01
DEF NES_DIAG_EVENT_QUEUE_FLUSH     EQU $02
DEF NES_DIAG_EVENT_FULL_REBUILD    EQU $04
DEF NES_DIAG_EVENT_CATCHUP         EQU $08
DEF NES_DIAG_EVENT_STAT_SPLIT      EQU $10
DEF NES_DIAG_EVENT_BG_BANK_REWRITE EQU $20
DEF NES_DIAG_EVENT_PALETTE_COMMIT  EQU $40
DEF NES_DIAG_EVENT_CTRL_COMMIT     EQU $80

SECTION "NES renderer diagnostic ring", WRAM0[$C800]
nes_diag_ring: ds $10

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
nes_vblank_acked:       ds 1 ; 1 after $2002 cleared vblank in this host vblank
nes_sprite_bank_tmp_wram_pad: ds 1
nes_controller_strobe: ds 1
nes_controller_shift:  ds 1
nes_host_vblank_pending: ds 1
nes_current_code_bank: ds 1
