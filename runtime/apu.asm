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
; Coarse NES-style length counters (pulse1/pulse2/triangle/noise).
nes_apu_len_p1:     ds 1
nes_apu_len_p2:     ds 1
nes_apu_len_tri:    ds 1
nes_apu_len_noi:    ds 1
; Frame sequencer divider (host VBlank ticks).
nes_apu_frame_div:  ds 1

SECTION "NES APU code", ROM0

; ---------------------------------------------------------------------------
nes_apu_init:
    ld hl, nes_apu_regs
    ld b, $18 + 7          ; regs + prev/idx/lens/frame_div
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

;; ---------------------------------------------------------------------------
; Pulse 1 → Square 1 (NR10–NR14)
; ---------------------------------------------------------------------------
nes_apu_update_pulse1:
    ld a, [nes_apu_write_idx]
    cp $03
    jr nz, .no_preload
    ld a, [nes_apu_regs + $03]
    ld hl, nes_apu_len_p1
    call nes_apu_load_length
.no_preload:
    ld a, [nes_apu_regs + $15]
    and $01
    jr nz, .enabled
    xor a
    ldh [rNR12], a
    ret

.enabled:
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
    ld a, [nes_apu_write_idx]
    cp $07
    jr nz, .no_preload
    ld a, [nes_apu_regs + $07]
    ld hl, nes_apu_len_p2
    call nes_apu_load_length
.no_preload:
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
    ld a, [nes_apu_write_idx]
    cp $0B
    jr nz, .no_preload
    ld a, [nes_apu_regs + $0B]
    ld hl, nes_apu_len_tri
    call nes_apu_load_length
.no_preload:
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

    call nes_apu_timer_to_period  ; DE = GB "n" (same mapping as pulse)
    ; Drop one more octave for triangle: p=2048-n; p*=2; n=2048-p.
    ld a, e
    or d
    jr z, .tri_max_p
    xor a
    sub e
    ld e, a
    ld a, $08
    sbc d
    ld d, a                       ; DE = p
    sla e
    rl d
    jr c, .tri_p_cap
    ld a, d
    cp $08
    jr c, .tri_p_ok
.tri_p_cap:
    ld de, $07FF
.tri_p_ok:
    xor a
    sub e
    ld e, a
    ld a, $08
    sbc d
    and $07
    ld d, a                       ; DE = new n
    jr .tri_have_period
.tri_max_p:
    ld de, $0000                  ; p was 2048 → n 0 after octave drop clamp
.tri_have_period:
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
    ld a, [nes_apu_write_idx]
    cp $0F
    jr nz, .no_preload
    ld a, [nes_apu_regs + $0F]
    ld hl, nes_apu_len_noi
    call nes_apu_load_length
.no_preload:
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
    ; NES $4000/$4004/$400C:
    ;   bits0-3 volume OR envelope period
    ;   bit4    1=constant volume, 0=envelope
    ;   bit5    length halt / envelope loop
    bit 4, a
    jr z, .envelope
    ; Constant volume: bits0-3 → NR volume, envelope period 0.
    and $0F
    swap a
    ret
.envelope:
    ; Envelope always starts at volume 15 and decays. Bits0-2 → GB period.
    ; (Old code wrongly used the period as the volume nibble, muting music.)
    ld b, a
    and $07
    ld c, a
    ld a, $F0                     ; vol 15, decrease
    or c
    bit 5, b                      ; loop/halt: keep a slow decay vs silence
    ret z
    ; Sustained notes often set halt+envelope; prefer audible sustain.
    ld a, $F0
    ret

; ---------------------------------------------------------------------------
; Input:  BC = NES 11-bit timer t
; Output: DE = GBC period n = 2048 - min(2047, ((t+1)*75)/16), in 0..2047
; Clobbers: AF, HL, BC
;
; Base ratio is (t+1)*75/64 ≈ NES→GB square. SMB listening was ~2 octaves
; sharp, so we *4 the period length afterward (effective /16).
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

    ; Empirical: first listen was ~2 octaves sharp vs NES, so lengthen
    ; the GBC period by 4 (drop two octaves) before clamping.
    sla e
    rl d
    jr c, .period_overflow
    sla e
    rl d
    jr nc, .period_scaled
.period_overflow:
    ld de, $07FF
    jr .capped
.period_scaled:

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


; ---------------------------------------------------------------------------
; A = length/freq hi register value; HL -> length counter byte
; Loads NES length-table entry from bits 3-7.
; ---------------------------------------------------------------------------
nes_apu_load_length:
    rrca
    rrca
    rrca
    and $1F
    ld e, a
    ld d, 0
    push hl
    ld hl, nes_apu_length_table
    add hl, de
    ld a, [hl]
    pop hl
    ld [hl], a
    ret

; ---------------------------------------------------------------------------
; Coarse frame tick from host VBlank (~60Hz). NES clocks length at 120Hz
; (quarter frames); two decrements per VBlank is a usable approximation.
; ---------------------------------------------------------------------------
nes_apu_frame_tick:
    ; Pulse1
    ld a, [nes_apu_regs + $00]
    bit 5, a
    jr nz, .p2
    ld a, [nes_apu_len_p1]
    and a
    jr z, .p1_silent
    dec a
    ld [nes_apu_len_p1], a
    jr nz, .p2
.p1_silent:
    xor a
    ldh [rNR12], a
.p2:
    ld a, [nes_apu_regs + $04]
    bit 5, a
    jr nz, .tri
    ld a, [nes_apu_len_p2]
    and a
    jr z, .p2_silent
    dec a
    ld [nes_apu_len_p2], a
    jr nz, .tri
.p2_silent:
    xor a
    ldh [rNR22], a
.tri:
    ld a, [nes_apu_regs + $08]
    bit 7, a                      ; triangle length halt is bit7 of $4008
    jr nz, .noi
    ld a, [nes_apu_len_tri]
    and a
    jr z, .tri_silent
    dec a
    ld [nes_apu_len_tri], a
    jr nz, .noi
.tri_silent:
    xor a
    ldh [rNR30], a
.noi:
    ld a, [nes_apu_regs + $0C]
    bit 5, a
    ret nz
    ld a, [nes_apu_len_noi]
    and a
    jr z, .noi_silent
    dec a
    ld [nes_apu_len_noi], a
    ret nz
.noi_silent:
    xor a
    ldh [rNR42], a
    ret

; Official NES length counter table (bits 3-7 of $4003/$4007/$400B/$400F).
nes_apu_length_table:
    db 10,254, 20,  2, 40,  4, 80,  6, 160,  8, 60, 10, 14, 12, 26, 14
    db 12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30

; NES noise period index 0..F → rough NR43 encoding (lower = higher pitch).
nes_apu_noise_nr43:
    ; Rough NR43 encoding; +1 clock shift from first draft after pitch fix.
    db $F7, $E3, $D3, $C3, $B3, $A3, $93, $83
    db $73, $63, $53, $43, $33, $23, $13, $03
