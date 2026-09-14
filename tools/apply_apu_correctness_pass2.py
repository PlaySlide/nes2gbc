from pathlib import Path
import re

p = Path("runtime/apu.asm")
s = p.read_text()


def replace_once(old: str, new: str, name: str):
    global s
    n = s.count(old)
    if n != 1:
        raise SystemExit(f"{name}: expected exactly 1 match, found {n}")
    s = s.replace(old, new, 1)

# State: keep an approximate frame-sequencer phase and a real triangle reload flag.
replace_once(
"""; Frame sequencer divider (host VBlank ticks).
nes_apu_frame_div:  ds 1
""",
"""; Approximate NES frame-counter phase. 4-step uses 0..3, 5-step uses 0..4.
nes_apu_frame_phase: ds 1
; Triangle linear-counter reload flag, set by $400B and consumed on quarter clocks.
nes_apu_tri_reload:  ds 1
""",
"frame state",
)
replace_once(
"""    ld b, $18 + 25         ; regs + prev/idx/lens/linear/lastvol/sweep1+2/frame_div
""",
"""    ld b, $18 + 26         ; regs + prev/idx/lens/linear/lastvol/sweeps/frame phase/tri reload
""",
"init clear size",
)

# $4017 is a real frame-counter control write, not just a shadow register.
replace_once(
"""    cp $15
    jp z, nes_apu_update_status
    ; $10/$12/$13 DMC and $17 frame counter: shadow only for v1.
    ret
""",
"""    cp $15
    jp z, nes_apu_update_status
    cp $17
    jp z, nes_apu_update_frame_counter
    ; $10/$12/$13 DMC registers are shadow-only for now.
    ret
""",
"4017 dispatch",
)

# Pulse timer < 8 mutes output on the NES regardless of sweep clocking.
insert_marker = "; ---------------------------------------------------------------------------\n; Pulse 1 → Square 1 (NR10–NR14)\n"
helpers = r"""; ---------------------------------------------------------------------------
; NES pulse channels are muted whenever their current timer is < 8. These
; helpers gate the GB DAC without changing the emulated length/sweep state.
; Carry is set when muted, clear when the timer is audible.
nes_apu_gate_pulse1_timer:
    ld a, [nes_apu_regs + $03]
    and $07
    jr nz, .audible
    ld a, [nes_apu_regs + $02]
    cp $08
    jr nc, .audible
    xor a
    ldh [rNR12], a
    ld [nes_apu_last_nr12_p1], a
    scf
    ret
.audible:
    and a                           ; clear carry
    ret

nes_apu_gate_pulse2_timer:
    ld a, [nes_apu_regs + $07]
    and $07
    jr nz, .audible
    ld a, [nes_apu_regs + $06]
    cp $08
    jr nc, .audible
    xor a
    ldh [rNR22], a
    ld [nes_apu_last_nr12_p2], a
    scf
    ret
.audible:
    and a                           ; clear carry
    ret

"""
if s.count(insert_marker) != 1:
    raise SystemExit("pulse helper insertion marker not unique")
s = s.replace(insert_marker, helpers + insert_marker, 1)

# Gate pulse volume writes too: a volume update cannot make t<8 audible.
replace_once(
"""    ld [nes_apu_last_nr12_p1], a
    ldh [rNR12], a
    ; If we were silent and now have volume, retrigger so music/SFX ordered
""",
"""    ld [nes_apu_last_nr12_p1], a
    ldh [rNR12], a
    call nes_apu_gate_pulse1_timer
    ret c
    ; If we were silent and now have volume, retrigger so music/SFX ordered
""",
"pulse1 volume timer gate",
)
replace_once(
"""    ld [nes_apu_last_nr12_p2], a
    ldh [rNR22], a
    ld a, c
""",
"""    ld [nes_apu_last_nr12_p2], a
    ldh [rNR22], a
    call nes_apu_gate_pulse2_timer
    ret c
    ld a, c
""",
"pulse2 volume timer gate",
)

# Gate low-byte frequency writes before mapping them to GB frequency.
replace_once(
"""    ld [nes_apu_sweep_period_hi], a
    call nes_apu_timer_to_period
""",
"""    ld [nes_apu_sweep_period_hi], a
    call nes_apu_gate_pulse1_timer
    ret c
    call nes_apu_timer_to_period
""",
"pulse1 frequency timer gate",
)
replace_once(
"""    ld a, [nes_apu_regs + $07]
    and $07
    ld b, a
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR23], a
""",
"""    ld a, [nes_apu_regs + $07]
    and $07
    ld b, a
    call nes_apu_gate_pulse2_timer
    ret c
    call nes_apu_timer_to_period
    ld a, e
    ldh [rNR23], a
""",
"pulse2 frequency timer gate",
)

# High-byte writes retrigger a note, but t<8 still has to remain muted.
replace_once(
""".have_timer:
    ld a, c
    ld [nes_apu_sweep_period_lo], a
""",
""".have_timer:
    ld a, b
    and a
    jr nz, .timer_valid
    ld a, c
    cp $08
    jr nc, .timer_valid
    xor a
    ldh [rNR12], a
    ld [nes_apu_last_nr12_p1], a
    ret
.timer_valid:
    ld a, c
    ld [nes_apu_sweep_period_lo], a
""",
"pulse1 retrigger timer gate",
)
replace_once(
"""nes_apu_retrigger_p2:
    ld a, [nes_apu_regs + $06]
    ld c, a
    ld a, [nes_apu_regs + $07]
    and $07
    ld b, a
    ld a, c
""",
"""nes_apu_retrigger_p2:
    ld a, [nes_apu_regs + $06]
    ld c, a
    ld a, [nes_apu_regs + $07]
    and $07
    ld b, a
    ld a, b
    and a
    jr nz, .timer_valid
    ld a, c
    cp $08
    jr nc, .timer_valid
    xor a
    ldh [rNR22], a
    ld [nes_apu_last_nr12_p2], a
    ret
.timer_valid:
    ld a, c
""",
"pulse2 retrigger timer gate",
)

# Triangle: $400B sets a reload flag; the linear counter reload occurs on the
# next quarter-frame clock, not immediately. $4008 only changes control/value.
replace_once(
""".reload_linear:
    ; Current bridge approximation reloads the working linear counter here.
    ld a, [nes_apu_regs + $08]
    and $7F
    ld [nes_apu_linear_tri], a
""",
""".reload_linear:
    ld a, $01
    ld [nes_apu_tri_reload], a
""",
"triangle reload flag",
)
replace_once(
""".do_linear:
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
""",
""".do_linear:
    ; $4008 changes only control/reload value. The working counter changes on
    ; the next quarter-frame clock if the reload flag is set.
    jp nes_apu_apply_triangle_gate
""",
"triangle 4008 semantics",
)

# Triangle timer writes do not reset phase on the NES. Update GB frequency
# without triggering; the gate helper triggers only when CH3 actually has to
# restart after being halted by a zero length/linear counter.
old_tri_freq = r""".do_freq:
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
"""
new_tri_freq = r""".do_freq:
    ld a, [nes_apu_regs + $0A]
    ld c, a
    ld a, [nes_apu_regs + $0B]
    and $07
    ld b, a
    call nes_apu_timer_to_period
    ; Same octave as pulse mapping (existing bridge tuning retained for now).
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

    ; Preserve the bridge's existing ultrasonic-timer suppression (<2).
    ld a, [nes_apu_regs + $0B]
    and $07
    jr nz, .audible
    ld a, [nes_apu_regs + $0A]
    cp $02
    jr c, .mute

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
"""
replace_once(old_tri_freq, new_tri_freq, "triangle frequency/phase semantics")

# $4015 disabling triangle clears only its length counter; linear state keeps
# running independently under the frame counter.
replace_once(
"""    ldh [rNR30], a
    ld [nes_apu_len_tri], a
    ld [nes_apu_linear_tri], a
.noi:
""",
"""    ldh [rNR30], a
    ld [nes_apu_len_tri], a
    ; $4015 does not clear the triangle linear counter or its reload flag.
.noi:
""",
"4015 triangle state",
)

# Replace the coarse once-per-VBlank clocks with a 4-microstep approximation
# of the NES frame sequencer. This preserves 4-step 240/120 Hz and 5-step
# 192/96 Hz average rates, and honors the common $4017=$C0/$FF resync pattern.
pattern = re.compile(
    r"; ---------------------------------------------------------------------------\n"
    r"; Coarse frame tick from host VBlank.*?\n\n"
    r"; ---------------------------------------------------------------------------\n"
    r"; NES-style pulse1 sweep",
    re.S,
)
frame_impl = r"""; ---------------------------------------------------------------------------
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

; Quarter-frame clocks: NES envelopes also live here, but pulse/noise envelopes
; are still delegated to GB hardware in this bridge. Triangle linear timing is
; software-owned and therefore clocked canonically here.
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
    ; Pulse 1
    ld a, [nes_apu_regs + $00]
    bit 5, a
    jr nz, .p2
    ld a, [nes_apu_len_p1]
    and a
    jr z, .p2
    dec a
    ld [nes_apu_len_p1], a
    jr nz, .p2
    ldh [rNR12], a
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
    ldh [rNR42], a
    ld a, $10
    ld [nes_apu_last_nr12_noi], a
.gate_triangle:
    jp nes_apu_apply_triangle_gate


; ---------------------------------------------------------------------------
; NES-style pulse1 sweep"""
s, n = pattern.subn(frame_impl, s, count=1)
if n != 1:
    raise SystemExit(f"frame sequencer replacement: expected 1 match, found {n}")

p.write_text(s)
print("Applied APU correctness pass 2")
