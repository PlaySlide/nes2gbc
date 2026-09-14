from pathlib import Path

p = Path('runtime/video.asm')
s = p.read_text()
old = '''nes_video_rebuild_generic_maps_atomic:
    ldh a, [rLCDC]
    ld [nes_saved_lcdc], a
    bit 7, a
    jr z, .rebuild_lcd_off

    ; LCD may only be disabled during VBlank.
    call nes_video_wait_oam
    ldh a, [rLCDC]
    and $7F
    ldh [rLCDC], a

.rebuild_lcd_off:
    ld a, [nes_fit_screen]
    and a
    jr z, .rebuild_generic

    ; Fit: one resident recompose instead of 2048 per-byte composes.
    ld a, $01
    ld [nes_fit_dirty], a
    ld [nes_vram_unlocked], a
    call nes_video_fit_flush_dirty
    xor a
    ld [nes_vram_unlocked], a
    jr .rebuild_fit_done
'''
new = '''nes_video_rebuild_generic_maps_atomic:
    ; FIT_SCREEN composes a 16x15 resident surface and is intentionally
    ; chunked across host VBlanks. Never clear LCDC.7 before that work: doing
    ; so leaves Balloon Fight Game A with the physical LCD disabled for the
    ; duration of a very expensive full fit recompose (mVL: $90 -> $10, then
    ; no more scanlines). Schedule a fresh authoritative pass and return; the
    ; normal VBlank fit flusher will publish it incrementally.
    ld a, [nes_fit_screen]
    and a
    jr z, .rebuild_generic_entry
    ld a, $01
    ld [nes_fit_dirty], a
    xor a
    ld [nes_fit_recompose_my], a
    ld [nes_fit_mt_mx], a
    ld a, [nes_ppuctrl]
    and $10
    srl a
    ld [nes_bg_pattern_committed], a
    ret

.rebuild_generic_entry:
    ldh a, [rLCDC]
    ld [nes_saved_lcdc], a
    bit 7, a
    jr z, .rebuild_lcd_off

    ; LCD may only be disabled during VBlank.
    call nes_video_wait_oam
    ldh a, [rLCDC]
    and $7F
    ldh [rLCDC], a

.rebuild_lcd_off:
.rebuild_generic:
'''
count = s.count(old)
if count != 1:
    raise SystemExit(f'expected one rebuild block, found {count}')
s = s.replace(old, new, 1)
p.write_text(s)
