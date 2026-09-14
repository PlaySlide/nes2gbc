; NES APU → GBC sound bridge (v1).
;
; nes_apu_write calling convention (matches cpu.asm $40xx path):
;   L = NES APU register low byte ($00-$17 for $4000-$4017)
;   E = value written
; Clobbers: AF, BC, DE, HL (void return; cpu write path ignores result).
;
; nes_apu_read_status:
;   Output A = $4015-style active-channel status from length counters.
;   DMC/IRQ status is currently unsupported and reads as 0.

SECTION "NES APU state", WRAM0[$CA00]
; Mirror of $4000-$4017 indexed by low address byte.
nes_apu_regs:       ds $18
; Low byte of the register currently being written (survives helpers).
nes_apu_write_idx:  ds 1
; NES length counters.
nes_apu_len_p1:     ds 1
nes_apu_len_p2:     ds 1
nes_apu_len_tri:    ds 1
nes_apu_len_noi:    ds 1
; Triangle linear counter and reload flag.
nes_apu_linear_tri: ds 1
nes_apu_tri_reload: ds 1
; Pulse sweep divider counters and reload flags.
nes_apu_sweep_div_p1:    ds 1
nes_apu_sweep_reload_p1: ds 1
nes_apu_sweep_div_p2:    ds 1
nes_apu_sweep_reload_p2: ds 1
; Approximate NES frame-counter phase. 4-step uses 0..3, 5-step uses 0..4.
nes_apu_frame_phase: ds 1

SECTION "NES APU code", ROM0

; ---------------------------------------------------------------------------
nes_apu_init:
    ld hl, nes_apu_regs
    ld b, $18 + 12         ; register mirror + APU architectural state
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
    ; NES $4015 read bits 0-3 report whether each channel length counter is
    ; non-zero, not whether its enable bit was last written as 1. DMC and IRQ
    ; status are not implemented yet, so bits 4, 6, and 7 remain clear.
    ld b, $00
    ld a, [nes_apu_len_p1]
    and a
    jr z, .status_p2
    set 0, b
.status_p2:
    ld a, [nes_apu_len_p2]
    and a
    jr z, .status_tri
    set 1, b
.status_tri:
    ld a, [nes_apu_len_tri]
    and a
    jr z, .status_noi
    set 2, b
.status_noi:
    ld a, [nes_apu_len_noi]
    and a
    jr z, .status_done
    set 3, b
.status_done:
    ld a, b
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
    cp $17
    jp z, nes_apu_update_frame_counter
    ; $10/$12/$13 DMC registers are shadow-only for now.
    ret

.dmc_load:
    ld a, e
    ld [nes_dac], a
    ret

; ---------------------------------------------------------------------------
; Pulse helpers. The GBC renderer is gated from NES length/timer/sweep state.
; ---------------------------------------------------------------------------
nes_apu_load_p1_timer_bc:
    ld a, [nes_apu_regs + $02]
    ld c, a
    ld a, [nes_apu_regs + $03]
    and $07
    ld b, a
    ret

nes_apu_load_p2_timer_bc:
    ld a, [nes_apu_regs + $06]
    ld c, a
    ld a, [nes_apu_regs + $07]
    and $07
    ld b, a
    ret

; Compute pulse 1 sweep target in HL. Carry means the channel is muted by
; current-period < 8 or positive target overflow > $7FF.
nes_apu_sweep_target_p1:
    ld a, [nes_apu_regs + $02]
    ld l, a
    ld a, [nes_apu_regs + $03]
    and $07
    ld h, a
    and a
    jr nz, .period_ok
    ld a, l
    cp $08
    jr c, .muted
.period_ok:
    ld a, [nes_apu_regs + $01]
    and $07
    ld c, a
    ld d, h
    ld e, l
    ld a, c
    and a
    jr z, .shift_done
.shift:
    srl d
    rr e
    dec a
    jr nz, .shift
.shift_done:
    ld a, [nes_apu_regs + $01]
    bit 3, a
    jr nz, .negative
    ld a, l
    add e
    ld l, a
    ld a, h
    adc d
    ld h, a
    and $F8
    jr nz, .muted
    and a
    ret
.negative:
    ; Pulse 1 uses one's-complement negate: period - change - 1.
    ld a, l
    sub e
    ld l, a
    ld a, h
    sbc d
    ld h, a
    jr c, .clamp_zero
    ld a, l
    sub $01
    ld l, a
    ld a, h
    sbc $00
    ld h, a
    jr c, .clamp_zero
    and a
    ret
.clamp_zero:
    ld hl, $0000
    and a
    ret
.muted:
    scf
    ret

; Pulse 2 differs only in negate: two's-complement period - change.
nes_apu_sweep_target_p2:
    ld a, [nes_apu_regs + $06]
    ld l, a
    ld a, [nes_apu_regs + $07]
    and $07
    ld h, a
    and a
    jr nz, .period_ok
    ld a, l
    cp $08
    jr c, .muted
.period_ok:
    ld a, [nes_apu_regs + $05]
    and $07
    ld c, a
    ld d, h
    ld e, l
    ld a, c
    and a
    jr z, .shift_done
.shift:
    srl d
    rr e
    dec a
    jr nz, .shift
.shift_done:
    ld a, [nes_apu_regs + $05]
    bit 3, a
    jr nz, .negative
    ld a, l
    add e
    ld l, a
    ld a, h
    adc d
    ld h, a
    and $F8
    jr nz, .muted
    and a
    ret
.negative:
    ld a, l
    sub e
    ld l, a
    ld a, h
    sbc d
    ld h, a
    jr c, .clamp_zero
    and a
    ret
.clamp_zero:
    ld hl, $0000
    and a
    ret
.muted:
    scf
    ret

nes_apu_start_p1:
    call nes_apu_load_p1_timer_bc
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR13], a
    ld a, [nes_apu_regs + $00]
    and $C0
    ldh [rNR11], a
    ld a, [nes_apu_regs + $00]
    call nes_apu_vol_to_nrx2
    ldh [rNR12], a
    ld a, d
    and $07
    or $80
    ldh [rNR14], a
    ret

nes_apu_start_p2:
    call nes_apu_load_p2_timer_bc
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR23], a
    ld a, [nes_apu_regs + $04]
    and $C0
    ldh [rNR21], a
    ld a, [nes_apu_regs + $04]
    call nes_apu_vol_to_nrx2
    ldh [rNR22], a
    ld a, d
    and $07
    or $80
    ldh [rNR24], a
    ret

nes_apu_refresh_p1:
    ld a, [nes_apu_regs + $15]
    and $01
    jr z, .mute
    ld a, [nes_apu_len_p1]
    and a
    jr z, .mute
    call nes_apu_sweep_target_p1
    jr c, .mute
    ld a, [nes_apu_regs + $00]
    call nes_apu_vol_to_nrx2
    ldh [rNR12], a
    and $F8
    ret z
    ldh a, [rNR52]
    bit 0, a
    ret nz
    jp nes_apu_start_p1
.mute:
    xor a
    ldh [rNR12], a
    ret

nes_apu_refresh_p2:
    ld a, [nes_apu_regs + $15]
    and $02
    jr z, .mute
    ld a, [nes_apu_len_p2]
    and a
    jr z, .mute
    call nes_apu_sweep_target_p2
    jr c, .mute
    ld a, [nes_apu_regs + $04]
    call nes_apu_vol_to_nrx2
    ldh [rNR22], a
    and $F8
    ret z
    ldh a, [rNR52]
    bit 1, a
    ret nz
    jp nes_apu_start_p2
.mute:
    xor a
    ldh [rNR22], a
    ret

nes_apu_trigger_p1:
    ld a, [nes_apu_regs + $15]
    and $01
    jr z, .mute
    ld a, [nes_apu_len_p1]
    and a
    jr z, .mute
    call nes_apu_sweep_target_p1
    jr c, .mute
    jp nes_apu_start_p1
.mute:
    xor a
    ldh [rNR12], a
    ret

nes_apu_trigger_p2:
    ld a, [nes_apu_regs + $15]
    and $02
    jr z, .mute
    ld a, [nes_apu_len_p2]
    and a
    jr z, .mute
    call nes_apu_sweep_target_p2
    jr c, .mute
    jp nes_apu_start_p2
.mute:
    xor a
    ldh [rNR22], a
    ret

; ---------------------------------------------------------------------------
; Pulse 1 -> GBC square 1.
; ---------------------------------------------------------------------------
nes_apu_update_pulse1:
    ld a, [nes_apu_write_idx]
    cp $03
    jr nz, .dispatch
    ld a, [nes_apu_regs + $15]
    and $01
    jr z, .dispatch
    ld a, [nes_apu_regs + $03]
    ld hl, nes_apu_len_p1
    call nes_apu_load_length
.dispatch:
    ld a, [nes_apu_write_idx]
    cp $00
    jr z, .volume
    cp $01
    jr z, .sweep
    cp $02
    jr z, .freq
    cp $03
    jr z, .trigger
    ret
.volume:
    ld a, [nes_apu_regs + $00]
    and $C0
    ldh [rNR11], a
    jp nes_apu_refresh_p1
.sweep:
    ld a, $01
    ld [nes_apu_sweep_reload_p1], a
    jp nes_apu_refresh_p1
.freq:
    call nes_apu_load_p1_timer_bc
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR13], a
    ld a, d
    and $07
    ldh [rNR14], a
    jp nes_apu_refresh_p1
.trigger:
    jp nes_apu_trigger_p1

; ---------------------------------------------------------------------------
; Pulse 2 -> GBC square 2.
; ---------------------------------------------------------------------------
nes_apu_update_pulse2:
    ld a, [nes_apu_write_idx]
    cp $07
    jr nz, .dispatch
    ld a, [nes_apu_regs + $15]
    and $02
    jr z, .dispatch
    ld a, [nes_apu_regs + $07]
    ld hl, nes_apu_len_p2
    call nes_apu_load_length
.dispatch:
    ld a, [nes_apu_write_idx]
    cp $04
    jr z, .volume
    cp $05
    jr z, .sweep
    cp $06
    jr z, .freq
    cp $07
    jr z, .trigger
    ret
.volume:
    ld a, [nes_apu_regs + $04]
    and $C0
    ldh [rNR21], a
    jp nes_apu_refresh_p2
.sweep:
    ld a, $01
    ld [nes_apu_sweep_reload_p2], a
    jp nes_apu_refresh_p2
.freq:
    call nes_apu_load_p2_timer_bc
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR23], a
    ld a, d
    and $07
    ldh [rNR24], a
    jp nes_apu_refresh_p2
.trigger:
    jp nes_apu_trigger_p2

; ---------------------------------------------------------------------------
; Triangle → Wave (NR30–NR34)
; ---------------------------------------------------------------------------
nes_apu_update_triangle:
    ld a, [nes_apu_write_idx]
    cp $0B
    jr nz, .no_preload
    ; $400B sets the triangle linear reload state regardless, but the NES
    ; length counter only reloads while triangle is enabled in $4015.
    ld a, [nes_apu_regs + $15]
    and $04
    jr z, .reload_linear
    ld a, [nes_apu_regs + $0B]
    ld hl, nes_apu_len_tri
    call nes_apu_load_length
.reload_linear:
    ld a, $01
    ld [nes_apu_tri_reload], a
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
    ; $4008 changes only control/reload value. The working counter changes on
    ; the next quarter-frame clock if the reload flag is set.
    jp nes_apu_apply_triangle_gate

.do_freq:
    ld a, [nes_apu_regs + $0A]
    ld c, a
    ld a, [nes_apu_regs + $0B]
    and $07
    ld b, a
    call nes_apu_timer_to_period
    ; Pulse and triangle use the same NES->GB divisor ratio.
    ld a, e
    ldh [rNR33], a
    ld a, d
    and $07
    ldh [rNR34], a                 ; frequency update only; no phase reset
    jp nes_apu_apply_triangle_gate

; Gate GB wave output from the NES triangle's two counters. NES triangle phase
; freezes while either counter is zero. GB CH3 cannot reproduce that perfectly,
; so we stop CH3 and only retrigger it when the gate opens again.
nes_apu_apply_triangle_gate:
    ld a, [nes_apu_regs + $15]
    and $04
    jr z, .mute
    ld a, [nes_apu_len_tri]
    and a
    jr z, .mute
    ld a, [nes_apu_linear_tri]
    and a
    jr z, .mute

.audible:
    ld a, $80
    ldh [rNR30], a
    ld a, $20
    ldh [rNR32], a
    ldh a, [rNR52]
    bit 2, a
    ret nz                          ; already running: preserve GB phase

    ; Gate just reopened: start CH3 at the currently programmed frequency.
    ld a, [nes_apu_regs + $0A]
    ld c, a
    ld a, [nes_apu_regs + $0B]
    and $07
    ld b, a
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR33], a
    ld a, d
    and $07
    or $80
    ldh [rNR34], a
    ret

.mute:
    xor a
    ldh [rNR30], a
    ldh [rNR32], a
    ret

; ---------------------------------------------------------------------------
; Noise -> GBC noise. NR43 is the closest hardware frequency/LFSR mapping.
; ---------------------------------------------------------------------------
nes_apu_write_noise_period:
    ld a, [nes_apu_regs + $0E]
    ld b, a
    and $0F
    ld e, a
    ld d, 0
    ld hl, nes_apu_noise_nr43
    add hl, de
    ld a, [hl]
    bit 7, b
    jr z, .write
    or $08
.write:
    ldh [rNR43], a
    ret

nes_apu_start_noi:
    call nes_apu_write_noise_period
    ld a, [nes_apu_regs + $0C]
    call nes_apu_vol_to_nrx2
    ldh [rNR42], a
    ld a, $80
    ldh [rNR44], a
    ret

nes_apu_refresh_noi:
    ld a, [nes_apu_regs + $15]
    and $08
    jr z, .mute
    ld a, [nes_apu_len_noi]
    and a
    jr z, .mute
    ld a, [nes_apu_regs + $0C]
    call nes_apu_vol_to_nrx2
    ldh [rNR42], a
    and $F8
    ret z
    ldh a, [rNR52]
    bit 3, a
    ret nz
    jp nes_apu_start_noi
.mute:
    xor a
    ldh [rNR42], a
    ret

nes_apu_update_noise:
    ld a, [nes_apu_write_idx]
    cp $0F
    jr nz, .dispatch
    ld a, [nes_apu_regs + $15]
    and $08
    jr z, .dispatch
    ld a, [nes_apu_regs + $0F]
    ld hl, nes_apu_len_noi
    call nes_apu_load_length
.dispatch:
    ld a, [nes_apu_write_idx]
    cp $0C
    jr z, .volume
    cp $0E
    jr z, .freq
    cp $0F
    jr z, .trigger
    ret
.volume:
    xor a
    ldh [rNR41], a
    jp nes_apu_refresh_noi
.freq:
    call nes_apu_write_noise_period
    jp nes_apu_refresh_noi
.trigger:
    ld a, [nes_apu_regs + $15]
    and $08
    jr z, .mute
    ld a, [nes_apu_len_noi]
    and a
    jr z, .mute
    jp nes_apu_start_noi
.mute:
    xor a
    ldh [rNR42], a
    ret

; ---------------------------------------------------------------------------
; $4015: channel enables. Clearing a bit clears that channel's length counter;
; setting a bit does not reload or restart the channel.
; ---------------------------------------------------------------------------
nes_apu_update_status:
    ld a, e
    and $1F
    ld c, a

    bit 0, c
    jr nz, .p2
    xor a
    ld [nes_apu_len_p1], a
    ldh [rNR12], a
.p2:
    bit 1, c
    jr nz, .tri
    xor a
    ld [nes_apu_len_p2], a
    ldh [rNR22], a
.tri:
    bit 2, c
    jr nz, .noi
    xor a
    ld [nes_apu_len_tri], a
    ldh [rNR30], a
.noi:
    bit 3, c
    ret nz
    xor a
    ld [nes_apu_len_noi], a
    ldh [rNR42], a
    ret

; ---------------------------------------------------------------------------
; NES volume/envelope register -> GBC NRx2 approximation.
; Constant volume maps 0..15 directly. For NES decay envelopes, GBC's envelope
; timer is coarser (64 Hz, 3-bit period), so choose the nearest practical decay
; period while keeping the NES initial level of 15. No game-specific boosts.
; ---------------------------------------------------------------------------
nes_apu_vol_to_nrx2:
    bit 4, a
    jr z, .envelope
    and $0F
    swap a
    ret
.envelope:
    and $0F
    ld e, a
    ld d, 0
    ld hl, nes_apu_gb_env_period
    add hl, de
    ld a, [hl]
    or $F0
    ret

nes_apu_gb_env_period:
    ; nearest GB envelope period to (V+1)/240 s, clamped to 1..7
    db 1,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4

; ---------------------------------------------------------------------------
; Input:  BC = NES 11-bit timer t
; Output: DE = GBC period n = 2048 - min(2047, ((t+1)*75)/64), in 0..2047
; Clobbers: AF, HL, BC
;
; 75/64 = 1.171875, within ~0.03% of the NTSC NES->GBC divisor ratio for
; both pulse and triangle channels. No title-specific pitch scaling is applied.
; ---------------------------------------------------------------------------
nes_apu_timer_to_period:
    ; HL = t + 1
    inc bc
    ld l, c
    ld h, b

    push hl
    ; DE = HL * 11 = HL*8 + HL*2 + HL
    push hl
    add hl, hl
    add hl, hl
    add hl, hl
    ld e, l
    ld d, h
    pop hl
    push hl
    add hl, hl
    ld a, l
    add e
    ld e, a
    ld a, h
    adc d
    ld d, a
    pop hl
    ld a, l
    add e
    ld e, a
    ld a, h
    adc d
    ld d, a

    ; DE = ((t+1)*11)/64
    ld b, 6
.shr6:
    srl d
    rr e
    dec b
    jr nz, .shr6

    ; + (t+1) = ((t+1)*75)/64
    pop hl
    ld a, l
    add e
    ld e, a
    ld a, h
    adc d
    ld d, a

    ; GB period divisor cannot exceed 2047.
    ld a, d
    cp $08
    jr nc, .cap
    cp $07
    jr c, .convert
    ld a, e
    cp $FF
    jr c, .convert
.cap:
    ld de, $07FF
.convert:
    xor a
    sub e
    ld e, a
    ld a, $08
    sbc d
    and $07
    ld d, a
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
; $4017 frame-counter control. Bit 7 selects 5-step mode. We ignore frame IRQ
; generation for now, but reset the phase on every write. In 5-step mode the
; NES immediately generates one quarter+half-frame clock after the write; the
; real 3/4 CPU-cycle delay is below this bridge's timing resolution.
nes_apu_update_frame_counter:
    xor a
    ld [nes_apu_frame_phase], a
    ld a, e
    bit 7, a
    ret z
    call nes_apu_clock_quarter
    jp nes_apu_clock_half

; Host VBlank is ~60 Hz, so advance four ~240 Hz frame-sequencer slots each
; VBlank. Keeping phase across calls gives mode 0 four slots (240/120 Hz) and
; mode 1 five slots including its blank step (192/96 Hz average).
nes_apu_frame_tick:
    ld b, 4
.step:
    push bc
    call nes_apu_frame_step
    pop bc
    dec b
    jr nz, .step
    ret

nes_apu_frame_step:
    ld a, [nes_apu_regs + $17]
    bit 7, a
    jr nz, .mode5

.mode4:
    ld a, [nes_apu_frame_phase]
    and $03
    jr z, .m4_q0
    cp $01
    jr z, .m4_qh1
    cp $02
    jr z, .m4_q2
    ; phase 3: quarter + half, then wrap.
    xor a
    ld [nes_apu_frame_phase], a
    call nes_apu_clock_quarter
    jp nes_apu_clock_half
.m4_q0:
    ld a, $01
    ld [nes_apu_frame_phase], a
    jp nes_apu_clock_quarter
.m4_qh1:
    ld a, $02
    ld [nes_apu_frame_phase], a
    call nes_apu_clock_quarter
    jp nes_apu_clock_half
.m4_q2:
    ld a, $03
    ld [nes_apu_frame_phase], a
    jp nes_apu_clock_quarter

.mode5:
    ld a, [nes_apu_frame_phase]
    cp $01
    jr z, .m5_qh1
    cp $02
    jr z, .m5_q2
    cp $03
    jr z, .m5_blank3
    cp $04
    jr z, .m5_qh4
    ; phase 0: quarter only.
    ld a, $01
    ld [nes_apu_frame_phase], a
    jp nes_apu_clock_quarter
.m5_qh1:
    ld a, $02
    ld [nes_apu_frame_phase], a
    call nes_apu_clock_quarter
    jp nes_apu_clock_half
.m5_q2:
    ld a, $03
    ld [nes_apu_frame_phase], a
    jp nes_apu_clock_quarter
.m5_blank3:
    ld a, $04
    ld [nes_apu_frame_phase], a
    ret
.m5_qh4:
    xor a
    ld [nes_apu_frame_phase], a
    call nes_apu_clock_quarter
    jp nes_apu_clock_half

; Quarter-frame clocks: triangle linear timing is software-owned. Pulse/noise
; envelope decay is rendered by the closest GBC hardware envelope period.
nes_apu_clock_quarter:
    jp nes_apu_clock_triangle_linear

; Half-frame clocks drive length counters and both software sweep units.
nes_apu_clock_half:
    call nes_apu_clock_lengths
    call nes_apu_clock_sweep_p1
    jp nes_apu_clock_sweep_p2

nes_apu_clock_triangle_linear:
    ld a, [nes_apu_tri_reload]
    and a
    jr z, .decrement
    ld a, [nes_apu_regs + $08]
    and $7F
    ld [nes_apu_linear_tri], a
    jr .reload_done
.decrement:
    ld a, [nes_apu_linear_tri]
    and a
    jr z, .reload_done
    dec a
    ld [nes_apu_linear_tri], a
.reload_done:
    ; Control=0 clears the reload flag after the clock. Control=1 leaves it
    ; asserted, causing the reload value to be applied every quarter frame.
    ld a, [nes_apu_regs + $08]
    bit 7, a
    jr nz, .gate
    xor a
    ld [nes_apu_tri_reload], a
.gate:
    jp nes_apu_apply_triangle_gate

nes_apu_clock_lengths:
    ld a, [nes_apu_regs + $00]
    bit 5, a
    jr nz, .p2
    ld a, [nes_apu_len_p1]
    and a
    jr z, .p2
    dec a
    ld [nes_apu_len_p1], a
    jr nz, .p2
    xor a
    ldh [rNR12], a
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
    xor a
    ldh [rNR22], a
.tri:
    ld a, [nes_apu_regs + $08]
    bit 7, a
    jr nz, .noi
    ld a, [nes_apu_len_tri]
    and a
    jr z, .noi
    dec a
    ld [nes_apu_len_tri], a
.noi:
    ld a, [nes_apu_regs + $0C]
    bit 5, a
    jr nz, .gate_triangle
    ld a, [nes_apu_len_noi]
    and a
    jr z, .gate_triangle
    dec a
    ld [nes_apu_len_noi], a
    jr nz, .gate_triangle
    xor a
    ldh [rNR42], a
.gate_triangle:
    jp nes_apu_apply_triangle_gate


; ---------------------------------------------------------------------------
; NES pulse sweep units. Target muting is combinational; period updates happen
; only on half-frame clocks when divider=0, enabled=1, and shift!=0.
; ---------------------------------------------------------------------------
nes_apu_clock_sweep_p1:
    ld a, [nes_apu_sweep_div_p1]
    and a
    jr nz, .divider_nonzero

    ld a, [nes_apu_regs + $01]
    bit 7, a
    jr z, .reload
    and $07
    jr z, .reload
    call nes_apu_sweep_target_p1
    jr c, .reload

    ; Commit target to the NES timer register shadow.
    ld a, l
    ld [nes_apu_regs + $02], a
    ld b, h
    ld a, [nes_apu_regs + $03]
    and $F8
    or b
    ld [nes_apu_regs + $03], a

    ld c, l
    ld b, h
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR13], a
    ld a, d
    and $07
    ldh [rNR14], a
    jr .reload

.divider_nonzero:
    ld a, [nes_apu_sweep_reload_p1]
    and a
    jr nz, .reload
    ld a, [nes_apu_sweep_div_p1]
    dec a
    ld [nes_apu_sweep_div_p1], a
    jp nes_apu_refresh_p1

.reload:
    ld a, [nes_apu_regs + $01]
    swap a
    and $07
    ld [nes_apu_sweep_div_p1], a
    xor a
    ld [nes_apu_sweep_reload_p1], a
    jp nes_apu_refresh_p1

nes_apu_clock_sweep_p2:
    ld a, [nes_apu_sweep_div_p2]
    and a
    jr nz, .divider_nonzero

    ld a, [nes_apu_regs + $05]
    bit 7, a
    jr z, .reload
    and $07
    jr z, .reload
    call nes_apu_sweep_target_p2
    jr c, .reload

    ld a, l
    ld [nes_apu_regs + $06], a
    ld b, h
    ld a, [nes_apu_regs + $07]
    and $F8
    or b
    ld [nes_apu_regs + $07], a

    ld c, l
    ld b, h
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR23], a
    ld a, d
    and $07
    ldh [rNR24], a
    jr .reload

.divider_nonzero:
    ld a, [nes_apu_sweep_reload_p2]
    and a
    jr nz, .reload
    ld a, [nes_apu_sweep_div_p2]
    dec a
    ld [nes_apu_sweep_div_p2], a
    jp nes_apu_refresh_p2

.reload:
    ld a, [nes_apu_regs + $05]
    swap a
    and $07
    ld [nes_apu_sweep_div_p2], a
    xor a
    ld [nes_apu_sweep_reload_p2], a
    jp nes_apu_refresh_p2

; Official NES length counter table (bits 3-7 of $4003/$4007/$400B/$400F).
nes_apu_length_table:
    db 10,254, 20,  2, 40,  4, 80,  6, 160,  8, 60, 10, 14, 12, 26, 14
    db 12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30

; NES noise period index 0..F → nearest GB NR43 clock setting.
; NES index 0 is the highest clock and index F the lowest. Values below are
; chosen by nearest log-frequency match using NTSC NES noise periods and the
; GB clock formula 262144 / (divider * 2^shift). Bit 3 remains clear here and
; is ORed in separately when NES short/periodic-noise mode is selected.
nes_apu_noise_nr43:
    db $00, $01, $02, $05, $15, $17, $25, $26
    db $27, $35, $37, $45, $47, $55, $65, $75
