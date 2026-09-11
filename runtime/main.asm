; nes2gbc Game Boy Color runtime skeleton.
INCLUDE "hardware.inc"

SECTION "VBlank Vector", ROM0[$0040]
    jp nes_gbc_vblank_isr

SECTION "STAT Vector", ROM0[$0048]
    jp nes_gbc_stat_isr

SECTION "Header Entry", ROM0[$0100]
    nop
    jp Start

; C817 is the remaining free byte beside the hidden-map change counter at C816.
; Remember whether the current proven SMB stitch already consumed its one extra
; duplicate-NMI grace. This prevents the host-side grace from relatching forever.
SECTION "SMB split grace state", WRAM0[$C817]
nes_split_retire_grace_used: ds 1

SECTION "Runtime", ROM0[$0150]
nes_gbc_vblank_isr:
    push af
    push bc
    push de
    push hl

    ; Snapshot the host frame that just finished, then clear its event latch so
    ; work done by this VBlank is attributed to the frame about to be shown.
    call nes_diag_snapshot_frame

    ; A proven SMB split occasionally emits two duplicate-only scroll NMIs in a
    ; row even though gameplay has not left the stitched presentation. The PPU
    ; recognizer deliberately retires a generic split after two duplicates for
    ; Ice Climber, but the SMB video logs show that this creates a 4-17 frame
    ; hole where the physical backing map (future pipes/clouds) is exposed.
    ;
    ; Give an already-valid stitched renderer exactly one more NES-NMI chance.
    ; The one-shot flag remains set if a third duplicate really retires it; a
    ; later genuine distinct split has duplicate_streak=0 and clears the flag.
    ldh a, [nes_split_active]
    and a
    jr z, .split_grace_consider

    ld a, [nes_split_duplicate_streak]
    and a
    jr nz, .split_grace_done
    xor a
    ld [nes_split_retire_grace_used], a
    jr .split_grace_done

.split_grace_consider:
    ld a, [nes_split_retire_grace_used]
    and a
    jr nz, .split_grace_done
    ld a, [nes_hstitch_valid]
    and a
    jr z, .split_grace_done
    ld a, [nes_hstitch_seen]
    and a
    jr z, .split_grace_done
    ld a, [nes_ppumask]
    and $18
    cp $18
    jr nz, .split_grace_done
    ld a, $01
    ld [nes_split_retire_grace_used], a
    ldh [nes_split_active], a
    ld a, $02
    ld [nes_split_duplicate_streak], a
.split_grace_done:

    ; Arm the HUD/playfield raster state before any potentially long
    ; completed-frame publication. The long BG path below temporarily allows
    ; only STAT to nest so this armed split can still fire exactly at LYC.
    ldh a, [nes_split_active]
    and a
    jp z, .early_split_done

    ; A completed translated NMI has a coherent new split state. Freeze it now
    ; so the raster state used by this host frame matches the transaction being
    ; published below. If an NMI is still active, retain the previous armed state.
    ld a, [nes_nmi_active]
    and a
    jr nz, .early_split_apply

    ldh a, [nes_split_top_x]
    ldh [nes_split_armed_top_x], a
    ldh a, [nes_split_top_y]
    ldh [nes_split_armed_top_y], a
    ldh a, [nes_split_top_ctrl]
    ldh [nes_split_armed_top_ctrl], a

    ldh a, [nes_split_bottom_x]
    ldh [nes_split_armed_x], a
    ldh a, [nes_split_bottom_y]
    ldh [nes_split_armed_y], a
    ldh a, [nes_split_bottom_ctrl]
    ldh [nes_split_armed_ctrl], a

    ldh a, [nes_view_x]
    ld [nes_view_armed_x], a
    ldh a, [nes_view_y]
    ld [nes_view_armed_y], a

.early_split_apply:
    call nes_video_apply_split_top_map
    ldh a, [nes_split_armed_top_x]
    ldh [rSCX], a
    ldh a, [nes_split_armed_top_y]
    ldh [rSCY], a

    ldh a, [nes_split_line]
    ldh [rLYC], a
    ldh a, [rSTAT]
    or $40
    ldh [rSTAT], a

.early_split_done:

    ; A translated NES NMI may take more than one host GBC frame. Never publish
    ; partially updated NES video state while it is still running; staged OAM,
    ; palette, control, and scroll state are committed atomically on the first
    ; host VBlank after translated RTI clears nes_nmi_active.
    ld a, [nes_nmi_active]
    and a
    jp z, .commit_ready

    ; Keep displaying the last fully completed raster split while the next NES
    ; NMI is still running. Do not publish any new staged state, but do re-arm
    ; the one-shot LYC split so the completed frame remains visually stable.
    ldh a, [nes_split_active]
    and a
    jp nz, .done

.nmi_check_seam:
    ldh a, [nes_seam_active]
    and a
    jp z, .done
    call nes_video_rearm_vertical_seam
    jp .done

.commit_ready:
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_COMMIT
    ld [nes_diag_event_flags], a

    ; Sprite OAM has a hard scanout deadline: once visible lines begin, a
    ; 160-byte hardware-OAM copy can mix two NES metasprite states in one GBC
    ; frame. Publish ONLY OAM before the heavier BG transaction. Palette,
    ; control, scroll, and all BG ordering remain unchanged from the stable
    ; renderer baseline.
    ld a, [nes_oam_dirty]
    and a
    jp z, .oam_done
    xor a
    ld [nes_oam_dirty], a

    ldh a, [nes_oam_shadow_ready]
    and a
    jp nz, .oam_shadow_ready
    call nes_video_build_oam_shadow
.oam_shadow_ready:
    call nes_video_sync_oam
.oam_done:

    ; SMB's stitched BG publication can run well past the line-32 HUD split.
    ; While a game-authored split is armed, allow only STAT to preempt this
    ; long section. Mask VBlank itself so this ISR cannot recursively re-enter
    ; if publication crosses another host frame. EI takes effect after the NOP.
    ldh a, [nes_split_active]
    and a
    jr z, .bg_publish
    ld a, $02
    ld [rIE], a
    ei
    nop

.bg_publish:
    ; Preserve the proven background publication order.
    call nes_video_flush_nametable_queue_atomic
    call nes_gbc_try_fast_initial_hstitch
    call nes_video_update_horizontal_stitch

    ; Resume ordinary non-nested VBlank work. If BG publication completed
    ; before LYC, the still-armed STAT source will fire normally after RETI.
    di
    ld a, $03
    ld [rIE], a

    ldh a, [nes_palette_dirty]
    and a
    jp z, .palette_done
    xor a
    ldh [nes_palette_dirty], a
    call nes_video_sync_palette_shadow
.palette_done:

    ; Commit display-control and scroll state only on a host frame boundary.
    ; This prevents partial $2005 pairs / mid-scan PPUCTRL writes from tearing
    ; the entire GBC viewport.
    ldh a, [nes_ctrl_dirty]
    and a
    jp z, .ctrl_done
    xor a
    ldh [nes_ctrl_dirty], a

    ; Commit PPUCTRL.4 only from a completed NES frame.  Ignore transient
    ; mid-NMI toggles that return to the already-published bank.
    ld a, [nes_ppuctrl]
    and $10
    srl a
    ld b, a
    ld a, [nes_bg_pattern_committed]
    cp b
    jr z, .ctrl_bank_done
    ld a, b
    ld [nes_bg_pattern_committed], a
    call nes_video_toggle_bg_pattern_bank
.ctrl_bank_done:
    ; While a raster split owns map selection, a global PPUCTRL commit must not
    ; transiently seize LCDC.3 after STAT already switched to the playfield.
    ; nes_video_update_ctrl clears/recomputes both sprite-size and map bits;
    ; during an active split update only sprite-size bit 2 and preserve the live
    ; map bit. This removes the one-scanline $9C00->$9800->$9C00 ghosts seen in
    ; the post-fix video logs.
    ldh a, [nes_split_active]
    and a
    jr z, .ctrl_update_global

    ldh a, [rLCDC]
    and $FB
    ld b, a
    ld a, [nes_ppuctrl]
    bit 5, a
    jr z, .ctrl_update_split_store
    ld a, b
    or $04
    ld b, a
.ctrl_update_split_store:
    ld a, b
    ldh [rLCDC], a
    jr .ctrl_update_done

.ctrl_update_global:
    call nes_video_update_ctrl
.ctrl_update_done:

    ; A completed PPUCTRL commit may change LCDC's map bit. During a captured
    ; raster split, restore whichever half of the split currently owns scanout:
    ; top/HUD while LYC is still armed, bottom/playfield after STAT consumed it.
    ldh a, [nes_split_active]
    and a
    jr z, .ctrl_done
    ldh a, [rSTAT]
    bit 6, a
    jr nz, .ctrl_reassert_top
    call nes_video_apply_split_bottom_map
    jr .ctrl_done
.ctrl_reassert_top:
    call nes_video_apply_split_top_map
.ctrl_done:

    ld a, [nes_mask_dirty]
    and a
    jr z, .mask_done
    xor a
    ld [nes_mask_dirty], a
    call nes_video_update_mask
.mask_done:

    ; A proven HUD/playfield split is persistent display state, not merely a
    ; reaction to a fresh $2005 write. The LYC source is one-shot, so once a
    ; split has been detected it must be re-armed on every presented host frame.
    ldh a, [nes_split_active]
    and a
    jp nz, .scroll_split

    ; Ordinary single-scroll games need a fresh calculation when the NES
    ; writes a complete $2005 pair or the follow camera moves. A previously
    ; armed vertical seam must also be re-armed every host frame because the
    ; STAT source is one-shot.
    ldh a, [nes_scroll_dirty]
    and a
    jr nz, .scroll_single
    ldh a, [nes_seam_active]
    and a
    jp z, .scroll_done
    call nes_video_rearm_vertical_seam
    jp .scroll_done

.scroll_single:
    xor a
    ldh [nes_scroll_dirty], a
    call nes_video_apply_single_scroll
    jp .scroll_done

.scroll_split:
    ; A true game-authored raster split takes precedence over the synthetic
    ; single-scroll vertical nametable seam.
    xor a
    ldh [nes_seam_active], a

    ; The raster trigger was armed at ISR entry. Do not switch back to the top
    ; map here if publication has already overrun line 32; that was the source
    ; of whole-frame HUD-map ghosts. Just consume the fresh scroll notification.
    xor a
    ldh [nes_scroll_dirty], a
.scroll_done:

    ; This host frame was presented from a completed NES state, so it may
    ; also become the next translated NES NMI event.
    ld a, $01
    ld [nes_host_vblank_pending], a
.done:
    pop hl
    pop de
    pop bc
    pop af
    reti

; Fast path for the one initial SMB stitch state proven by the .mvl traces:
; mapper 0, 32 KiB PRG, vertical mirroring, first stitch activation, coarse
; world key 0. At that exact point the desired 30-row stitched surface is
; byte-for-byte the already-correct $9800 map in both VRAM banks. Copy it
; directly instead of recomputing 960 tile/palette cells one at a time.
nes_gbc_try_fast_initial_hstitch:
    ld a, [nes_hstitch_valid]
    and a
    ret nz
    ld a, [nes_mapper]
    and a
    ret nz
    ld a, [nes_mirroring]
    cp $01
    ret nz
    ld a, [nes_prg_16k_mirror]
    and a
    ret nz
    ldh a, [nes_split_active]
    and a
    ret z

    ; Compute the same 0..63 coarse world key as
    ; nes_video_update_horizontal_stitch and accept only key 0.
    ldh a, [nes_split_bottom_x]
    ld b, a
    ldh a, [nes_view_x]
    add b
    ld c, a
    ld b, $00
    jr nc, .key_no_carry
    inc b
.key_no_carry:
    ldh a, [nes_split_bottom_ctrl]
    and $01
    xor b
    and $01
    swap a
    add a
    ld b, a
    ld a, c
    srl a
    srl a
    srl a
    or b
    ret nz

    ; Keep the old full-rebuild diagnostics/state accounting. Only the inner
    ; implementation changes from per-cell reconstruction to an exact map copy.
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_FULL_REBUILD
    ld [nes_diag_event_flags], a
    ld a, [nes_hstitch_full_rebuilds]
    inc a
    ld [nes_hstitch_full_rebuilds], a
    xor a
    ld [nes_hstitch_key], a

    ; This is still the proven LCD-off publication boundary. The copy is only
    ; 960 bytes per bank and does no palette derivation or VRAM-mode waiting.
    ldh a, [rLCDC]
    ld [nes_saved_lcdc], a
    and $7F
    ldh [rLCDC], a

    xor a
    ldh [rVBK], a
    ld hl, $9800
    ld de, $9C00
    ld bc, $03C0
    call nes_video_copy

    ld a, $01
    ldh [rVBK], a
    ld hl, $9800
    ld de, $9C00
    ld bc, $03C0
    call nes_video_copy

    ; Match the scratch/result state left by the original 32-column rebuild.
    xor a
    ldh [rVBK], a
    ld [nes_hstitch_dirty], a
    ld [nes_hstitch_copy_len], a
    ldh a, [nes_split_bottom_ctrl]
    and $10
    srl a
    ld [nes_hstitch_copy_skip], a
    ld a, $20
    ld [nes_hstitch_copy_start], a
    ld a, $01
    ld [nes_hstitch_valid], a

    ld a, [nes_saved_lcdc]
    ldh [rLCDC], a
    ret

nes_gbc_stat_isr:
    push af
    push bc

    ldh a, [nes_split_active]
    and a
    jr z, .check_vertical_seam

    ; One-shot lower/playfield scroll for a captured two-state NES raster split.
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_STAT_SPLIT
    ld [nes_diag_event_flags], a
    call nes_video_apply_split_bottom_map

    ldh a, [nes_split_armed_x]
    ld b, a
    ld a, [nes_view_armed_x]
    add b
    ldh [rSCX], a

    ldh a, [nes_split_armed_y]
    ld b, a
    ld a, [nes_view_armed_y]
    add b
    ldh [rSCY], a
    jr .disable_stat

.check_vertical_seam:
    ldh a, [nes_seam_active]
    and a
    jr z, .disable_stat

    ; Synthetic NES Y=240 seam: switch to the vertically adjacent logical
    ; nametable and compensate for the CGB map's extra 16 pixel rows.
    ldh a, [nes_seam_bottom_ctrl]
    call nes_video_apply_map_select_a
    ldh a, [nes_seam_bottom_y]
    ldh [rSCY], a

.disable_stat:
    ; Disable the LYC source until the next VBlank arms another split.
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a

    pop bc
    pop af
    reti

; Record the host frame that just completed into C800-C80F.
; Four records of four bytes:
;   +0 frame serial
;   +1 state bits: 0=NMI active, 1=split active, 2=stitch valid,
;      3=stitch dirty, 4=NT queue nonempty, 5=BG+OBJ rendering enabled,
;      6=ctrl dirty, 7=scroll dirty
;   +2 horizontal-stitch key
;   +3 event bits (NES_DIAG_EVENT_* above)
; C8FE points to the next slot to be written, so the newest record is the
; preceding slot modulo four.
nes_diag_snapshot_frame:
    ld a, [nes_diag_ring_index]
    and $03
    add a
    add a
    add LOW(nes_diag_ring)
    ld l, a
    ld h, HIGH(nes_diag_ring)

    ld a, [nes_diag_frame_serial]
    inc a
    ld [nes_diag_frame_serial], a
    ld [hli], a

    xor a
    ld b, a

    ld a, [nes_nmi_active]
    and a
    jr z, .diag_state_split
    set 0, b
.diag_state_split:
    ldh a, [nes_split_active]
    and a
    jr z, .diag_state_valid
    set 1, b
.diag_state_valid:
    ld a, [nes_hstitch_valid]
    and a
    jr z, .diag_state_dirty
    set 2, b
.diag_state_valid:
.diag_state_dirty:
    ld a, [nes_hstitch_dirty]
    and a
    jr z, .diag_state_queue
    set 3, b
.diag_state_queue:
    ld a, [nes_nametable_queue_ptr_hi]
    cp $D8
    jr nz, .diag_queue_nonempty
    ld a, [nes_nametable_queue_ptr_lo]
    and a
    jr z, .diag_state_render
.diag_queue_nonempty:
    set 4, b
.diag_state_render:
    ld a, [nes_ppumask]
    and $18
    cp $18
    jr nz, .diag_state_ctrl
    set 5, b
.diag_state_ctrl:
    ldh a, [nes_ctrl_dirty]
    and a
    jr z, .diag_state_scroll
    set 6, b
.diag_state_scroll:
    ldh a, [nes_scroll_dirty]
    and a
    jr z, .diag_state_store
    set 7, b
.diag_state_store:
    ld a, b
    ld [hli], a

    ld a, [nes_hstitch_key]
    ld [hli], a
    ld a, [nes_diag_event_flags]
    ld [hl], a

    ld a, [nes_diag_ring_index]
    inc a
    and $03
    ld [nes_diag_ring_index], a

    xor a
    ld [nes_diag_event_flags], a
    ret

Start:
    di
    ld sp, $D000

    ; Request CGB double-speed mode.
    ld a, $01
    ldh [rKEY1], a
    stop

    ; Match NES overlap ordering: in CGB mode, OPRI=0 gives priority by
    ; OAM index rather than DMG-style X-coordinate priority.
    xor a
    ldh [rOPRI], a

    ; Canonical power-on state used by the recompiled 6502.
    xor a
    ldh [nes_a], a
    ldh [nes_x], a
    ldh [nes_y], a
    ld [nes_ppu_status], a
    ld [nes_ppuctrl], a
    ld [nes_ppumask], a
    ld [nes_oamaddr], a
    ld [nes_ppu_scroll_x], a
    ld [nes_ppu_scroll_y], a
    ld [nes_ppu_addr_hi], a
    ld [nes_ppu_addr_lo], a
    ld [nes_ppu_latch], a
    ld [nes_ppu_read_buffer], a
    ld [nes_dac], a
    ld [nes_controller_strobe], a
    ld [nes_controller_shift], a
    ld [nes_host_vblank_pending], a
    ld [nes_nmi_active], a
    ld [nes_oam_dirty], a
    ld [nes_current_code_bank], a
    ld [nes_dispatch_cache_valid], a
    ld [nes_nametable_queue_ptr_lo], a
    ld [nes_nametable_queue_overflow], a
    ld [nes_mask_dirty], a
    ld a, $D8
    ld [nes_nametable_queue_ptr_hi], a
    xor a
    ; Follow camera is the default. Start centered until the first OAM
    ; projection acquires a plausible player sprite.
    ld a, $04
    ld [nes_view_mode], a
    ld a, $30
    ldh [nes_view_x], a
    ldh [nes_view_y], a
    ld [nes_view_armed_x], a
    ld [nes_view_armed_y], a
    ld a, $01
    ld [nes_view_follow_enabled], a
    xor a
    ld [nes_view_follow_valid], a
    ld [nes_view_follow_was_valid], a
    ld [nes_view_follow_x], a
    ld [nes_view_follow_y], a
    ld [nes_view_follow_ref_x], a
    ld [nes_view_follow_ref_y], a
    ld [nes_view_follow_best_dist], a
    ld [nes_view_follow_candidate_x], a
    ld [nes_view_follow_candidate_y], a
    ld [nes_view_follow_slot], a
    ld [nes_view_select_prev], a
    ld [nes_bg_pattern_committed], a
    ld [nes_nametable_stage_used], a
    ld [nes_generic_map_rebuild_dirty], a
    ld [nes_hstitch_valid], a
    ld [nes_hstitch_seen], a
    ld [nes_hstitch_dirty], a
    ld [nes_hstitch_key], a
    ld [nes_hstitch_copy_start], a
    ld [nes_hstitch_copy_len], a
    ld [nes_hstitch_copy_skip], a
    ld [nes_hstitch_target_key], a
    ld [nes_hstitch_full_rebuilds], a
    ld [nes_hstitch_catchups], a
    ld [nes_diag_frame_serial], a
    ld [nes_diag_ring_index], a
    ld [nes_diag_event_flags], a

    ld hl, nes_diag_ring
    ld b, $10
.clear_diag_ring:
    ld [hli], a
    dec b
    jr nz, .clear_diag_ring

    ld [nes_ntdiag_min_row], a
    ld [nes_ntdiag_max_row], a
    ld [nes_ntdiag_display_map], a
    ld [nes_ntdiag_tile_count], a
    ld [nes_ntdiag_phys_mask], a
    ld [nes_ntdiag_min_col], a
    ld [nes_ntdiag_max_col], a
    ld [nes_ntdiag_first_hi], a
    ld [nes_ntdiag_first_lo], a
    ld [nes_ntdiag_last_hi], a
    ld [nes_ntdiag_last_lo], a
    ld [nes_ntdiag_commit_serial], a
    ld [nes_split_duplicate_streak], a
    ld [nes_split_retire_grace_used], a
    ldh [nes_reset_count], a
    ldh [nes_fault_hram], a
    ldh [nes_last_indirect_lo], a
    ldh [nes_last_indirect_hi], a
    ldh [nes_fault_pc_lo], a
    ldh [nes_fault_pc_hi], a
    ldh [nes_oam_shadow_ready], a
    ldh [nes_palette_dirty], a
    ldh [nes_scroll_dirty], a
    ldh [nes_ctrl_dirty], a
    ldh [nes_scroll_pair_count], a
    ldh [nes_split_active], a
    ldh [nes_split_top_x], a
    ldh [nes_split_top_y], a
    ldh [nes_split_bottom_x], a
    ldh [nes_split_bottom_y], a
    ldh [nes_split_top_ctrl], a
    ldh [nes_split_bottom_ctrl], a
    ldh [nes_split_armed_x], a
    ldh [nes_split_armed_y], a
    ldh [nes_split_armed_ctrl], a
    ldh [nes_split_pending_x], a
    ldh [nes_split_pending_y], a
    ldh [nes_split_pending_ctrl], a
    ldh [nes_split_armed_top_x], a
    ldh [nes_split_armed_top_y], a
    ldh [nes_split_armed_top_ctrl], a
    ldh [nes_seam_active], a
    ldh [nes_seam_line], a
    ldh [nes_seam_top_ctrl], a
    ldh [nes_seam_bottom_ctrl], a
    ldh [nes_seam_top_y], a
    ldh [nes_seam_bottom_y], a
    ld a, $20
    ldh [nes_split_line], a
    xor a

    ld hl, nes_gbc_palette_shadow
    ld b, $40
.clear_palette_shadow:
    ld [hli], a
    dec b
    jr nz, .clear_palette_shadow

    ld a, $FD
    ldh [nes_sp], a
    ld a, $24
    ldh [nes_p], a
    ; Initial P=$24 has C=0, Z=0, N=0.
    ld a, $01
    ldh [nes_z_shadow], a
    xor a
    ldh [nes_n_shadow], a
    ldh [nes_c_shadow], a

    call nes_generated_init
    call nes_generated_follow_init
    call nes_video_init
    ; Start profiling at the translated NES reset, excluding GBC boot/setup work.
    call nes_profile_reset

    ; Use the real GBC VBlank interrupt only as a one-byte event latch. The
    ; translated NES interrupt is still delivered at compiler-selected safe points.
    xor a
    ldh [rIF], a
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    ld a, $03
    ld [rIE], a
    ei
    jp nes_reset

INCLUDE "io.asm"
INCLUDE "profile.asm"
INCLUDE "cpu.asm"
INCLUDE "ppu.asm"
INCLUDE "video.asm"
INCLUDE "input.asm"
INCLUDE "generated.asm"