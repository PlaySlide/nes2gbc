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
p = Path(video)
s = p.read_text()

# Restore the ordinary bounded-by-BC copy helper. The second pass inserted a
# FIT-specific guard into this generic primitive; that coincides with the
# complete loss of fit BG publication in the new logs.
start = s.index("; All intentional callers copy exactly one converted 4 KiB pattern table to\n")
end = s.index("; Publish every nametable address staged by the completed translated NES NMI.\n", start)
s = s[:start] + '''nes_video_copy:
.loop:
    ld a, [hli]
    ld [de], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .loop
    ret

''' + s[end:]

# Roll back only the second-pass BC preservation experiment. The first-pass
# X/Y resume state stays intact.
wrapped = '''    push bc
    call nes_video_fit_publish_at_mx_my
    pop bc
'''
count = s.count(wrapped)
if count != 2:
    raise SystemExit(f"restore fit publish calls: expected 2 matches, got {count}")
s = s.replace(wrapped, '''    call nes_video_fit_publish_at_mx_my
''')

# Restore the conservative first-pass sprite-PT rule: never perform the 4 KiB
# swap with LCD scanout active. This was the state where DKC stopped hanging
# and DK Jr became stable; the forced LCD-off swap in pass two reintroduced the
# crash (the new BF log ends with LCDC=0 and zero scanlines).
sprite_start = s.index("nes_video_fit_sync_sprite_chr:\n")
s = s[:sprite_start] + '''nes_video_fit_sync_sprite_chr:
    ld a, [nes_fit_screen]
    and a
    ret z
    ld a, [nes_ppuctrl]
    and $08
    ld b, a
    ld a, [nes_fit_sprite_pt]
    cp b
    ret z

    ; A 4 KiB CPU copy cannot fit in one host VBlank. The old path continued
    ; through active scanout, producing hundreds of bank-1 VRAM flushes in the
    ; DK video log and transient/missing sprite tiles. Until the swap is made
    ; properly chunked/HDMA, only perform it while the physical LCD is already
    ; off. Keeping the previous complete PT is preferable to displaying a
    ; half-copied one.
    ldh a, [rLCDC]
    bit 7, a
    ret nz

    ld a, [nes_vram_unlocked]
    ld c, a
    ld a, $01
    ld [nes_vram_unlocked], a
    push bc

    ld a, [nes_chr_gbc_bank_base]
    ld b, a
    ld a, [nes_chr_bank]
    add b
    ld [$2000], a
    call nes_video_fit_upload_sprite_chr
    call nes_restore_code_bank

    pop bc
    ld a, c
    ld [nes_vram_unlocked], a
    ret
'''

p.write_text(s)
