from pathlib import Path

path = Path("runtime/apu.asm")
text = path.read_text()


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    text = text.replace(old, new, 1)


replace_once(
    "; nes_apu_read_status:\n;   Output A = $4015-style status (v1: last written enable bits 0-4).",
    "; nes_apu_read_status:\n;   Output A = $4015-style active-channel status from length counters.\n;   DMC/IRQ status is currently unsupported and reads as 0.",
    "status header comment",
)

replace_once(
    "nes_apu_read_status:\n    ld a, [nes_apu_regs + $15]\n    and $1F\n    ret",
    "nes_apu_read_status:\n    ; NES $4015 read bits 0-3 report whether each channel length counter is\n    ; non-zero, not whether its enable bit was last written as 1. DMC and IRQ\n    ; status are not implemented yet, so bits 4, 6, and 7 remain clear.\n    ld b, $00\n    ld a, [nes_apu_len_p1]\n    and a\n    jr z, .status_p2\n    set 0, b\n.status_p2:\n    ld a, [nes_apu_len_p2]\n    and a\n    jr z, .status_tri\n    set 1, b\n.status_tri:\n    ld a, [nes_apu_len_tri]\n    and a\n    jr z, .status_noi\n    set 2, b\n.status_noi:\n    ld a, [nes_apu_len_noi]\n    and a\n    jr z, .status_done\n    set 3, b\n.status_done:\n    ld a, b\n    ret",
    "$4015 read semantics",
)

replace_once(
    "nes_apu_update_pulse1:\n    ld a, [nes_apu_write_idx]\n    cp $03\n    jr nz, .no_preload\n    ld a, [nes_apu_regs + $03]\n    ld hl, nes_apu_len_p1\n    call nes_apu_load_length\n.no_preload:",
    "nes_apu_update_pulse1:\n    ld a, [nes_apu_write_idx]\n    cp $03\n    jr nz, .no_preload\n    ; NES ignores length reloads while this channel is disabled in $4015.\n    ld a, [nes_apu_regs + $15]\n    and $01\n    jr z, .no_preload\n    ld a, [nes_apu_regs + $03]\n    ld hl, nes_apu_len_p1\n    call nes_apu_load_length\n.no_preload:",
    "pulse1 disabled length reload",
)

replace_once(
    "nes_apu_update_pulse2:\n    ld a, [nes_apu_write_idx]\n    cp $07\n    jr nz, .no_preload\n    ld a, [nes_apu_regs + $07]\n    ld hl, nes_apu_len_p2\n    call nes_apu_load_length\n.no_preload:",
    "nes_apu_update_pulse2:\n    ld a, [nes_apu_write_idx]\n    cp $07\n    jr nz, .no_preload\n    ; NES ignores length reloads while this channel is disabled in $4015.\n    ld a, [nes_apu_regs + $15]\n    and $02\n    jr z, .no_preload\n    ld a, [nes_apu_regs + $07]\n    ld hl, nes_apu_len_p2\n    call nes_apu_load_length\n.no_preload:",
    "pulse2 disabled length reload",
)

replace_once(
    "nes_apu_update_triangle:\n    ld a, [nes_apu_write_idx]\n    cp $0B\n    jr nz, .no_preload\n    ld a, [nes_apu_regs + $0B]\n    ld hl, nes_apu_len_tri\n    call nes_apu_load_length\n    ; $400B also reloads the linear counter from $4008 (NES reload flag).\n    ld a, [nes_apu_regs + $08]\n    and $7F\n    ld [nes_apu_linear_tri], a\n.no_preload:",
    "nes_apu_update_triangle:\n    ld a, [nes_apu_write_idx]\n    cp $0B\n    jr nz, .no_preload\n    ; $400B sets the triangle linear reload state regardless, but the NES\n    ; length counter only reloads while triangle is enabled in $4015.\n    ld a, [nes_apu_regs + $15]\n    and $04\n    jr z, .reload_linear\n    ld a, [nes_apu_regs + $0B]\n    ld hl, nes_apu_len_tri\n    call nes_apu_load_length\n.reload_linear:\n    ; Current bridge approximation reloads the working linear counter here.\n    ld a, [nes_apu_regs + $08]\n    and $7F\n    ld [nes_apu_linear_tri], a\n.no_preload:",
    "triangle disabled length reload",
)

replace_once(
    "nes_apu_update_noise:\n    ld a, [nes_apu_write_idx]\n    cp $0F\n    jr nz, .no_preload\n    ld a, [nes_apu_regs + $0F]\n    ld hl, nes_apu_len_noi\n    call nes_apu_load_length\n.no_preload:",
    "nes_apu_update_noise:\n    ld a, [nes_apu_write_idx]\n    cp $0F\n    jr nz, .no_preload\n    ; NES ignores length reloads while this channel is disabled in $4015.\n    ld a, [nes_apu_regs + $15]\n    and $08\n    jr z, .no_preload\n    ld a, [nes_apu_regs + $0F]\n    ld hl, nes_apu_len_noi\n    call nes_apu_load_length\n.no_preload:",
    "noise disabled length reload",
)

replace_once(
    ".neg2:\n    ld a, e\n    or d\n    jr z, .store2\n    ld a, l\n    sub e\n    ld c, a\n    ld a, h\n    sbc d\n    ld b, a\n    jp c, .mute2\n    ld a, c\n    sub $01\n    ld l, a\n    ld a, b\n    sbc $00\n    ld h, a\n    jp c, .mute2",
    ".neg2:\n    ; Pulse 2 uses two's-complement negate: target = period - delta.\n    ; Only pulse 1 applies the extra -1 from its one's-complement adder.\n    ld a, e\n    or d\n    jr z, .store2\n    ld a, l\n    sub e\n    ld l, a\n    ld a, h\n    sbc d\n    ld h, a\n    jp c, .mute2",
    "pulse2 negate sweep",
)

replace_once(
    ".mute2:\n    xor a\n    ldh [rNR22], a\n    ld [nes_apu_sweep2_active], a\n    ld a, $10\n    ld [nes_apu_last_nr12_p2], a\n    ld a, [nes_apu_regs + $05]\n    and $7F\n    ld [nes_apu_regs + $05], a\n    ret",
    ".mute2:\n    xor a\n    ldh [rNR22], a\n    ld [nes_apu_sweep2_active], a\n    ld a, $10\n    ld [nes_apu_last_nr12_p2], a\n    ; Sweep overflow/underflow mutes the channel but does not rewrite $4005.\n    ret",
    "pulse2 sweep mute shadow",
)

replace_once(
    "; NES noise period index 0..F → rough NR43 encoding (lower = higher pitch).\nnes_apu_noise_nr43:\n    ; One octave up from prior table (stage percussion was dull).\n    db $E7, $E3, $D3, $C3, $B3, $A3, $93, $83\n    db $73, $63, $53, $43, $33, $23, $13, $03",
    "; NES noise period index 0..F → nearest GB NR43 clock setting.\n; NES index 0 is the highest clock and index F the lowest. Values below are\n; chosen by nearest log-frequency match using NTSC NES noise periods and the\n; GB clock formula 262144 / (divider * 2^shift). Bit 3 remains clear here and\n; is ORed in separately when NES short/periodic-noise mode is selected.\nnes_apu_noise_nr43:\n    db $00, $01, $02, $05, $15, $17, $25, $26\n    db $27, $35, $37, $45, $47, $55, $65, $75",
    "noise frequency table",
)

replace_once(
    "; $4015: channel enables. Rising edge reloads/triggers that channel.",
    "; $4015: channel enables. Disabling clears length; enabling alone does not retrigger.",
    "$4015 section comment",
)

path.write_text(text)
print("Applied APU correctness pass 1")
