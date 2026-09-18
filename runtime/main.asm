; nes2gbc Game Boy Color runtime skeleton.
INCLUDE "hardware.inc"

SECTION "VBlank Vector", ROM0[$0040]
    jp nes_gbc_vblank_isr

SECTION "STAT Vector", ROM0[$0048]
    jp nes_gbc_stat_isr

SECTION "Header Entry", ROM0[$0100]
    nop
    jp Start

; Reserve the Nintendo logo + cartridge header ($0104-$014F) so floating
; ROM0 sections (generated follow/fit init, etc.) cannot land here. rgbfix
; fills this range; without the reserve it warns "-Woverwrite" and destroys
; whatever code the linker parked on top of the logo.
SECTION "Header Logo and Cart", ROM0[$0104]
    ds $4C

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

    ; Before the first SMB stitched surface is ever exposed, spend otherwise
    ; withheld host frames constructing it in hidden $9C00. A handled step
    ; deliberately leaves the previous completed frame completely untouched:
    ; no new split, no new scroll, and no LCD shutdown.
    ld a, [nes_fit_screen]
    and a
    jr nz, .initial_hstitch_prep_done
    call nes_gbc_prepare_initial_hstitch_hidden_step
    and a
    jr z, .initial_hstitch_prep_done
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    jp .done
.initial_hstitch_prep_done:

    ; Arm the HUD/playfield raster state before any potentially long
    ; completed-frame publication. The long BG path below temporarily allows
    ; only STAT to nest so this armed split can still fire exactly at LYC.
    ld a, [nes_fit_screen]
    and a
    jr nz, .early_split_fit
    ldh a, [nes_split_active]
    and a
    jp z, .early_split_done
    jp .early_split_capture

.early_split_fit:
    ; Fit uses its own scaled ring, but once SMB has established the HUD/playfield
    ; split it needs the same persistent "stitched presentation is valid" lifetime
    ; as main.  Do not run the raw $9C00 stitch; just retain valid/seen so a
    ; transient duplicate-only NMI or PPUCTRL page crossing cannot fall into the
    ; generic PPUMASK screen-rebuild path.
    ldh a, [nes_split_active]
    and a
    jp z, .early_split_done
    ld a, $01
    ld [nes_hstitch_valid], a
    ld [nes_hstitch_seen], a

.early_split_capture:
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
    ld a, [nes_fit_screen]
    and a
    jr nz, .early_split_apply_fit

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
    jr .early_split_done

.early_split_apply_fit:
    ; Compute the scaled split line before touching the top/HUD scroll. A host
    ; VBlank interrupt may be serviced late after long translated work; if LY
    ; has already crossed this line, arming LYC now can never fire this frame.
    ; In that case keep/apply the playfield scroll instead of splashing the HUD
    ; backing ring across the rest of the visible frame.
    ldh a, [nes_split_line]
    srl a
    add 12
    cp 144
    jr c, .fit_lyc_value_ok
    ld a, 143
.fit_lyc_value_ok:
    ld d, a

    ldh a, [rLY]
    cp 144
    jr nc, .fit_apply_top
    cp d
    jr c, .fit_apply_top

    ; Missed raster deadline: present the lower/playfield state immediately and
    ; leave STAT disabled until the next host VBlank can arm the split on time.
    ld a, [nes_fit_play_scx]
    ldh [rSCX], a
    ldh a, [nes_split_armed_y]
    srl a
    sub 12
    ldh [rSCY], a
    ldh a, [rSTAT]
    and $BF
    ldh [rSTAT], a
    jr .early_split_done

.fit_apply_top:
    ; 160x120 fit: X uses 5/8 NES scale; Y stays half-scale with 12px bars.
    ldh a, [nes_split_armed_top_x]
    ld c, a
    ld b, $00
    call nes_video_fit_scale_x_bc
    ld a, l
    ldh [rSCX], a
    ldh a, [nes_split_armed_top_y]
    srl a
    sub 12
    ldh [rSCY], a

    ld a, d
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

    ; Wide FIT mirrors the proven 32-column stitch. Give the recycled
    ; far-offscreen column update (dirty=2/3) first use of VBlank before
    ; SMB's staged nametable publication can run into visible scanout. Full
    ; dirty rebuilds are deliberately not attempted here: the SMB offscreen
    ; parser is filtered below and legitimate full rebuilds use their normal
    ; transition path.
    ld a, [nes_fit_screen]
    and a
    jr z, .fit_edge_done
    ld a, [nes_mirroring]
    cp $01
    jr nz, .fit_edge_done
    ldh a, [nes_split_active]
    and a
    jr z, .fit_edge_done
    call nes_video_fit_update_scroll_window
    ld a, [nes_fit_dirty]
    cp $01
    jr z, .fit_full_service
    cp $02
    jr z, .fit_edge_service
    cp $03
    jr nz, .fit_edge_done

.fit_full_service:
    ; A genuine FIT full invalidation (death/area reload or PPUCTRL.4 tileset
    ; change) may begin while SMB temporarily has no split, then survive after
    ; the HUD/playfield split returns. The split path used to service only
    ; entering-column dirty=2/3, leaving dirty=1 stranded forever with the
    ; previous area's patterns still resident. Continue the existing chunked
    ; authoritative rebuild here; do not disable LCD or perform an atomic reset.
    ;
    ; Once SMB has established its fixed HUD, rows 0-1 are HUD-owned and must
    ; not be replaced by the scrolling playfield during a full recompose.
    ld a, [nes_hstitch_seen]
    and a
    jr z, .fit_edge_resume
    ld a, [nes_fit_recompose_my]
    cp $02
    jr nc, .fit_edge_resume
    ld a, $02
    ld [nes_fit_recompose_my], a
    xor a
    ld [nes_fit_mt_mx], a
    jr .fit_edge_resume

.fit_edge_service:
    ; Rows 0-1 are the fixed FIT HUD surface. A recycled playfield column may
    ; reuse the same physical X slot, but it must never replace those two rows.
    ; Start a fresh recycled-column pass at row 2; preserve later progress.
    ld a, [nes_fit_recompose_my]
    and a
    jr nz, .fit_edge_resume
    ld a, $02
    ld [nes_fit_recompose_my], a
.fit_edge_resume:
    ld a, $01
    ld [nes_vram_unlocked], a
    call nes_video_fit_flush_dirty
    xor a
    ld [nes_vram_unlocked], a
.fit_edge_done:

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
    ; FIT SMB already has authoritative virtual nametable state. Publish only
    ; the 21 columns that scanout can actually see; the 11 future backing
    ; columns are coalesced by scaled destination and committed once per
    ; completed NES NMI below.
    ld a, [nes_fit_screen]
    and a
    jr z, .bg_publish_generic
    ld a, [nes_mirroring]
    cp $01
    jr nz, .bg_publish_generic
    ldh a, [nes_split_active]
    and a
    jr z, .bg_publish_generic
    call nes_gbc_fit_smb_flush_visible_queue
    jr .bg_publish_queue_done
.bg_publish_generic:
    call nes_video_flush_nametable_queue_atomic
.bg_publish_queue_done:
    call nes_gbc_fit_smb_service_future

    ; FIT owns its own scaled 32-column ring. Do not call main's raw stitch
    ; updater here: its FIT guard clears nes_hstitch_valid, which drops the
    ; proven SMB presentation exactly at the 160px (one NES nametable) boundary
    ; and lets PPUMASK trigger the LCD-off generic rebuild/white flash.
    ld a, [nes_fit_screen]
    and a
    call z, nes_video_update_horizontal_stitch

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

    ; Commit PPUCTRL.4 only from a completed NES frame. Ignore transient
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
    ; Fit-screen: refresh sprite CHR in bank 1 when PPUCTRL.3 changes.
    ld a, [nes_fit_screen]
    and a
    call nz, nes_video_fit_sync_sprite_chr

    ; Fit-screen owns map select via fit_apply_scroll; do not let SMB split
    ; logic preserve/reassert LCDC.3 independently.
    ld a, [nes_fit_screen]
    and a
    jr nz, .ctrl_update_global

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
    ld a, [nes_fit_screen]
    and a
    jr nz, .ctrl_done
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
    ; Fit-screen: never take the SMB split/hstitch scroll path — it fights the
    ; half-scale identity maps. Keep applying fit single-scroll every frame.
    ld a, [nes_fit_screen]
    and a
    jr nz, .scroll_fit

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

.scroll_fit:
    xor a
    ldh [nes_scroll_dirty], a
    ; When SMB HUD/playfield split is live, early_split+STAT own SCX/SCY.
    ; Still refresh play_scx, pin LCDC.3, and continue chunked dirty flush.
    ldh a, [nes_split_active]
    and a
    jr z, .scroll_fit_single
    ldh a, [rLCDC]
    and $F7
    ldh [rLCDC], a
    call nes_video_fit_update_scroll_window

    ; Preserve recycled-column progress. It is now offset 31/0, eleven whole
    ; columns offscreen, so it can finish incrementally without blocking scanout.
    jp .scroll_done
.scroll_fit_single:
    call nes_video_apply_single_scroll
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
    ldh [nes_host_vblank_pending], a
.done:
    pop hl
    pop de
    pop bc
    pop af
    reti

; First-stitch preparation that never changes the currently presented frame.
; valid=2 means hidden $9C00 is being built; valid=3 means it is complete but
; intentionally held for one more host boundary. valid=1 remains the ordinary
; live stitched state used everywhere else.
;
; Return A=1 while this helper owns the host frame and the caller must RETI
; without publishing new scroll/control state. Return A=0 for the normal path.
nes_gbc_prepare_initial_hstitch_hidden_step:
    ; Only the already-observed SMB hardware shape uses this experiment.
    ld a, [nes_hstitch_seen]
    and a
    jr z, .prep_shape_mapper
    xor a
    ret

.prep_shape_mapper:
    ld a, [nes_mapper]
    and a
    jr z, .prep_shape_mirroring
    xor a
    ret

.prep_shape_mirroring:
    ld a, [nes_mirroring]
    cp $01
    jr z, .prep_shape_prg
    xor a
    ret

.prep_shape_prg:
    ld a, [nes_prg_16k_mirror]
    and a
    jr z, .prep_split_check
    xor a
    ret

.prep_split_check:
    ldh a, [nes_split_active]
    and a
    jr nz, .prep_split_active

    ; If the candidate split disappears before handoff, abandon the hidden
    ; surface. It has never been displayed, so cancellation is harmless.
    ld a, [nes_hstitch_valid]
    cp $02
    jr z, .prep_cancel
    cp $03
    jr z, .prep_cancel
    xor a
    ret

.prep_cancel:
    xor a
    ld [nes_hstitch_valid], a
    ld [nes_hstitch_copy_start], a
    ld [nes_hstitch_dirty], a
    ret

.prep_split_active:
    ld a, [nes_hstitch_valid]
    cp $03
    jr z, .prep_ready
    cp $02
    jp z, .prep_columns
    and a
    jr z, .prep_begin_check_map
    xor a
    ret

.prep_ready:
    ; A completed translated NMI must remain frozen until the next host
    ; boundary, so queue publication and the first real split begin together.
    ld a, [nes_nmi_active]
    and a
    jp nz, .prep_hold
    ld a, $01
    ld [nes_hstitch_valid], a
    xor a
    ret

.prep_begin_check_map:
    ; $9C00 is only hidden if the currently presented physical map is $9800.
    ; Fall back to the old authoritative rebuild for any other geometry.
    ldh a, [rLCDC]
    and $08
    jr z, .prep_begin
    xor a
    ret

.prep_begin:
    ; Freeze the seam geometry at the first proven split. Later coarse motion
    ; is reconciled by the ordinary catch-up path after the hidden map is live.
    ldh a, [nes_split_bottom_x]
    ld b, a
    ldh a, [nes_view_x]
    add b
    ld c, a
    ld b, $00
    jr nc, .prep_key_no_carry
    inc b
.prep_key_no_carry:
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
    ld [nes_hstitch_key], a

    xor a
    ld [nes_hstitch_copy_start], a
    ld [nes_hstitch_dirty], a
    ld a, $02
    ld [nes_hstitch_valid], a

    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_FULL_REBUILD
    ld [nes_diag_event_flags], a
    ld a, [nes_hstitch_full_rebuilds]
    inc a
    ld [nes_hstitch_full_rebuilds], a

.prep_columns:
    ; Four authoritative columns per host VBlank leaves CPU time for the
    ; translated NMI to continue between preparations. Unlike the previous
    ; staged experiment, the old frame never scrolls while this work happens.
    ld a, $04
    ld [nes_hstitch_target_key], a
.prep_column_loop:
    ld a, [nes_hstitch_copy_start]
    cp $20
    jr nc, .prep_complete
    call nes_video_refresh_stitch_column
    ld a, [nes_hstitch_copy_start]
    inc a
    ld [nes_hstitch_copy_start], a
    cp $20
    jr nc, .prep_complete

    ld a, [nes_hstitch_target_key]
    dec a
    ld [nes_hstitch_target_key], a
    jr nz, .prep_column_loop

.prep_hold:
    ld a, $01
    ret

.prep_complete:
    xor a
    ld [nes_hstitch_dirty], a
    ld a, $03
    ld [nes_hstitch_valid], a
    ld a, $01
    ret

nes_gbc_stat_isr:
    push af
    push bc

    ld a, [nes_fit_screen]
    and a
    jr nz, .stat_fit_split

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

.stat_fit_split:
    ldh a, [nes_split_active]
    and a
    jr z, .check_vertical_seam

    ; Fit: playfield scroll only. No map select — identity maps are shared.
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_STAT_SPLIT
    ld [nes_diag_event_flags], a

    ld a, [nes_fit_play_scx]
    ldh [rSCX], a
    ldh a, [nes_split_armed_y]
    srl a
    sub 12
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

; FIT SMB queue publisher. The NES WRAM nametables are already authoritative
; when an address is staged. During the active SMB split, compose only output
; columns 0..20 that can be scanned this frame; offsets 21..31 are future
; backing and are intentionally left for the recycled-column builder.
nes_gbc_fit_smb_flush_visible_queue:
    ld a, [nes_nametable_queue_ptr_hi]
    cp $D8
    jr nz, .fitq_has_entries
    ld a, [nes_nametable_queue_ptr_lo]
    and a
    ret z

.fitq_has_entries:
    ld a, [nes_diag_event_flags]
    or NES_DIAG_EVENT_QUEUE_FLUSH
    ld [nes_diag_event_flags], a

    ld a, $01
    ld [nes_vram_unlocked], a
    ldh [rSVBK], a
    ld de, nes_nametable_queue

.fitq_loop:
    ld a, [nes_nametable_queue_ptr_hi]
    cp d
    jr nz, .fitq_read
    ld a, [nes_nametable_queue_ptr_lo]
    cp e
    jr z, .fitq_done

.fitq_read:
    ld a, [de]
    inc de
    ld l, a
    ld a, [de]
    inc de
    ld h, a

    ; Current FIT SMB attribute writes are intentionally ignored by the normal
    ; split path too. Skip them here instead of manufacturing dirty/full rebuilds.
    ld a, h
    and $03
    cp $03
    jr c, .fitq_tile
    ld a, l
    cp $C0
    jr nc, .fitq_loop

.fitq_tile:
    push de
    call nes_gbc_fit_smb_publish_backing_hl
    pop de
    jr .fitq_loop

.fitq_done:
    xor a
    ld [nes_vram_unlocked], a
IF DEF(NES2GBC_DEBUG_TRACE)
    ld a, [nes_ntdiag_commit_serial]
    inc a
    ld [nes_ntdiag_commit_serial], a
ENDC
    xor a
    ld [nes_nametable_queue_ptr_lo], a
    ld [nes_nametable_queue_overflow], a
    ld a, $D8
    ld [nes_nametable_queue_ptr_hi], a
    xor a
    ldh [rVBK], a
    ret

; HL = authoritative physical NES tile address. Publish only the scaled output
; cells touched by this source tile that are inside the 21-column scan window.
nes_gbc_fit_smb_publish_visible_hl:
    ; Source NES tile row -> FIT row.
    ld a, h
    and $03
    add a
    add a
    add a
    ld b, a
    ld a, l
    and $E0
    rrca
    rrca
    rrca
    rrca
    rrca
    and $07
    add b
    srl a
    cp 15
    ret nc
    ld [nes_fit_mt_my], a

    ; Vertical mirroring: physical page 1 is source world columns 32..63.
    ld a, l
    and $1F
    ld c, a
    ld a, h
    and $04
    jr z, .fitq_src_x_ready
    ld a, c
    or $20
    ld c, a
.fitq_src_x_ready:

    ; host_x = source_tile_x * 5. One NES tile can touch at most two GBC cells.
    ld d, $00
    ld e, c
    ld h, d
    ld l, e
    add hl, hl
    add hl, hl
    add hl, de
    ld a, l
    and $07
    ld e, a
    srl h
    rr l
    srl h
    rr l
    srl h
    rr l
    ld a, l
    cp 40
    jr c, .fitq_dest0_ready
    sub 40
.fitq_dest0_ready:
    ld d, a

    ; Save the optional second cell as a viewport offset, or $FF if offscreen.
    ld a, $FF
    ld [nes_fit_mt_quad + 3], a
    ld a, e
    cp 4
    jr c, .fitq_second_done
    ld a, d
    inc a
    cp 40
    jr c, .fitq_second_world_ready
    sub 40
.fitq_second_world_ready:
    ld b, a
    ld a, [nes_fit_origin_mx]
    ld c, a
    ld a, b
    sub c
    jr nc, .fitq_second_delta_ready
    add 40
.fitq_second_delta_ready:
    cp 21
    jr nc, .fitq_second_done
    ld [nes_fit_mt_quad + 3], a
.fitq_second_done:

    ; First cell, only if it is actually in the 160px + partial-edge scan window.
    ld a, [nes_fit_origin_mx]
    ld c, a
    ld a, d
    sub c
    jr nc, .fitq_first_delta_ready
    add 40
.fitq_first_delta_ready:
    cp 21
    jr nc, .fitq_after_first
    ld [nes_fit_mt_mx], a
    call nes_video_fit_publish_at_mx_my
.fitq_after_first:
    ld a, [nes_fit_mt_quad + 3]
    cp $FF
    ret z
    ld [nes_fit_mt_mx], a
    jp nes_video_fit_publish_at_mx_my

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
    ldh [nes_host_vblank_pending], a
    ld [nes_vram_unlocked], a
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
    xor a
    ld [nes_fit_screen], a
    call nes_gbc_fit_smb_future_init
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
    ld [nes_reset_count], a
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
    call nes_generated_fit_init
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
INCLUDE "fit_smb_future.asm"
INCLUDE "input.asm"
INCLUDE "generated.asm"