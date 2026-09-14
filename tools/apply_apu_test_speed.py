from pathlib import Path

p = Path("runtime/apu.asm")
s = p.read_text()

anchor = '''; nes_apu_read_status:
;   Output A = $4015-style active-channel status from length counters.
;   DMC/IRQ status is currently unsupported and reads as 0.

'''
insert = '''; nes_apu_read_status:
;   Output A = $4015-style active-channel status from length counters.
;   DMC/IRQ status is currently unsupported and reads as 0.
;
; NES2GBC_APU_TEST_SPEED is a compile-time host-fast-forward compensation.
; 1 = normal hardware timing, 2 = compensate mGBA 2x, 4 = compensate mGBA 4x.
; It changes only the GBC audio renderer/timing, never NES-side register state.
IF !DEF(NES2GBC_APU_TEST_SPEED)
    DEF NES2GBC_APU_TEST_SPEED EQU 1
ENDC

'''
assert anchor in s
s = s.replace(anchor, insert, 1)

old = '''    ld a, [hl]
    bit 7, b
    jr z, .write
    or $08
.write:
    ldh [rNR43], a
'''
new = '''    ld a, [hl]
IF NES2GBC_APU_TEST_SPEED == 2
    ; One NR43 shift step halves the GBC noise clock.
    add $10
ELIF NES2GBC_APU_TEST_SPEED == 4
    ; Two shift steps quarter it for 4x host fast-forward.
    add $20
ENDC
    bit 7, b
    jr z, .write
    or $08
.write:
    ldh [rNR43], a
'''
assert old in s
s = s.replace(old, new, 1)

old = '''    ld a, [hl]
    or $F0
    ret

nes_apu_gb_env_period:
'''
new = '''    ld a, [hl]
IF NES2GBC_APU_TEST_SPEED == 2
    add a
    cp $08
    jr c, .env_scaled
    ld a, $07
.env_scaled:
ELIF NES2GBC_APU_TEST_SPEED == 4
    add a
    add a
    cp $08
    jr c, .env_scaled
    ld a, $07
.env_scaled:
ENDC
    or $F0
    ret

nes_apu_gb_env_period:
'''
assert old in s
s = s.replace(old, new, 1)

old = '''    ; + (t+1) = ((t+1)*75)/64
    pop hl
    ld a, l
    add e
    ld e, a
    ld a, h
    adc d
    ld d, a

    ; GB period divisor cannot exceed 2047.
'''
new = '''    ; + (t+1) = ((t+1)*75)/64
    pop hl
    ld a, l
    add e
    ld e, a
    ld a, h
    adc d
    ld d, a

    ; Fast-forward compensation operates on the GB frequency divisor only.
    ; Doubling the divisor lowers pitch one octave; x4 lowers two octaves.
IF NES2GBC_APU_TEST_SPEED == 2
    sla e
    rl d
ELIF NES2GBC_APU_TEST_SPEED == 4
    sla e
    rl d
    sla e
    rl d
ENDC

    ; GB period divisor cannot exceed 2047.
'''
assert old in s
s = s.replace(old, new, 1)

old = '''; Host VBlank is ~60 Hz, so advance four ~240 Hz frame-sequencer slots each
; VBlank. Keeping phase across calls gives mode 0 four slots (240/120 Hz) and
; mode 1 five slots including its blank step (192/96 Hz average).
nes_apu_frame_tick:
    ld b, 4
'''
new = '''; Host VBlank is ~60 Hz at normal speed. Advance enough ~240 Hz sequencer
; slots to preserve wall-clock APU timing at the selected emulator test speed.
; 1x: 4 slots/VBlank, 2x: 2, 4x: 1. Keeping phase across calls preserves the
; same effective NES quarter/half-frame rates while mGBA is fast-forwarding.
nes_apu_frame_tick:
IF NES2GBC_APU_TEST_SPEED == 2
    ld b, 2
ELIF NES2GBC_APU_TEST_SPEED == 4
    ld b, 1
ELSE
    ld b, 4
ENDC
'''
assert old in s
s = s.replace(old, new, 1)

p.write_text(s)
