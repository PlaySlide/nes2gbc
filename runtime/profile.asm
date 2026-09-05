; Optional runtime profiling support. Enabled with PROFILE=1.
; Counters are little-endian 32-bit values in WRAM0 at $C870.

SECTION "NES runtime profile", WRAM0[$C870]
nes_profile_dispatch:        ds 4 ; C870
nes_profile_cpu_read:        ds 4 ; C874
nes_profile_cpu_write:       ds 4 ; C878
nes_profile_ppu_read:        ds 4 ; C87C
nes_profile_ppu_write:       ds 4 ; C880
nes_profile_jsr_push:        ds 4 ; C884
nes_profile_rts_pop:         ds 4 ; C888
nes_profile_adc:             ds 4 ; C88C
nes_profile_sbc:             ds 4 ; C890
nes_profile_compare:         ds 4 ; C894
nes_profile_bit:             ds 4 ; C898
nes_profile_nametable_sync:  ds 4 ; C89C
nes_profile_palette_sync:    ds 4 ; C8A0
nes_profile_oam_sync:        ds 4 ; C8A4
nes_profile_nmi:             ds 4 ; C8A8
nes_profile_chr_upload:      ds 4 ; C8AC
nes_profile_end:

; Inline counter increment. Preserves AF and HL; leaves BC/DE untouched.
; Macro-local labels use RGBDS' unique \@ suffix.
MACRO PROFILE_INC
IF DEF(NES2GBC_PROFILE)
    push af
    push hl
    ld hl, \1
    inc [hl]
    jr nz, .profile_done\@
    inc hl
    inc [hl]
    jr nz, .profile_done\@
    inc hl
    inc [hl]
    jr nz, .profile_done\@
    inc hl
    inc [hl]
.profile_done\@:
    pop hl
    pop af
ENDC
ENDM

SECTION "NES runtime profile helpers", ROM0
nes_profile_reset:
IF DEF(NES2GBC_PROFILE)
    xor a
    ld hl, nes_profile_dispatch
    ld bc, nes_profile_end - nes_profile_dispatch
.clear:
    ld [hli], a
    dec bc
    ld a, b
    or c
    jr nz, .clear
ENDC
    ret
