; NES APU → GBC sound bridge (v1).
;
; nes_apu_write calling convention (matches cpu.asm $40xx path):
;   L = NES APU register low byte ($00-$17 for $4000-$4017)
;   E = value written
; Clobbers: AF, BC, DE, HL (void return; cpu write path ignores result).
;
; nes_apu_read_status:
;   Output A = $4015-style status (v1: last written enable bits 0-4).

SECTION "NES APU state", WRAM0[$CA00]
; Mirror of $4000-$4017 indexed by low address byte.
nes_apu_regs:       ds $18
; Previous $4015 channel-enable bits (rising-edge trigger detection).
nes_apu_prev_4015:  ds 1
; Low byte of the register currently being written (survives helpers).
nes_apu_write_idx:  ds 1

SECTION "NES APU code", ROM0

; ---------------------------------------------------------------------------
nes_apu_init:
    ld hl, nes_apu_regs
    ld b, $18 + 1          ; regs + prev_4015
    xor a
.clear:
    ld [hli], a
    dec b
    jr nz, .clear

    ; Master APU on, full stereo volume, all pans enabled.
    ld a, $80
    ldh [rNR52], a
    ld a, $77
    ldh [rNR50], a
    ld a, $FF
    ldh [rNR51], a

    ; Classic 32-step triangle (0..F..0) into wave RAM.
    ld hl, rWave
    ld de, .triangle_wave
    ld b, 16
.load_wave:
    ld a, [de]
    inc de
    ld [hli], a
    dec b
    jr nz, .load_wave

    ; Silence channels initially (DAC / envelope off).
    xor a
    ldh [rNR10], a
    ldh [rNR12], a
    ldh [rNR22], a
    ldh [rNR30], a
    ldh [rNR42], a
    ret

.triangle_wave:
    db $01, $23, $45, $67, $89, $AB, $CD, $EF
    db $FE, $DC, $BA, $98, $76, $54, $32, $10

; ---------------------------------------------------------------------------
nes_apu_read_status:
    ld a, [nes_apu_regs + $15]
    and $1F
    ret

; ---------------------------------------------------------------------------
nes_apu_write:
    ld a, l
    cp $18
    ret nc

    ; Shadow store. Base is $CA00 so H=$CA, L=index.
    ld [nes_apu_write_idx], a
    ld h, HIGH(nes_apu_regs)
    ld a, e
    ld [hl], a

    ld a, [nes_apu_write_idx]
    cp $04
    jp c, nes_apu_update_pulse1
    cp $08
    jp c, nes_apu_update_pulse2
    cp $0C
    jp c, nes_apu_update_triangle
    cp $10
    jp c, nes_apu_update_noise
    cp $11
    jr z, .dmc_load
    cp $15
    jp z, nes_apu_update_status
    ; $10/$12/$13 DMC and $17 frame counter: shadow only for v1.
    ret

.dmc_load:
    ld a, e
    ld [nes_dac], a
    ret

; ---------------------------------------------------------------------------
; Pulse 1 → Square 1 (NR10–NR14)
; ---------------------------------------------------------------------------
nes_apu_update_pulse1:
    ld a, [nes_apu_regs + $15]
    and $01
    jr nz, .enabled
    xor a
    ldh [rNR12], a
    ret

.enabled:
    xor a
    ldh [rNR10], a                    ; sweep unused in v1

    ; Duty bits 6-7 of $4000 → NR11 bits 6-7.
    ld a, [nes_apu_regs + $00]
    and $C0
    ldh [rNR11], a

    ld a, [nes_apu_regs + $00]
    call nes_apu_vol_to_nrx2
    ldh [rNR12], a

    ld a, [nes_apu_regs + $02]
    ld c, a
    ld a, [nes_apu_regs + $03]
    and $07
    ld b, a
    call nes_apu_timer_to_period      ; DE = GBC period
    ld a, e
    ldh [rNR13], a

    ld a, d
    and $07
    ld d, a
    ld a, [nes_apu_write_idx]
    cp $03                            ; length/freq hi → trigger
    ld a, d
    jr nz, .write_nr14
    or $80
.write_nr14:
    ldh [rNR14], a
    ret

; ---------------------------------------------------------------------------
; Pulse 2 → Square 2 (NR21–NR24); no sweep register on GB.
; ---------------------------------------------------------------------------
nes_apu_update_pulse2:
    ld a, [nes_apu_regs + $15]
    and $02
    jr nz, .enabled
    xor a
    ldh [rNR22], a
    ret

.enabled:
    ld a, [nes_apu_regs + $04]
    and $C0
    ldh [rNR21], a

    ld a, [nes_apu_regs + $04]
    call nes_apu_vol_to_nrx2
    ldh [rNR22], a

    ld a, [nes_apu_regs + $06]
    ld c, a
    ld a, [nes_apu_regs + $07]
    and $07
    ld b, a
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR23], a

    ld a, d
    and $07
    ld d, a
    ld a, [nes_apu_write_idx]
    cp $07
    ld a, d
    jr nz, .write_nr24
    or $80
.write_nr24:
    ldh [rNR24], a
    ret

; ---------------------------------------------------------------------------
; Triangle → Wave (NR30–NR34)
; ---------------------------------------------------------------------------
nes_apu_update_triangle:
    ld a, [nes_apu_regs + $15]
    and $04
    jr nz, .chan_on
.silent:
    xor a
    ldh [rNR30], a
    ret

.chan_on:
    ld a, [nes_apu_regs + $0A]
    ld c, a
    ld a, [nes_apu_regs + $0B]
    and $07
    ld b, a
    ; Ultrasonic mute when timer < 2.
    ld a, b
    or a
    jr nz, .audible
    ld a, c
    cp $02
    jr c, .silent

.audible:
    ld a, $80
    ldh [rNR30], a
    ld a, $FF
    ldh [rNR31], a

    ; NR32: 100% if linear-counter reload nonzero, else mute.
    ld a, [nes_apu_regs + $08]
    and $7F
    jr z, .vol_mute
    ld a, $20                     ; 100% wave volume
    jr .vol_write
.vol_mute:
    xor a
.vol_write:
    ldh [rNR32], a

    call nes_apu_timer_to_period  ; BC still holds timer
    ld a, e
    ldh [rNR33], a

    ld a, d
    and $07
    ld d, a
    ld a, [nes_apu_write_idx]
    cp $0B
    ld a, d
    jr nz, .write_nr34
    or $80
.write_nr34:
    ldh [rNR34], a
    ret

; ---------------------------------------------------------------------------
; Noise → GB noise (NR41–NR44)
; ---------------------------------------------------------------------------
nes_apu_update_noise:
    ld a, [nes_apu_regs + $15]
    and $08
    jr nz, .enabled
    xor a
    ldh [rNR42], a
    ret

.enabled:
    xor a
    ldh [rNR41], a

    ld a, [nes_apu_regs + $0C]
    call nes_apu_vol_to_nrx2
    ldh [rNR42], a

    ; Period index + LFSR mode from $400E.
    ld a, [nes_apu_regs + $0E]
    ld b, a
    and $0F
    ld e, a
    ld d, 0
    ld hl, nes_apu_noise_nr43
    add hl, de
    ld a, [hl]
    bit 7, b                      ; NES mode → NR43 bit3 (7-bit LFSR)
    jr z, .nr43
    or $08
.nr43:
    ldh [rNR43], a

    ld a, [nes_apu_write_idx]
    cp $0F
    ld a, $00
    jr nz, .write_nr44
    ld a, $80
.write_nr44:
    ldh [rNR44], a
    ret

; ---------------------------------------------------------------------------
; $4015: channel enables. Rising edge reloads/triggers that channel.
; ---------------------------------------------------------------------------
nes_apu_update_status:
    ld a, [nes_apu_prev_4015]
    ld b, a
    ld a, e
    and $1F
    ld c, a
    ld [nes_apu_prev_4015], a

    ; Gate NR51 pans from enable bits 0-3 (duplicate to both nibbles).
    and $0F
    ld d, a
    swap a
    or d
    ldh [rNR51], a

    ; D = newly enabled bits = (new ^ old) & new
    ld a, c
    xor b
    and c
    ld d, a

    ; ---- pulse1 ----
    bit 0, c
    jr nz, .p1_on
    xor a
    ldh [rNR12], a
    jr .p2
.p1_on:
    bit 0, d
    jr z, .p2
    ld a, $03
    ld [nes_apu_write_idx], a
    call nes_apu_update_pulse1
.p2:
    bit 1, c
    jr nz, .p2_on
    xor a
    ldh [rNR22], a
    jr .tri
.p2_on:
    bit 1, d
    jr z, .tri
    ld a, $07
    ld [nes_apu_write_idx], a
    call nes_apu_update_pulse2
.tri:
    bit 2, c
    jr nz, .tri_on
    xor a
    ldh [rNR30], a
    jr .noi
.tri_on:
    bit 2, d
    jr z, .noi
    ld a, $0B
    ld [nes_apu_write_idx], a
    call nes_apu_update_triangle
.noi:
    bit 3, c
    jr nz, .noi_on
    xor a
    ldh [rNR42], a
    ret
.noi_on:
    bit 3, d
    ret z
    ld a, $0F
    ld [nes_apu_write_idx], a
    jp nes_apu_update_noise

; ---------------------------------------------------------------------------
; A = NES vol/env ($4000/$4004/$400C) → A = NRx2
; ---------------------------------------------------------------------------
nes_apu_vol_to_nrx2:
    bit 4, a
    jr z, .envelope
    ; Constant volume: bits0-3 → NR volume, period 0, direction 0.
    and $0F
    swap a
    ret
.envelope:
    ld b, a
    and $0F
    swap a                        ; rough start volume
    bit 3, b                      ; envelope add → direction
    jr z, .period
    or $08
.period:
    ld c, a
    ld a, b
    and $07                       ; period
    or c
    ret

; ---------------------------------------------------------------------------
; Input:  BC = NES 11-bit timer t
; Output: DE = GBC period n = 2048 - min(2047, ((t+1)*75)/64), in 0..2047
; Clobbers: AF, HL, BC
;
; Uses (t+1)*75/64 = (t+1) + ((t+1)*11)/64 so intermediates fit in 16 bits
; (max t+1 = 2048 → max product 22528).
; ---------------------------------------------------------------------------
nes_apu_timer_to_period:
    ; HL = t + 1
    inc bc
    ld l, c
    ld h, b

    push hl                       ; save (t+1)
    ; DE = HL * 11 = HL*8 + HL*2 + HL
    push hl
    add hl, hl
    add hl, hl
    add hl, hl                    ; *8
    ld e, l
    ld d, h
    pop hl
    push hl
    add hl, hl                    ; *2
    ld a, l
    add e
    ld e, a
    ld a, h
    adc d
    ld d, a                       ; *10
    pop hl
    ld a, l
    add e
    ld e, a
    ld a, h
    adc d
    ld d, a                       ; DE = (t+1)*11

    ; DE >>= 6 → ((t+1)*11)/64
    ld b, 6
.shr6:
    srl d
    rr e
    dec b
    jr nz, .shr6

    pop hl                        ; HL = t+1
    ld a, l
    add e
    ld e, a
    ld a, h
    adc d
    ld d, a                       ; DE = (t+1)*75/64

    ; Cap at 2047 ($07FF).
    ld a, d
    cp $08
    jr nc, .cap
    cp $07
    jr c, .capped
    ld a, e
    cp $FF
    jr c, .capped
.cap:
    ld de, $07FF
.capped:
    ; n = 2048 - DE; if DE==0 → clamp to 2047.
    ld a, e
    or d
    jr z, .max_period
    xor a
    sub e
    ld e, a
    ld a, $08
    sbc d
    and $07
    ld d, a
    ret
.max_period:
    ld de, $07FF
    ret

; NES noise period index 0..F → rough NR43 encoding (lower = higher pitch).
nes_apu_noise_nr43:
    db $F7, $F3, $E3, $D3, $C3, $B3, $A3, $93
    db $83, $73, $63, $53, $43, $33, $23, $13
