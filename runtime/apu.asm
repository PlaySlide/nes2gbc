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
nes_apu_linear_tri: ds 1   ; working triangle linear counter
; Last written NRx2 (GB volume is write-only; track silence→audible).
nes_apu_last_nr12_p1: ds 1
nes_apu_last_nr12_p2: ds 1
nes_apu_last_nr12_noi: ds 1
; Pulse1 NES sweep working state (do NOT use GB NR10 — it fights our period map).
nes_apu_sweep_div:    ds 1
nes_apu_sweep_period_lo: ds 1
nes_apu_sweep_period_hi: ds 1
nes_apu_sweep_start_lo: ds 1  ; period at bend origin (~1 octave cap)
nes_apu_sweep_start_hi: ds 1
nes_apu_sweep_active: ds 1   ; 1 while pulse1 software sweep running
nes_apu_sweep_muted:  ds 1   ; silenced by sweep unit; no retrigger until $4003
nes_apu_sweep_last:   ds 1   ; last $4001 (detect mid-note mode change)
; Pulse2 software sweep (fireworks/blast etc.)
nes_apu_sweep2_div:    ds 1
nes_apu_sweep2_period_lo: ds 1
nes_apu_sweep2_period_hi: ds 1
nes_apu_sweep2_start_lo: ds 1
nes_apu_sweep2_start_hi: ds 1
nes_apu_sweep2_active: ds 1
; Frame sequencer divider (host VBlank ticks).
nes_apu_frame_div:  ds 1

SECTION "NES APU code", ROM0

; ---------------------------------------------------------------------------
nes_apu_init:
    ld hl, nes_apu_regs
    ld b, $18 + 25         ; regs + prev/idx/lens/linear/lastvol/sweep1+2/frame_div
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
; Only touch the GB regs that correspond to the NES register written.
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
    ld a, [nes_apu_write_idx]
    cp $00
    jr z, .do_vol_duty
    cp $01
    jp z, .do_sweep
    cp $02
    jp z, .do_freq
    cp $03
    jp z, .do_freq
    ret

.do_vol_duty:
    ld a, [nes_apu_regs + $00]
    and $C0
    ldh [rNR11], a
    ld a, [nes_apu_regs + $00]
    call nes_apu_vol_to_nrx2
    ld b, a
    ld a, [nes_apu_last_nr12_p1]
    ld c, a
    ld a, b
    ld [nes_apu_last_nr12_p1], a
    ldh [rNR12], a
    ; If we were silent and now have volume, retrigger so music/SFX ordered
    ; as freq-then-volume become audible. Skip if already audible so mid-note
    ; envelope ticks (jump) do not reset sweep pitch to the bottom.
    ld a, c
    and $F0
    ret nz
    ld a, b
    and $F0
    ret z
    ; Note: do NOT block silence→audible while sweep is on. Flagpole dumps
    ; $4001 before $4000; blocking left NR12 silent with no trigger.
    ; Jump restart-after-mute is prevented by last_nr12=$10 poison instead.
    jp nes_apu_retrigger_p1

.do_sweep:
    ; Always disable GB hardware sweep; we emulate NES sweep in software.
    xor a
    ldh [rNR10], a
    ld a, [nes_apu_regs + $01]
    bit 7, a
    jr nz, .sweep_on
    ; Sweep off ($7F): stop the sweep unit only. Do NOT silence NR12 —
    ; death music Dumps $7F then writes $94 every frame; muting here made
    ; death silent. Jump/SFX end via $4015 or period mute instead.
    ld a, [nes_apu_regs + $01]
    ld [nes_apu_sweep_last], a
    xor a
    ld [nes_apu_sweep_active], a
    ld [nes_apu_sweep_muted], a
    ret
.sweep_on:
    ld a, [nes_apu_regs + $01]
    ld b, a
    rrca
    rrca
    rrca
    rrca
    and $07
    ld [nes_apu_sweep_div], a
    ld a, [nes_apu_sweep_last]
    ld c, a                       ; C = previous $4001
    bit 7, a
    ld a, b
    ld [nes_apu_sweep_last], a
    jr nz, .sweep_was_on
    ; 0→1 enable: arm sweep from current timer, do NOT retrigger here —
    ; $4000/$4003 follow and must apply volume (flagpole order is $4001 then $4000).
.sweep_arm:
    xor a
    ld [nes_apu_sweep_muted], a
    ld a, $01
    ld [nes_apu_sweep_active], a
    ld a, [nes_apu_regs + $02]
    ld [nes_apu_sweep_period_lo], a
    ld [nes_apu_sweep_start_lo], a
    ld a, [nes_apu_regs + $03]
    and $07
    ld [nes_apu_sweep_period_hi], a
    ld [nes_apu_sweep_start_hi], a
    ret
.sweep_was_on:
    ld a, [nes_apu_sweep_active]
    and a
    jr z, .sweep_inactive
    ld a, b
    xor c
    and $0F
    ret z
    ld a, [nes_apu_sweep_period_lo]
    ld [nes_apu_sweep_start_lo], a
    ld a, [nes_apu_sweep_period_hi]
    ld [nes_apu_sweep_start_hi], a
    ret
.sweep_inactive:
    ; Period-muted but enable still on (death $94 spam): stay quiet until $4003.
    ret

.do_freq:
    ld a, [nes_apu_write_idx]
    cp $03
    jr z, nes_apu_retrigger_p1
    ld a, [nes_apu_regs + $02]
    ld c, a
    ld [nes_apu_sweep_period_lo], a
    ld a, [nes_apu_regs + $03]
    and $07
    ld b, a
    ld [nes_apu_sweep_period_hi], a
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR13], a
    ld a, d
    and $07
    ldh [rNR14], a
    ret

nes_apu_retrigger_p1:
    xor a
    ld [nes_apu_sweep_muted], a
    ld a, [nes_apu_regs + $01]
    ld [nes_apu_sweep_last], a
    bit 7, a
    jr z, .retrig_seed
    ld a, $01
    ld [nes_apu_sweep_active], a
.retrig_seed:
    ld a, [nes_apu_regs + $02]
    ld c, a
    ld a, [nes_apu_regs + $03]
    and $07
    ld b, a
    ; Head-bump ($93, falling, shift 3) ~2 octaves sharp vs NES — drop timer×4.
    ; Jump uses shift 7; fireball/flagpole are negate (skip).
    ld a, [nes_apu_regs + $01]
    bit 7, a
    jr z, .have_timer
    bit 3, a
    jr nz, .have_timer
    and $07
    cp $04
    jr nc, .have_timer
    sla c
    rl b
    sla c
    rl b
    ld a, b
    and $07
    ld b, a
.have_timer:
    ld a, c
    ld [nes_apu_sweep_period_lo], a
    ld [nes_apu_sweep_start_lo], a
    ld a, b
    ld [nes_apu_sweep_period_hi], a
    ld [nes_apu_sweep_start_hi], a
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR13], a
    ld a, [nes_apu_regs + $00]
    and $C0
    ldh [rNR11], a
    ld a, [nes_apu_regs + $00]
    call nes_apu_vol_to_nrx2
    ld [nes_apu_last_nr12_p1], a
    ldh [rNR12], a
    ld a, d
    and $07
    or $80
    ldh [rNR14], a
    ret

; ---------------------------------------------------------------------------
; Pulse 2 → Square 2 (NR21–NR24)
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
    ld a, [nes_apu_write_idx]
    cp $04
    jr z, .do_vol_duty
    cp $05
    jr z, .do_sweep2
    cp $06
    jr z, .do_freq
    cp $07
    jr z, .do_freq
    ret

.do_sweep2:
    ld a, [nes_apu_regs + $05]
    bit 7, a
    jr nz, .sweep2_on
    ld a, [nes_apu_sweep2_active]
    and a
    jr z, .sweep2_off_idle
    ; End of blast-style sweep: silence pulse2 SFX tail.
    xor a
    ldh [rNR22], a
    ld [nes_apu_sweep2_active], a
    ld a, $10
    ld [nes_apu_last_nr12_p2], a
.sweep2_off_idle:
    ret
.sweep2_on:
    ld a, [nes_apu_regs + $05]
    rrca
    rrca
    rrca
    rrca
    and $07
    ld [nes_apu_sweep2_div], a
    ld a, [nes_apu_sweep2_active]
    and a
    jr z, .sweep2_first
    ld a, [nes_apu_sweep2_period_lo]
    ld [nes_apu_sweep2_start_lo], a
    ld a, [nes_apu_sweep2_period_hi]
    ld [nes_apu_sweep2_start_hi], a
    ret
.sweep2_first:
    ld a, $01
    ld [nes_apu_sweep2_active], a
    jp nes_apu_retrigger_p2

.do_vol_duty:
    ld a, [nes_apu_regs + $04]
    and $C0
    ldh [rNR21], a
    ld a, [nes_apu_regs + $04]
    call nes_apu_vol_to_nrx2
    ld b, a
    ld a, [nes_apu_last_nr12_p2]
    ld c, a
    ld a, b
    ld [nes_apu_last_nr12_p2], a
    ldh [rNR22], a
    ld a, c
    and $F0
    ret nz
    ld a, b
    and $F0
    ret z
    jp nes_apu_retrigger_p2

.do_freq:
    ld a, [nes_apu_write_idx]
    cp $07
    jr z, nes_apu_retrigger_p2
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
    ldh [rNR24], a
    ret

nes_apu_retrigger_p2:
    ld a, [nes_apu_regs + $06]
    ld c, a
    ld a, [nes_apu_regs + $07]
    and $07
    ld b, a
    ld a, c
    ld [nes_apu_sweep2_period_lo], a
    ld [nes_apu_sweep2_start_lo], a
    ld a, b
    ld [nes_apu_sweep2_period_hi], a
    ld [nes_apu_sweep2_start_hi], a
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR23], a
    ld a, [nes_apu_regs + $04]
    and $C0
    ldh [rNR21], a
    ld a, [nes_apu_regs + $04]
    call nes_apu_vol_to_nrx2
    ld [nes_apu_last_nr12_p2], a
    ldh [rNR22], a
    ld a, d
    and $07
    or $80
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
    ; $400B also reloads the linear counter from $4008 (NES reload flag).
    ld a, [nes_apu_regs + $08]
    and $7F
    ld [nes_apu_linear_tri], a
.no_preload:
    ld a, [nes_apu_regs + $15]
    and $04
    jr nz, .chan_on
.silent:
    xor a
    ldh [rNR30], a
    ret

.chan_on:
    ld a, [nes_apu_write_idx]
    cp $08
    jr z, .do_linear
    cp $0A
    jr z, .do_freq
    cp $0B
    jr z, .do_freq
    ret

.do_linear:
    ; Load working linear counter from $4008. Vol 0 / reload 0 → silence.
    ld a, [nes_apu_regs + $08]
    and $7F
    ld [nes_apu_linear_tri], a
    jr z, .lin_mute
    ld a, $80
    ldh [rNR30], a
    ld a, $20
    ldh [rNR32], a
    ret
.lin_mute:
    xor a
    ldh [rNR30], a
    ldh [rNR32], a
    ret

.do_freq:
    ld a, [nes_apu_regs + $0A]
    ld c, a
    ld a, [nes_apu_regs + $0B]
    and $07
    ld b, a
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
    ld a, [nes_apu_linear_tri]
    and a
    jr z, .vol_mute
    ld a, $20
    jr .vol_write
.vol_mute:
    xor a
.vol_write:
    ldh [rNR32], a
    call nes_apu_timer_to_period
    ; Same octave as pulse mapping (extra triangle drop removed — too low).
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
    ld a, [nes_apu_write_idx]
    cp $0C
    jr z, .do_vol
    cp $0E
    jr z, .do_freq
    cp $0F
    jr z, .do_freq
    ret

.do_vol:
    xor a
    ldh [rNR41], a
    ld a, [nes_apu_regs + $0C]
    call nes_apu_vol_to_nrx2
    ld b, a
    ld a, [nes_apu_last_nr12_noi]
    ld c, a
    ld a, b
    ld [nes_apu_last_nr12_noi], a
    ldh [rNR42], a
    ld a, c
    and $F0
    ret nz
    ld a, b
    and $F0
    ret z
    jp nes_apu_retrigger_noi

.do_freq:
    ld a, [nes_apu_write_idx]
    cp $0F
    jr z, nes_apu_retrigger_noi
    ld a, [nes_apu_regs + $0E]
    ld b, a
    and $0F
    ld e, a
    ld d, 0
    ld hl, nes_apu_noise_nr43
    add hl, de
    ld a, [hl]
    bit 7, b
    jr z, .nr43
    or $08
.nr43:
    ldh [rNR43], a
    xor a
    ldh [rNR44], a
    ret

nes_apu_retrigger_noi:
    ld a, [nes_apu_regs + $0E]
    ld b, a
    and $0F
    ld e, a
    ld d, 0
    ld hl, nes_apu_noise_nr43
    add hl, de
    ld a, [hl]
    bit 7, b
    jr z, .nr43b
    or $08
.nr43b:
    ldh [rNR43], a
    ld a, [nes_apu_regs + $0C]
    call nes_apu_vol_to_nrx2
    ld [nes_apu_last_nr12_noi], a
    ldh [rNR42], a
    ld a, $80
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

    ; NES $4015: clearing an enable bit forces that channel's length to 0
    ; (silence). Setting it again does NOT restart — stays silent until the
    ; next length reload ($4003/$4007/$400B/$400F). SMB StopSquare*Sfx toggles
    ; $4015 to kill SFX; our old rising-edge retrigger brought the tone back
    ; forever (underground hang) and machine-gunned death music.

    ; ---- pulse1 ----
    bit 0, c
    jr nz, .p2
    xor a
    ldh [rNR12], a
    ld [nes_apu_len_p1], a
    ld [nes_apu_sweep_active], a
    ld [nes_apu_sweep_muted], a
    ld a, $10
    ld [nes_apu_last_nr12_p1], a
.p2:
    bit 1, c
    jr nz, .tri
    xor a
    ldh [rNR22], a
    ld [nes_apu_len_p2], a
    ld [nes_apu_sweep2_active], a
    ld a, $10
    ld [nes_apu_last_nr12_p2], a
.tri:
    bit 2, c
    jr nz, .noi
    xor a
    ldh [rNR30], a
    ld [nes_apu_len_tri], a
    ld [nes_apu_linear_tri], a
.noi:
    bit 3, c
    ret nz
    xor a
    ldh [rNR42], a
    ld [nes_apu_len_noi], a
    ld a, $10
    ld [nes_apu_last_nr12_noi], a
    ret

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
    ; Vol 0 must stay 0 — SMB music envelope ends in $90 (silence) for note gaps.
    ; Floor only 1–3 (pipe/injury); never boost rests into legato.
    and $0F
    jr z, .const_ok
    cp $04
    jr nc, .const_ok
    ld a, $06
.const_ok:
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
    ; NES sweep unit ~120Hz; two clocks per 60Hz VBlank.
    call nes_apu_clock_sweep_p1
    call nes_apu_clock_sweep_p1
    call nes_apu_clock_sweep_p2
    call nes_apu_clock_sweep_p2
    ; ~60Hz length clock. Mute only on the frame the counter hits zero so we
    ; do not keep forcing NRx2=0 while music reprograms the channel.
    ; Pulse1
    ld a, [nes_apu_regs + $00]
    bit 5, a
    jr nz, .p2
    ld a, [nes_apu_len_p1]
    and a
    jr z, .p2
    dec a
    ld [nes_apu_len_p1], a
    jr nz, .p2
    ldh [rNR12], a                ; A=0
    ld a, $10
    ld [nes_apu_last_nr12_p1], a
.p2:
    ld a, [nes_apu_regs + $04]
    bit 5, a
    jr nz, .tri
    ld a, [nes_apu_len_p2]
    and a
    jr z, .tri
    dec a
    ld [nes_apu_len_p2], a
    jr nz, .tri
    ldh [rNR22], a
    ld a, $10
    ld [nes_apu_last_nr12_p2], a
.tri:
    ; Linear ~120Hz (2 / VBlank) — middle articulation (lean staccato).
    ld a, [nes_apu_regs + $08]
    bit 7, a
    jr nz, .tri_len
    ld b, 2
.lin_clk:
    ld a, [nes_apu_linear_tri]
    and a
    jr z, .tri_len
    dec a
    ld [nes_apu_linear_tri], a
    jr nz, .lin_next
    ldh [rNR30], a
    jr .tri_len
.lin_next:
    dec b
    jr nz, .lin_clk
.tri_len:
    ld a, [nes_apu_regs + $08]
    bit 7, a
    jr nz, .noi
    ld a, [nes_apu_len_tri]
    and a
    jr z, .noi
    dec a
    ld [nes_apu_len_tri], a
    jr nz, .noi
    ldh [rNR30], a
.noi:
    ld a, [nes_apu_regs + $0C]
    bit 5, a
    ret nz
    ld a, [nes_apu_len_noi]
    and a
    ret z
    dec a
    ld [nes_apu_len_noi], a
    ret nz
    ldh [rNR42], a
    ld a, $10
    ld [nes_apu_last_nr12_noi], a
    ret


; ---------------------------------------------------------------------------
; NES-style pulse1 sweep at ~60Hz (host VBlank). GB NR10 is left off.
; Mute when period < 8 or would overflow $7FF — same idea as NES sweep unit.
; ---------------------------------------------------------------------------
nes_apu_clock_sweep_p1:
    ld a, [nes_apu_regs + $15]
    and $01
    ret z
    ld a, [nes_apu_sweep_active]
    and a
    ret z                         ; post-mute freeze until $4003
    ld a, [nes_apu_regs + $01]
    bit 7, a
    ret z
    ld e, a                       ; E = sweep reg
    and $07
    ret z                         ; shift 0 = no change (but still "enabled")
    ld b, a                       ; B = shift
    ; Divider
    ld a, [nes_apu_sweep_div]
    and a
    jr z, .do_sweep_step
    dec a
    ld [nes_apu_sweep_div], a
    ret
.do_sweep_step:
    ; Reload divider from period bits 4-6
    ld a, e
    rrca
    rrca
    rrca
    rrca
    and $07
    ld [nes_apu_sweep_div], a
    ; HL = current working period
    ld a, [nes_apu_sweep_period_lo]
    ld l, a
    ld a, [nes_apu_sweep_period_hi]
    ld h, a
    ; NES: channel muted while period < 8
    ld a, h
    and a
    jr nz, .period_ge8
    ld a, l
    cp $08
    jp c, .mute
.period_ge8:
    ; DE = HL >> shift
    ld a, l
    ld e, a
    ld a, h
    ld d, a
    ld a, b
.shr:
    srl d
    rr e
    dec a
    jr nz, .shr
    ; Negate?
    ld a, [nes_apu_regs + $01]
    bit 3, a
    jr nz, .negate
    ; target = period + delta; mute if target > $7FF
    ld a, l
    add e
    ld c, a
    ld a, h
    adc d
    ld b, a
    and $F8
    jp nz, .mute
    ld l, c
    ld h, b
    jr .store
.negate:
    ; target = period - delta - 1 (pulse1 ones-complement approx)
    ld a, e
    or d
    jr z, .store                 ; shift produced 0
    ld a, l
    sub e
    ld c, a
    ld a, h
    sbc d
    ld b, a
    jp c, .mute
    ld a, c
    sub $01
    ld l, a
    ld a, b
    sbc $00
    ld h, a
    jp c, .mute
.store:
    ; NES mute if period < 8
    ld a, h
    and a
    jr nz, .chk_span
    ld a, l
    cp $08
    jp c, .mute
.chk_span:
    ; Rising sweep octave cap only for gentler shifts (jump). Fireball shift 1
    ; halves every step — cap made it a click; use NES period < 8 only.
    ld a, [nes_apu_regs + $01]
    bit 3, a
    jr z, .ok_period
    and $07
    cp $03
    jr c, .ok_period
    ld a, [nes_apu_sweep_start_lo]
    ld e, a
    ld a, [nes_apu_sweep_start_hi]
    ld d, a
    srl d
    rr e
    srl d
    rr e
    ld a, h
    cp d
    jp c, .mute
    jr nz, .ok_period
    ld a, l
    cp e
    jp c, .mute
.ok_period:
    ld a, l
    ld [nes_apu_sweep_period_lo], a
    ld c, a
    ld a, h
    and $07
    ld [nes_apu_sweep_period_hi], a
    ld b, a
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR13], a
    ld a, d
    and $07
    ldh [rNR14], a                ; no trigger — continue note
    ret
.mute:
nes_apu_mute_p1_locked:
    xor a
    ldh [rNR12], a
    ld [nes_apu_sweep_active], a
    ld a, $01
    ld [nes_apu_sweep_muted], a
    ; Keep a non-zero high nibble in last_nr12 so the next $4000 envelope
    ; tick is NOT treated as silence→audible (which would restart jump).
    ; Do NOT clear $4001 bit7 — death music rewrites $94 every frame; clearing
    ; enable made each rewrite look like a fresh sweep and machine-gunned.
    ld a, $10
    ld [nes_apu_last_nr12_p1], a
    ret

; Pulse2 software sweep (fireworks/blast). Same rules as pulse1, ~1 octave cap.
nes_apu_clock_sweep_p2:
    ld a, [nes_apu_regs + $15]
    and $02
    ret z
    ld a, [nes_apu_regs + $05]
    bit 7, a
    ret z
    ld e, a
    and $07
    ret z
    ld b, a
    ld a, [nes_apu_sweep2_div]
    and a
    jr z, .do2
    dec a
    ld [nes_apu_sweep2_div], a
    ret
.do2:
    ld a, e
    rrca
    rrca
    rrca
    rrca
    and $07
    ld [nes_apu_sweep2_div], a
    ld a, [nes_apu_sweep2_period_lo]
    ld l, a
    ld a, [nes_apu_sweep2_period_hi]
    ld h, a
    ld a, h
    and a
    jr nz, .ge8_2
    ld a, l
    cp $08
    jp c, .mute2
.ge8_2:
    ld a, l
    ld e, a
    ld a, h
    ld d, a
    ld a, b
.shr2:
    srl d
    rr e
    dec a
    jr nz, .shr2
    ld a, [nes_apu_regs + $05]
    bit 3, a
    jr nz, .neg2
    ld a, l
    add e
    ld c, a
    ld a, h
    adc d
    ld b, a
    and $F8
    jp nz, .mute2
    ld l, c
    ld h, b
    jr .store2
.neg2:
    ld a, e
    or d
    jr z, .store2
    ld a, l
    sub e
    ld c, a
    ld a, h
    sbc d
    ld b, a
    jp c, .mute2
    ld a, c
    sub $01
    ld l, a
    ld a, b
    sbc $00
    ld h, a
    jp c, .mute2
.store2:
    ld a, h
    and a
    jr nz, .span2
    ld a, l
    cp $08
    jp c, .mute2
.span2:
    ld a, [nes_apu_regs + $05]
    bit 3, a
    jr nz, .rise2
    ld a, [nes_apu_sweep2_start_lo]
    add a
    ld e, a
    ld a, [nes_apu_sweep2_start_hi]
    adc a
    ld d, a
    and $F8
    jr z, .cmpf2
    ld de, $07FF
.cmpf2:
    ld a, h
    cp d
    jr c, .ok2
    jr nz, .mute2
    ld a, l
    cp e
    jr c, .ok2
    jr z, .ok2
    jr .mute2
.rise2:
    ld a, [nes_apu_sweep2_start_lo]
    ld e, a
    ld a, [nes_apu_sweep2_start_hi]
    ld d, a
    srl d
    rr e
    ld a, h
    cp d
    jr c, .mute2
    jr nz, .ok2
    ld a, l
    cp e
    jr c, .mute2
.ok2:
    ld a, l
    ld [nes_apu_sweep2_period_lo], a
    ld c, a
    ld a, h
    and $07
    ld [nes_apu_sweep2_period_hi], a
    ld b, a
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR23], a
    ld a, d
    and $07
    ldh [rNR24], a
    ret
.mute2:
    xor a
    ldh [rNR22], a
    ld [nes_apu_sweep2_active], a
    ld a, $10
    ld [nes_apu_last_nr12_p2], a
    ld a, [nes_apu_regs + $05]
    and $7F
    ld [nes_apu_regs + $05], a
    ret

; Official NES length counter table (bits 3-7 of $4003/$4007/$400B/$400F).
nes_apu_length_table:
    db 10,254, 20,  2, 40,  4, 80,  6, 160,  8, 60, 10, 14, 12, 26, 14
    db 12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30

; NES noise period index 0..F → rough NR43 encoding (lower = higher pitch).
nes_apu_noise_nr43:
    ; One octave up from prior table (stage percussion was dull).
    db $E7, $E3, $D3, $C3, $B3, $A3, $93, $83
    db $73, $63, $53, $43, $33, $23, $13, $03
