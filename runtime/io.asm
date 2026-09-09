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
nes_generic_map_rebuild_dirty: ds 1 ; changed NT data while generic rendering was off
nes_hstitch_seen: ds 1 ; title has successfully established horizontal stitched presentation
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

; Nametable writes made while a translated NES NMI is running are authoritative
; in virtual WRAM immediately, but are not exposed to live GBC VRAM until that
; whole NES NMI has completed. C859-C85B are free before the legacy viewport.
nes_nametable_queue_ptr_lo: ds 1 ; next byte in $D800-$DFFF staging queue
nes_nametable_queue_ptr_hi: ds 1
nes_nametable_queue_overflow: ds 1
nes_mask_dirty:             ds 1 ; $C85C defer PPUMASK hardware publication
nes_ntdiag_min_row:         ds 1 ; $C85D, minimum tile row touched (0-29)
nes_ntdiag_max_row:         ds 1 ; $C85E, maximum tile row touched
nes_ntdiag_display_map:     ds 1 ; $C85F, GBC BG map at publish: 0=$9800, 1=$9C00

SECTION "NES debug viewport", WRAM0[$C860]
nes_view_mode:              ds 1 ; 0 TL, 1 TR, 2 BL, 3 BR, 4 center
nes_view_armed_x:           ds 1 ; crop X committed with current host frame
nes_view_armed_y:           ds 1 ; crop Y committed with current host frame
nes_view_select_prev:       ds 1
nes_bg_pattern_committed:    ds 1 ; $C864 committed CGB attr bank bit (0/$08)
nes_nametable_stage_used:     ds 1 ; staging bitmap touched since last clear
nes_split_duplicate_streak:  ds 1 ; $C866 consecutive duplicate-only split NMIs

; Last completed translated-NMI nametable transaction diagnostics.
; These are observational only: they do not alter renderer behavior.
SECTION "NES nametable diagnostics", WRAM0[$C867]
nes_ntdiag_tile_count:      ds 1 ; C867, tile writes (attributes excluded)
nes_ntdiag_phys_mask:       ds 1 ; C868, bit0=$9800/phys0, bit1=$9C00/phys1
nes_ntdiag_min_col:         ds 1 ; C869, minimum tile column touched
nes_ntdiag_max_col:         ds 1 ; C86A, maximum tile column touched
nes_ntdiag_first_hi:        ds 1 ; C86B, first physical NT address high ($D0-$D7)
nes_ntdiag_first_lo:        ds 1 ; C86C
nes_ntdiag_last_hi:         ds 1 ; C86D, last physical tile address high
nes_ntdiag_last_lo:         ds 1 ; C86E
nes_ntdiag_commit_serial:   ds 1 ; C86F, increments on each nonempty NT publish

; Follow-camera bookkeeping lives in the gap immediately after the optional
; runtime profile counters ($C870-$C8E8) and before virtual NES OAM at $C900.
; Keep the legacy $C860 debug-view block fixed so profiler builds do not overlap.
SECTION "NES follow viewport", WRAM0[$C8E9]
nes_view_follow_enabled:     ds 1 ; 1=automatic player-follow camera, 0=manual
nes_view_follow_valid:       ds 1
nes_view_follow_was_valid:   ds 1
nes_view_follow_x:           ds 1 ; tracked NES sprite anchor
nes_view_follow_y:           ds 1
nes_view_follow_ref_x:       ds 1
nes_view_follow_ref_y:       ds 1
nes_view_follow_best_dist:   ds 1
nes_view_follow_candidate_x: ds 1
nes_view_follow_candidate_y: ds 1
nes_view_follow_slot:        ds 1 ; locked NES OAM slot (0-63)

; Horizontal nametable stitch state. For vertical mirroring, the two NES
; physical nametables are horizontal neighbours; a single 256px GBC map cannot
; represent their 512px scroll space without stitching the wrap columns.
SECTION "NES horizontal stitch state", WRAM0[$C8F4]
nes_hstitch_valid:          ds 1
nes_hstitch_dirty:          ds 1
nes_hstitch_key:            ds 1 ; bit5=base physical page, bits0-4=coarse X
nes_hstitch_copy_start:     ds 1
nes_hstitch_copy_len:       ds 1
nes_hstitch_copy_skip:      ds 1
nes_hstitch_target_key:     ds 1 ; C8FA target key for small multi-tile catch-up
nes_hstitch_full_rebuilds:  ds 1 ; C8FB diagnostic counter
nes_hstitch_catchups:       ds 1 ; C8FC diagnostic counter
nes_diag_frame_serial:      ds 1 ; C8FD, increments every host VBlank
nes_diag_ring_index:        ds 1 ; C8FE, next 4-byte ring slot (0-3)
nes_diag_event_flags:       ds 1 ; C8FF, events accumulated for current host frame

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
nes_split_top_ctrl:       ds 1   ; $FFE1, PPUCTRL paired with top/HUD scroll
nes_split_bottom_ctrl:    ds 1   ; $FFE2, captured lower/playfield PPUCTRL
nes_split_armed_x:        ds 1   ; $FFE3, immutable lower X for current host frame
nes_split_armed_y:        ds 1   ; $FFE4, immutable lower Y for current host frame
nes_split_armed_ctrl:     ds 1   ; $FFE5, immutable lower PPUCTRL for current host frame
nes_split_pending_x:      ds 1   ; $FFE6, uncommitted first scroll pair
nes_split_pending_y:      ds 1   ; $FFE7
nes_split_pending_ctrl:   ds 1   ; $FFE8
nes_split_armed_top_x:    ds 1   ; $FFE9, immutable HUD X for current host frame
nes_split_armed_top_y:    ds 1   ; $FFEA
nes_split_armed_top_ctrl: ds 1   ; $FFEB
nes_seam_active:           ds 1   ; $FFEC, single-scroll crop crosses NES Y=240
nes_seam_line:             ds 1   ; $FFED, host scanline for vertical nametable seam
nes_seam_top_ctrl:         ds 1   ; $FFEE, logical nametable above seam
nes_seam_bottom_ctrl:      ds 1   ; $FFEF, logical nametable below seam
nes_seam_top_y:            ds 1   ; $FFF0, GBC SCY before seam
nes_seam_bottom_y:         ds 1   ; $FFF1, GBC SCY after seam (+16 compensation)
nes_fault_pc_lo:           ds 1   ; $FFF2, exact 6502 PC for generated unimplemented block
nes_fault_pc_hi:           ds 1   ; $FFF3

; Crash snapshot. These are written only on nes_unimplemented, so they are free
; in the hot path and let mGBA show enough of JumpEngine/stack state to identify
; an out-of-range computed jump without a TRACE build.
nes_fault_target_lo:       ds 1   ; $FFF4, last dispatch-cache NES PC low
nes_fault_target_hi:       ds 1   ; $FFF5
nes_fault_sp_snapshot:     ds 1   ; $FFF6
nes_fault_a_snapshot:      ds 1   ; $FFF7
nes_fault_x_snapshot:      ds 1   ; $FFF8
nes_fault_y_snapshot:      ds 1   ; $FFF9
nes_fault_zp04_snapshot:   ds 1   ; $FFFA, JumpEngine caller-return pointer low
nes_fault_zp05_snapshot:   ds 1   ; $FFFB, caller-return pointer high
nes_fault_zp06_snapshot:   ds 1   ; $FFFC, computed indirect target low
nes_fault_zp07_snapshot:   ds 1   ; $FFFD, computed indirect target high
nes_fault_kind:            ds 1   ; $FFFE, $01 = nes_unimplemented

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

; Up to 1024 staged physical nametable addresses (2 bytes each). Values are
; read from authoritative nametable WRAM only when the completed NMI is published.
SECTION "NES nametable staging queue", WRAMX[$D800], BANK[1]
nes_nametable_queue: ds $800

; Last NES nametable bytes successfully processed for live GBC publication.
; Bank 6 is intentionally separate from authoritative NT RAM (bank 1) and the
; NROM PRG cache (banks 2-5).  Matching bytes can skip all synchronized VRAM
; work; stitched columns still source their actual content from bank-1 WRAM.
SECTION "NES published nametable shadow", WRAMX[$D000], BANK[6]
nes_nametable_published_shadow: ds $800

; One bit per physical nametable byte, cleared when a translated NES NMI starts.
; If the same PPU address is written repeatedly during that NMI, enqueue it once.
; The retained queue entry still publishes the final authoritative WRAM byte.
SECTION "NES nametable stage seen", WRAMX[$D800], BANK[6]
nes_nametable_stage_seen: ds $100
