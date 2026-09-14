from pathlib import Path
import re

p = Path('runtime/apu.asm')
s = p.read_text()

def sub(pattern, repl, count=1):
    global s
    s2, n = re.subn(pattern, repl, s, count=count, flags=re.S)
    if n != count:
        raise SystemExit(f'pattern matched {n}, expected {count}: {pattern[:80]}')
    s = s2

# Replace the accumulated game-tuned state with only NES architectural state.
sub(r'SECTION "NES APU state", WRAM0\[\$CA00\].*?SECTION "NES APU code", ROM0', r'''SECTION "NES APU state", WRAM0[$CA00]
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

SECTION "NES APU code", ROM0''')

s = re.sub(r'ld b, \$18 \+ 26\s*;[^\n]*', 'ld b, $18 + 12         ; register mirror + APU architectural state', s, count=1)

# Generic pulse implementation: no title/SFX-specific behavior.
pulse_block = r'''; ---------------------------------------------------------------------------
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

'''
sub(r'; ---------------------------------------------------------------------------\n; NES pulse channels are muted.*?(?=; ---------------------------------------------------------------------------\n; Triangle)', pulse_block)

# Triangle: allow the full NES timer range; the GB wave channel can represent it.
s = s.replace('''    ; Preserve the bridge's existing ultrasonic-timer suppression (<2).\n    ld a, [nes_apu_regs + $0B]\n    and $07\n    jr nz, .audible\n    ld a, [nes_apu_regs + $0A]\n    cp $02\n    jr c, .mute\n\n.audible:\n''', '''.audible:\n''')
s = s.replace('; Same octave as pulse mapping (existing bridge tuning retained for now).', '; Pulse and triangle use the same NES->GB divisor ratio.')

# Generic noise implementation with no silence->audible SFX heuristic.
noise_block = r'''; ---------------------------------------------------------------------------
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

'''
sub(r'; ---------------------------------------------------------------------------\n; Noise.*?(?=; ---------------------------------------------------------------------------\n; \$4015:)', noise_block)

# $4015 only changes channel enables/length state. Do not repurpose stereo routing.
status_and_vol = r'''; ---------------------------------------------------------------------------
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

'''
sub(r'; ---------------------------------------------------------------------------\n; \$4015:.*?(?=; ---------------------------------------------------------------------------\n; Input:  BC = NES 11-bit timer)', status_and_vol)

# Correct frequency conversion: remove empirical two-octave scaling.
timer_block = r'''; ---------------------------------------------------------------------------
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


'''
sub(r'; ---------------------------------------------------------------------------\n; Input:  BC = NES 11-bit timer.*?(?=; ---------------------------------------------------------------------------\n; A = length/freq hi register value)', timer_block)

# Length clocks are pure NES state transitions; renderer gates follow zero crossings.
length_block = r'''nes_apu_clock_lengths:
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
'''
sub(r'nes_apu_clock_lengths:.*?(?=\n\n; ---------------------------------------------------------------------------\n; NES-style pulse1 sweep)', length_block)

# Canonical sweep divider/reload semantics. No span caps or persistent SFX mute state.
sweep_block = r'''; ---------------------------------------------------------------------------
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

'''
sub(r'; ---------------------------------------------------------------------------\n; NES-style pulse1 sweep.*?(?=; Official NES length counter table)', sweep_block)

# Quarter-frame comment should accurately describe the remaining renderer approximation.
s = s.replace('''; Quarter-frame clocks: NES envelopes also live here, but pulse/noise envelopes\n; are still delegated to GB hardware in this bridge. Triangle linear timing is\n; software-owned and therefore clocked canonically here.''', '''; Quarter-frame clocks: triangle linear timing is software-owned. Pulse/noise\n; envelope decay is rendered by the closest GBC hardware envelope period.''')

# Remove stale game-specific prose if any survived in comments.
for token in ('SMB', 'flagpole', 'fireball', 'death music', 'head-bump', 'blast-style', 'octave cap', 'Jump/SFX'):
    if token.lower() in s.lower():
        raise SystemExit(f'stale game-specific customization remains: {token}')

p.write_text(s)
