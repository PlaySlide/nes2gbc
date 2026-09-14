from pathlib import Path

p = Path('runtime/video.asm')
s = p.read_text()

# Previous surgical fix: fit rebuilds must never clear LCDC.7. Keep this
# idempotent so the helper can run against either the old baseline or the
# already-fixed branch.
old_rebuild = '''nes_video_rebuild_generic_maps_atomic:
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
new_rebuild = '''nes_video_rebuild_generic_maps_atomic:
    ; FIT_SCREEN composes a 16x15 resident surface and is intentionally
    ; chunked across host VBlanks. Never clear LCDC.7 before that work.
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
'''
if old_rebuild in s:
    s = s.replace(old_rebuild, new_rebuild, 1)
s = s.replace('.rebuild_lcd_off:\n.rebuild_generic:\n\n.rebuild_generic:\n',
              '.rebuild_lcd_off:\n.rebuild_generic:\n', 1)

# Balloon Fight Game A still crashes after the LCD-off rebuild fix. The new
# mVL gives a stronger signature: starting around frame 422, writes march
# sequentially through VRAM $8000-$9FFF for ~4 host frames; at frame 434 the
# same runaway reaches FF40 and writes LCDC=0. Every intentional caller of
# nes_video_copy copies exactly one 4 KiB pattern table to $8000, so $9000 is
# an invariant boundary. Enforce it here. This does not reject fit bank-0
# writes (the bad second-pass experiment did); it merely prevents a clobbered
# copy count from escaping the pattern-table window into BG maps / I/O.
old_copy = '''nes_video_copy:
.loop:
    ld a, [hli]
    ld [de], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ret
'''
new_copy = '''nes_video_copy:
.loop:
    ; All callers target one 4 KiB pattern table at $8000-$8FFF. A corrupt
    ; count must never walk into $9000+ and eventually wrap into hardware I/O.
    ld a, d
    cp $90
    ret nc
    ld a, [hli]
    ld [de], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ret
'''
if old_copy in s:
    s = s.replace(old_copy, new_copy, 1)

if 'cp $90\n    ret nc\n    ld a, [hli]' not in s:
    raise SystemExit('bounded nes_video_copy guard missing after transform')

p.write_text(s)
