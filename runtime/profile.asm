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
nes_profile_read_ram:        ds 4 ; C8B0
nes_profile_read_ppu:        ds 4 ; C8B4
nes_profile_read_io:         ds 4 ; C8B8
nes_profile_read_prg:        ds 4 ; C8BC
nes_profile_read_other:      ds 4 ; C8C0
nes_profile_write_ram:       ds 4 ; C8C4
nes_profile_write_ppu:       ds 4 ; C8C8
nes_profile_write_io:        ds 4 ; C8CC
nes_profile_write_mapper:    ds 4 ; C8D0
nes_profile_write_other:     ds 4 ; C8D4
nes_profile_trace_index:     ds 1 ; C8D8, next 16-bit slot (0-127)
nes_profile_oam_dma_fast:    ds 4 ; C8D9
nes_profile_oam_dma_generic: ds 4 ; C8DD
nes_profile_vram_wait_spin:  ds 4 ; C8E1
nes_profile_oam_wait_spin:   ds 4 ; C8E5
nes_profile_end:

SECTION "NES profile block trace", WRAM0[$CA00]
; 128 little-endian NES PCs = 256-byte rolling trace.
nes_profile_trace_buffer: ds $100

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

; Input HL = translated NES basic-block PC. Preserve all host registers.
nes_profile_trace_pc:
IF DEF(NES2GBC_PROFILE)
    ld d, h
    ld e, l
    push af
    push bc
    push de
    push hl

    ld a, [nes_profile_trace_index]
    and $7F
    ld c, a
    add a
    ld l, a
    ld h, $CA
    ld a, e
    ld [hli], a
    ld a, d
    ld [hl], a

    ld a, c
    inc a
    and $7F
    ld [nes_profile_trace_index], a

    pop hl
    pop de
    pop bc
    pop af
ENDC
    ret

nes_profile_reset:
IF DEF(NES2GBC_PROFILE)
    xor a
    ld hl, nes_profile_dispatch
    ld bc, nes_profile_end - nes_profile_dispatch
.clear_state:
    xor a
    ld [hli], a
    dec bc
    ld a, b
    or c
    jr nz, .clear_state

    xor a
    ld hl, nes_profile_trace_buffer
    ld bc, $0100
.clear_trace:
    ld [hli], a
    dec bc
    ld a, b
    or c
    jr nz, .clear_trace
ENDC
    ret
