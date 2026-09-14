from pathlib import Path


def replace_once(path, old, new, label):
    p = Path(path)
    s = p.read_text()
    n = s.count(old)
    if n != 1:
        raise SystemExit(f"{label}: expected exactly 1 match, got {n}")
    p.write_text(s.replace(old, new, 1))


main = "runtime/main.asm"
early = '''    ; FIT_SCREEN background composition used to run only after the staged
    ; nametable transaction and the rest of the VBlank publication work. On
    ; SMB there was often no VBlank left by then: origin_mx advanced and SCX
    ; panned, but the entering identity column never reached VRAM. Service
    ; carried fit work here, near the front of VBlank, from completed NES state.
    ld a, [nes_fit_screen]
    and a
    jr z, .fit_early_flush_done
    call nes_video_fit_update_scroll_window
    ld a, [nes_fit_dirty]
    and a
    jr z, .fit_early_flush_done
    ld a, $01
    ld [nes_vram_unlocked], a
    call nes_video_fit_flush_dirty
    xor a
    ld [nes_vram_unlocked], a
.fit_early_flush_done:

'''
replace_once(main, early, "", "remove early fit flush")

video = "runtime/video.asm"
copy_now = '''; All intentional callers copy exactly one converted 4 KiB pattern table to
; $8000. Keep that invariant explicit. The Balloon Fight Game A video log
; showed a runaway sequential write walking from $8000 all the way through
; $9FFF before the native runtime fell over. In FIT_SCREEN there is never a
; legitimate bank-0 call to this helper (fit BG tiles are composed, not copied),
; so reject that shape and leave a breadcrumb at C82D. Also stop at $9000 even
; if BC is ever corrupted so a bad copy cannot eat the GBC BG map.
nes_video_copy:
    ld a, [nes_fit_screen]
    and a
    jr z, .loop
    ldh a, [rVBK]
    and $01
    jr nz, .loop
    ld a, $C2
    ld [nes_debug_fault], a
    ret
.loop:
    ld a, d
    cp $90
    jr nc, .overflow
    ld a, [hli]
    ld [de], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ret
.overflow:
    ld a, $C3
    ld [nes_debug_fault], a
    ret
'''
copy_first = '''nes_video_copy:
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
replace_once(video, copy_now, copy_first, "restore raw video copy")

p = Path(video)
s = p.read_text()
wrapped = '''    push bc
    call nes_video_fit_publish_at_mx_my
    pop bc
'''
count = s.count(wrapped)
if count != 2:
    raise SystemExit(f"restore fit publish calls: expected 2 matches, got {count}")
s = s.replace(wrapped, '''    call nes_video_fit_publish_at_mx_my
''')
p.write_text(s)

sprite_now = '''    ; A 4 KiB sprite-PT swap cannot be allowed to run into active scanout.
    ; The previous safety patch simply refused the swap while LCD was on, which
    ; stopped DK/DK Jr corruption but left sprites that genuinely use the new
    ; pattern table (notably Kong in DKC) missing. Do the rare whole-PT swap in
    ; a short LCD-off window instead. This is a raw 4 KiB copy, not the very
    ; expensive 2 KiB authoritative map rebuild that used to hold LCDC.7 low
    ; for many host frames.
    ldh a, [rLCDC]
    ld [nes_saved_lcdc], a
    bit 7, a
    jr z, .sprite_lcd_safe
    call nes_video_wait_oam
    ldh a, [rLCDC]
    and $7F
    ldh [rLCDC], a
.sprite_lcd_safe:
'''
sprite_first = '''    ; A 4 KiB CPU copy cannot fit in one host VBlank. The old path continued
    ; through active scanout, producing hundreds of bank-1 VRAM flushes in the
    ; DK video log and transient/missing sprite tiles. Until the swap is made
    ; properly chunked/HDMA, only perform it while the physical LCD is already
    ; off. Keeping the previous complete PT is preferable to displaying a
    ; half-copied one.
    ldh a, [rLCDC]
    bit 7, a
    ret nz
'''
replace_once(video, sprite_now, sprite_first, "restore conservative sprite PT guard")

restore_lcdc = '''
    ld a, [nes_saved_lcdc]
    ldh [rLCDC], a
'''
replace_once(video, restore_lcdc, "", "remove second-pass LCD restore")
