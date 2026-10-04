; Canonical NES CPU state and helper routines.
; Correctness-first implementation. Later passes may cache A/X/Y/P in host registers.

DEF NES_RAM_BASE EQU $C000

SECTION "NES CPU hot state", HRAM[$FF80]
nes_a:  ds 1
nes_x:  ds 1
nes_y:  ds 1
nes_sp: ds 1
nes_p:  ds 1
; Lazy status shadows. Z is set iff nes_z_shadow == 0; N follows bit 7.
nes_z_shadow: ds 1
nes_n_shadow: ds 1
nes_c_shadow: ds 1

SECTION "NES CPU helpers", ROM0

; Input: A = result value. Output: A preserved.
; Z/N are lazy: Z iff shadow == 0, N from shadow bit 7.
nes_set_nz_from_a:
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ret

; Materialize lazy C/Z/N into nes_p. Output A = complete 6502 P.
nes_materialize_p:
    ldh a, [nes_p]
    and $7C
    ld e, a

    ldh a, [nes_c_shadow]
    and a
    jr z, .no_c
    ld a, e
    or $01
    ld e, a
.no_c:
    ldh a, [nes_z_shadow]
    and a
    jr nz, .no_z
    ld a, e
    or $02
    ld e, a
.no_z:
    ldh a, [nes_n_shadow]
    bit 7, a
    jr z, .no_n
    ld a, e
    or $80
    ld e, a
.no_n:
    ld a, e
    ldh [nes_p], a
    ret

; Input A = popped/restored P. Normalize B/U and refresh lazy C/Z/N.
; Output A = normalized P.
nes_set_p_from_a:
    or $20
    and $EF
    ld e, a
    ldh [nes_p], a

    ld a, e
    and $01
    ldh [nes_c_shadow], a

    bit 1, e
    ld a, $01
    jr z, .z_ready
    xor a
.z_ready:
    ldh [nes_z_shadow], a

    ld a, e
    ldh [nes_n_shadow], a
    ret

; Compare canonical accumulator-like value in A against E.
; Updates lazy C/Z/N shadows from the subtraction.
nes_compare_a_e:
    PROFILE_INC nes_profile_compare
    ld d, a
    sub e
    ld c, a

    ld a, d
    cp e
    ld a, $00
    jr c, .carry_ready
    inc a
.carry_ready:
    ldh [nes_c_shadow], a

    ld a, c
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ret

; Virtual 6502 stack lives in mirrored RAM page $0100 at $C100.
; Push stores then decrements SP.
nes_stack_push_a:
    ld e, a
    ldh a, [nes_sp]
    ld l, a
    ld h, $C1
    ld a, e
    ld [hl], a
    ldh a, [nes_sp]
    dec a
    ldh [nes_sp], a
    ret

; Pop increments SP then reads.
nes_stack_pop_a:
    ldh a, [nes_sp]
    inc a
    ldh [nes_sp], a
    ld l, a
    ld h, $C1
    ld a, [hl]
    ret

; 6502 JSR pushes high byte then low byte of PC-1/return address.
; Input: HL = 6502 return address (address of last JSR operand byte).
nes_stack_push_return_hl:
    PROFILE_INC nes_profile_jsr_push
    ld b, h
    ld c, l
    ld a, b
    call nes_stack_push_a
    ld a, c
    call nes_stack_push_a
    ret

; Output: HL = stacked 6502 return address. RTS increments it before dispatch.
nes_stack_pop_return_hl:
    PROFILE_INC nes_profile_rts_pop
    call nes_stack_pop_a
    ld c, a
    call nes_stack_pop_a
    ld h, a
    ld l, c
    ret

; Input A = unsigned 8-bit offset, HL = base. Output HL += A.
nes_add_a_to_hl:
    add l
    ld l, a
    ret nc
    inc h
    ret

; Convert NES internal RAM address in HL to GBC WRAM mirror.
; Only valid for CPU addresses $0000-$1FFF.
nes_map_cpu_addr_hl:
    ld a, h
    and $07
    or $C0
    ld h, a
    ret

; Bank-safe translated-PC dispatcher.
; Dispatch tables occupy ROM banks 32-39. Each table bank covers $1000 NES addresses.
; Input HL = NES PC.
nes_dispatch_hl:
    PROFILE_INC nes_profile_dispatch
IF DEF(NES2GBC_DEBUG_TRACE)
    ; Record every requested NES PC before this routine repurposes HL for the
    ; dispatch-table lookup. If we hang, mGBA can show the exact missing target.
    ld a, h
    ld [nes_debug_pc_hi], a
    ld a, l
    ld [nes_debug_pc_lo], a
    xor a
    ld [nes_debug_fault], a
ENDC

    ld a, h
    cp $80
    jp c, nes_unimplemented

    ; Direct-mapped cache of resolved translations: 128 entries indexed by
    ; (lo - hi) & $7F (fewer hot collisions than lo & $7F), tagged by
    ; hi ^ ((lo - hi) & $80) so the tag and index identify the PC exactly
    ; (hi >= $80). Empty entries carry tag 0 and point at
    ; nes_dispatch_dm_empty, which takes the miss path. Hits avoid the
    ; dispatch-table bank switch and lookup. Clobbers B/E like the miss path.
    ld b, h
    ld e, l
    ld a, l
    sub h
    ld l, a
    and $80
    xor b
    res 7, l
    ld h, HIGH(nes_dispatch_dm_tag)
    cp [hl]
    jr nz, .dm_miss
    set 7, l
    ld a, [hl]
    ld [nes_current_code_bank], a
    ld [$2000], a
    inc h
    ld a, [hl]
    res 7, l
    ld l, [hl]
    ld h, a
    jp hl

.dm_miss:
    ld h, b
    ld l, e

.cache_miss:
    ; Cache-key state is independent from optional debug breadcrumbs.
    ld a, h
    ld [nes_dispatch_cache_pc_hi], a
    ld a, l
    ld [nes_dispatch_cache_pc_lo], a

    ; Table bank = high nibble($8-$F) + $18 => banks $20-$27 (32-39).
    ld a, h
    swap a
    and $0F
    add $18
    ld [$2000], a
    xor a
    ld [$3000], a

    ; Table offset = (PC & $0FFF) * 4, mapped into ROMX $4000-$7FFF.
    ld a, h
    and $0F
    ld h, a
    add hl, hl
    add hl, hl
    set 6, h

    ld a, [hli]
    and a
    jp z, nes_unimplemented
    ld b, a

    ; Skip reserved high-bank byte.
    inc hl
    ld a, [hli]
    ld e, a
    ld a, [hl]
    ld d, a

    ; Cache the resolved translation before switching back to its code bank.
    ld a, $01
    ld [nes_dispatch_cache_valid], a
    ld a, b
    ld [nes_dispatch_cache_bank], a
    ld a, d
    ld [nes_dispatch_cache_addr_hi], a
    ld a, e
    ld [nes_dispatch_cache_addr_lo], a

    ; Fill the direct-mapped cache entry for this PC.
    ld a, [nes_dispatch_cache_pc_hi]
    ld c, a
    ld a, [nes_dispatch_cache_pc_lo]
    sub c
    ld l, a
    and $80
    xor c
    res 7, l
    ld h, HIGH(nes_dispatch_dm_tag)
    ld [hl], a
    set 7, l
    ld [hl], b
    inc h
    ld [hl], d
    res 7, l
    ld [hl], e

    ld a, b
    ld [nes_current_code_bank], a
    ld [$2000], a
    xor a
    ld [$3000], a

    ld h, d
    ld l, e
    jp hl

; Target of empty direct-mapped dispatch entries (tag 0 is a valid tag, for
; hi=$80 with (lo-hi)&$80 set): resolve through the full lookup instead.
nes_dispatch_dm_empty::
    ld h, b
    ld l, e
    jp nes_dispatch_hl.cache_miss

; Fast path for statically known cross-bank transfers.
; Input: A = translated code bank, HL = linked ROMX target address.
nes_jump_known_hl_a:
    ld [nes_current_code_bank], a
    ld [$2000], a
    xor a
    ld [$3000], a
    jp hl

nes_restore_code_bank:
    ld a, [nes_current_code_bank]
    ld [$2000], a
    xor a
    ld [$3000], a
    ret

nes_unimplemented:
    ; Snapshot the computed-control-flow state before stopping. This has zero
    ; cost in normal execution and is especially useful for SMB's JumpEngine,
    ; which leaves its caller pointer in $04/$05 and computed target in $06/$07.
    ld a, [nes_dispatch_cache_pc_lo]
    ldh [nes_fault_target_lo], a
    ld a, [nes_dispatch_cache_pc_hi]
    ldh [nes_fault_target_hi], a
    ldh a, [nes_sp]
    ldh [nes_fault_sp_snapshot], a
    ldh a, [nes_a]
    ldh [nes_fault_a_snapshot], a
    ldh a, [nes_x]
    ldh [nes_fault_x_snapshot], a
    ldh a, [nes_y]
    ldh [nes_fault_y_snapshot], a

    ld a, [$C004]
    ldh [nes_fault_zp04_snapshot], a
    ld a, [$C005]
    ldh [nes_fault_zp05_snapshot], a
    ld a, [$C006]
    ldh [nes_fault_zp06_snapshot], a
    ld a, [$C007]
    ldh [nes_fault_zp07_snapshot], a

    ld a, $01
    ldh [nes_fault_kind], a
    ld a, $FF
    ldh [nes_fault_hram], a
    ld [nes_debug_fault], a
    di
.hang:
    halt
    jr .hang

; Cache a mirrored 16 KiB NES PRG into CGB WRAMX banks 2-5.
; This is a one-time startup cost for NROM-style 16 KiB cartridges and avoids
; destructive MBC ROM-bank switches on every later PRG data-table read.
nes_cache_prg16_to_wram:
    ld a, $01
    ld [$2000], a
    xor a
    ld [$3000], a

    ld de, $4000
    ld a, $02
.copy_bank:
    ldh [rSVBK], a
    push af
    ld hl, $D000
    ld bc, $1000
.copy_byte:
    ld a, [de]
    ld [hli], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .copy_byte
    pop af
    inc a
    cp $06
    jr c, .copy_bank
    ret

; Generic CPU read. Input HL = NES CPU address, output A = value.
nes_cpu_read:
    PROFILE_INC nes_profile_cpu_read
IF DEF(NES2GBC_DEBUG_TRACE)
    ld a, h
    ld [nes_debug_bus_hi], a
    ld a, l
    ld [nes_debug_bus_lo], a
ENDC
    ld a, h
IF DEF(NES2GBC_PROFILE)
    cp $20
    jp c, .ram
    cp $40
    jp c, .ppu
    cp $80
    jp nc, .prg
    cp $60
    jp nc, .prg_ram

    ; APU / controller register reads.
    cp $40
    jp nz, .unsupported
    ld a, l
    cp $11
    jp z, .read_4011
IF DEF(NES2GBC_APU)
    cp $15
    jp z, .read_4015
ENDC
    cp $16
    jp z, .read_4016
    cp $17
    jp z, .read_4017
    jp .unsupported
ELSE
    cp $20
    jr c, .ram
    cp $40
    jr c, .ppu
    cp $80
    jr nc, .prg
    cp $60
    jr nc, .prg_ram

    ; APU / controller register reads.
    cp $40
    jp nz, .unsupported
    ld a, l
    cp $11
    jr z, .read_4011
IF DEF(NES2GBC_APU)
    cp $15
    jr z, .read_4015
ENDC
    cp $16
    jr z, .read_4016
    cp $17
    jr z, .read_4017
    jp .unsupported
ENDC

.ram:
    PROFILE_INC nes_profile_read_ram
    ; NES $0000-$1FFF mirrors 2 KiB internal RAM. Map it directly instead
    ; of paying another CALL/RET through nes_map_cpu_addr_hl.
    ld a, h
    and $07
    or $C0
    ld h, a
    ld a, [hl]
    ret

.ppu:
    PROFILE_INC nes_profile_read_ppu
    ld a, l
    and $07
    ld l, a
    jp nes_ppu_cpu_read

.prg_ram:
    PROFILE_INC nes_profile_read_other
    push bc
    ldh a, [rSVBK]
    push af
    ld a, h
    bit 4, a
    ld a, $04
    jr z, .prg_ram_read_bank
    inc a
.prg_ram_read_bank:
    ldh [rSVBK], a
    ld a, h
    and $0F
    or $D0
    ld h, a
    ld b, [hl]
    pop af
    ldh [rSVBK], a
    ld a, b
    pop bc
    ret

.prg:
    PROFILE_INC nes_profile_read_prg

    ; UxROM: $8000-$BFFF is selected by the mapper register while
    ; $C000-$FFFF is permanently the last 16 KiB PRG bank. Raw NES PRG
    ; banks are embedded one-for-one in GBC ROM banks starting at bank 1.
    ld a, [nes_mapper]
    cp $02
    jr z, .prg_mapper2

    ; Mirrored 16 KiB PRG is cached in WRAMX banks 2-5 at startup.
    ld a, [nes_prg_16k_mirror]
    and a
    jr z, .prg_banked_rom

    ; NES $8000-$BFFF and $C000-$FFFF both mirror the same 16 KiB image.
    ; Bits 13-12 choose the 4 KiB WRAM bank; low 12 bits select the byte.
    ld a, h
    and $30
    swap a
    add $02
    ldh [rSVBK], a
    ld a, h
    and $0F
    or $D0
    ld h, a
    ld a, [hl]
    ret

.prg_mapper2:
    ld a, h
    cp $C0
    jr c, .prg_mapper2_switchable
    ld a, [nes_prg_fixed_bank]
    jr .prg_mapper2_have_bank
.prg_mapper2_switchable:
    ld a, [nes_prg_bank]
.prg_mapper2_have_bank:
    ; Physical PRG bank N is stored in GBC ROM bank N+1.
    inc a
    ld [$2000], a
    xor a
    ld [$3000], a

    ; A 16 KiB NES window maps directly onto ROMX $4000-$7FFF.
    ld a, h
    and $3F
    or $40
    ld h, a
    ld l, [hl]

    ; Generic PRG reads are temporary data-bank switches. Restore the
    ; translated-code bank before returning to generated code.
    ld a, [nes_current_code_bank]
    ld [$2000], a
    xor a
    ld [$3000], a
    ld a, l
IF DEF(NES2GBC_DEBUG_TRACE)
    ld [nes_debug_bus_value], a
ENDC
    ret

.prg_banked_rom:
    ; Select ROMX bank 1 ($8000-$BFFF) or 2 ($C000-$FFFF), map the offset to
    ; $4000-$7FFF, read, then restore the translated-code bank inline. The
    ; upper MBC5 bank bit ($3000) is always 0 in this runtime. Returned flags
    ; are those of `or $40` (NZ, NC), exactly as the old push/pop path left.
    ld a, h
    rlca
    rlca
    and $01
    inc a
.prg_select:
    ld [$2000], a
    ld a, h
    and $3F
    or $40
    ld h, a
    ld l, [hl]
    ld a, [nes_current_code_bank]
    ld [$2000], a
    ld a, l
IF DEF(NES2GBC_DEBUG_TRACE)
    ld [nes_debug_bus_value], a
ENDC
    ret

.read_4011:
    PROFILE_INC nes_profile_read_io
    ld a, [nes_dac]
    ret

IF DEF(NES2GBC_APU)
.read_4015:
    PROFILE_INC nes_profile_read_io
    jp nes_apu_read_status
ENDC

.read_4016:
    PROFILE_INC nes_profile_read_io
    jp nes_controller_read


.read_4017:
    PROFILE_INC nes_profile_read_io
    ; No second controller is connected yet. During the first eight serial
    ; joypad reads an unpressed controller must contribute zero button bits.
    ; Returning $01 here made games such as SMB see controller 2 as $FF
    ; (every button held), which masks Start/Select from controller 1.
    xor a
    ret

.unsupported:
    PROFILE_INC nes_profile_read_other
    xor a
    ret

; Emitted for LDA/LDX/LDY $4016,X / $4017,X (and ,Y): HL = base + index.
; Resolves the controller ports without the generic address ladder; any
; other effective address takes the full nes_cpu_read path. Results and
; returned flags match nes_cpu_read for every HL.
nes_cpu_read_joy_hl::
    ld a, h
    cp $40
    jp nz, nes_cpu_read
    ld a, l
    cp $16
    jp z, nes_controller_read
    cp $17
    jp nz, nes_cpu_read
    xor a
    ret

; Emitted (tools/fast_nonram_reads.py) where the inline $0000-$1FFF RAM test
; already failed, so H >= $20: PRG goes straight to the PRG path, anything
; else takes the generic ladder. Same results and flags as nes_cpu_read.
nes_cpu_read_hi::
    bit 7, h
    jp nz, nes_cpu_read.prg
    jp nes_cpu_read

; 32 KiB PRG variant of nes_cpu_read_hi (tools/inline_prg_reads.py): the
; build knows PRG is not the mirrored 16 KiB layout, so skip that test.
; Same results and flags as nes_cpu_read (PRG: `or $40` -> NZ, NC).
nes_cpu_read_hi32::
    bit 7, h
    jp z, nes_cpu_read
    PROFILE_INC nes_profile_read_prg
    ld a, h
    rlca
    rlca
    and $01
    inc a
    jp nes_cpu_read.prg_select

; Native serial joypad loop (tools/native_joypad_loops.py):
;   loop: PHA / LDA $40D,X / STA $00E / LSR / ORA $00E / LSR / PLA / ROL /
;         DEY / BNE loop
; In: D = low byte of the port base ($16/$17), E = zero-page address.
; Runs NES Y iterations (0 = 256). Out (HRAM state): A, C = last ROL carry,
; Y = 0, Z set / N clear (DEY), [zp] = last read, stack byte at SP = last
; pushed A. X, V, SP unchanged. Clobbers AF/BC/DE/HL.
nes_joy_serial_loop::
    ; Fast path: the loop-invariant address is $4016 and the strobe is low
    ; (only a write can change it), so each read returns bit 0 of the shift
    ; register and shifts in a 1 at bit 7. Only the last iteration's stack
    ; byte (A before the final ROL) and zero-page byte survive.
    ldh a, [nes_x]
    add d
    jp c, .generic
    cp $17
    jp z, .fast4017
    cp $16
    jp nz, .generic
    ld a, [nes_controller_strobe]
    and a
    jp nz, .generic
    ldh a, [nes_y]
    cp $07
    jr z, .fast7
    ld b, a
    ldh a, [nes_a]
    ld c, a
    ld a, [nes_controller_shift]
    ld d, a
.fast:
    ld l, c
    srl d
    set 7, d
    rl c
    dec b ; DEC keeps the ROL carry
    jr nz, .fast
    ld a, $00
    rla
    ldh [nes_c_shadow], a
    ld b, l
    ldh a, [nes_sp]
    ld l, a
    ld h, $C1
    ld [hl], b
    ld a, c
    and $01
    ld h, $C0
    ld l, e
    ld [hl], a
    ld a, d
    ld [nes_controller_shift], a
    jp .done
.fast7:
    ; Y = 7 (ReadPortBits after its first, translated iteration) in closed
    ; form: A' = A.0 << 7 | rev(shift) >> 1, C = A.1, last PHA byte =
    ; (A & 3) << 6 | rev(shift) >> 2, zp = A'.0, shift' = shift >> 7 | $FE.
    ld a, [nes_controller_shift]
    ld l, a
    rlca
    or $FE
    ld [nes_controller_shift], a
    ld h, HIGH(nes_bit_reverse)
    ld a, [hl]
    srl a
    ld b, a
    ldh a, [nes_a]
    ld c, a
    rrca
    rrca
    ld d, a
    ld a, $00
    rla
    ldh [nes_c_shadow], a
    ld a, d
    and $C0
    ld d, a
    ld a, b
    srl a
    or d
    ld d, a
    ldh a, [nes_sp]
    ld l, a
    ld h, $C1
    ld [hl], d
    ld a, b
    and $01
    ld h, $C0
    ld l, e
    ld [hl], a
    ld a, c
    rrca
    and $80
    or b
    ld c, a
    jp .done
.fast4017:
    ; $4017 reads are 0 (nes_cpu_read_joy_hl): each bit shifts a 0 into A.
    ldh a, [nes_y]
    cp $07
    jr z, .fast17_7
    ld b, a
    ldh a, [nes_a]
    ld c, a
.fast17_loop:
    ld l, c
    sla c
    dec b
    jr nz, .fast17_loop
    ld a, $00
    rla
    ldh [nes_c_shadow], a
    ld b, l
    ldh a, [nes_sp]
    ld l, a
    ld h, $C1
    ld [hl], b
    ld h, $C0
    ld l, e
    ld [hl], $00
    jp .done
.fast17_7:
    ; Y = 7: A' = A.0 << 7, C = A.1, last PHA byte = (A & 3) << 6, zp = 0.
    ldh a, [nes_a]
    ld c, a
    rrca
    rrca
    ld b, a
    ld a, $00
    rla
    ldh [nes_c_shadow], a
    ld a, b
    and $C0
    ld b, a
    ldh a, [nes_sp]
    ld l, a
    ld h, $C1
    ld [hl], b
    ld h, $C0
    ld l, e
    ld [hl], $00
    ld a, c
    rrca
    and $80
    ld c, a
    jp .done
.generic:
    ldh a, [nes_y]
    ld b, a
    ldh a, [nes_a]
    ld c, a
.loop:
    ; PHA ... PLA leaves SP unchanged but the pushed byte stays in memory.
    ldh a, [nes_sp]
    ld l, a
    ld h, $C1
    ld [hl], c
    ldh a, [nes_x]
    add d
    ld l, a
    ld a, $40
    adc 0
    ld h, a
    push bc
    push de
    call nes_cpu_read_joy_hl
    pop de
    pop bc
    ld h, $C0
    ld l, e
    ld [hl], a
    ; carry = bit0 of (v | v >> 1), rotated into the pulled A.
    ld l, a
    srl a
    or l
    rra
    rl c
    dec b ; DEC keeps the ROL carry
    jr nz, .loop
    ld a, $00
    rla
    ldh [nes_c_shadow], a
.done:
    ld a, c
    ldh [nes_a], a
    xor a
    ldh [nes_y], a
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ret

; Generic CPU write. Input HL = NES CPU address, A = value.
nes_cpu_write:
    PROFILE_INC nes_profile_cpu_write
    ld e, a
    ld a, h
IF DEF(NES2GBC_PROFILE)
    cp $20
    jp c, .ram
    cp $40
    jp c, .ppu
    cp $80
    jp nc, .mapper
    cp $60
    jp nc, .prg_ram

    cp $40
    jp nz, .unsupported
    ld a, l
    cp $11
    jp z, .write_4011
    cp $14
    jp z, .write_4014
    cp $16
    jp z, .write_4016
IF DEF(NES2GBC_APU)
    cp $18
    jp c, .write_apu
ENDC
    jp .unsupported
ELSE
    cp $20
    jr c, .ram
    cp $40
    jr c, .ppu
    cp $80
    jr nc, .mapper
    cp $60
    jr nc, .prg_ram

    cp $40
    jp nz, .unsupported
    ld a, l
    cp $11
    jr z, .write_4011
    cp $14
    jr z, .write_4014
    cp $16
    jr z, .write_4016
IF DEF(NES2GBC_APU)
    cp $18
    jr c, .write_apu
ENDC
    jp .unsupported
ENDC

.ram:
    PROFILE_INC nes_profile_write_ram
    ld a, h
    and $07
    or $C0
    ld h, a
    ld a, e
    ld [hl], a
    ret

.ppu:
    PROFILE_INC nes_profile_write_ppu
    ld a, l
    and $07
    ld l, a
    jp nes_ppu_cpu_write

.prg_ram:
    PROFILE_INC nes_profile_write_other
    ldh a, [rSVBK]
    push af
    ld a, h
    bit 4, a
    ld a, $04
    jr z, .prg_ram_write_bank
    inc a
.prg_ram_write_bank:
    ldh [rSVBK], a
    ld a, h
    and $0F
    or $D0
    ld h, a
    ld a, e
    ld [hl], a
    pop af
    ldh [rSVBK], a
    ret

.mapper:
    PROFILE_INC nes_profile_write_mapper
    ld a, [nes_mapper]
    cp $02
    jr z, .mapper2
    ; CNROM writes anywhere in $8000-$FFFF select the 8 KiB CHR bank.
    cp $03
    ret nz
    ld a, [nes_chr_bank_mask]
    and e
    ld b, a
    ld a, [nes_chr_bank]
    cp b
    ret z
    ld a, b
    ld [nes_chr_bank], a
    call nes_upload_chr_bank
    ret

.mapper2:
    ; UxROM writes anywhere in $8000-$FFFF select the 16 KiB bank visible
    ; at $8000-$BFFF. The fixed high bank never changes.
    ld a, [nes_prg_bank_mask]
    and e
    ld b, a
.mapper2_valid:
    ld a, b
    ld [nes_prg_bank], a
    ret

.write_4011:
    PROFILE_INC nes_profile_write_io
    ld a, e
    ld [nes_dac], a
    ret

IF DEF(NES2GBC_APU)
.write_apu:
    PROFILE_INC nes_profile_write_io
    ; L = register low byte, E = value (already set).
    jp nes_apu_write
ENDC

.write_4014:
    PROFILE_INC nes_profile_write_io
    ld a, e
    jp nes_oam_dma

.write_4016:
    PROFILE_INC nes_profile_write_io
    ld a, e
    jp nes_controller_write

.unsupported:
    PROFILE_INC nes_profile_write_other
    ret


; Input: A = lhs, E = rhs. Uses lazy 6502 carry-in.
; Output: A = result, updates lazy C/Z/N and V in nes_p.
nes_adc_a_e:
    PROFILE_INC nes_profile_adc
nes_adc_core:
    ld d, a
    ldh a, [nes_c_shadow]
    and a
    jr z, .adc_clear_c
    scf
    jr .adc_go
.adc_clear_c:
    and a
.adc_go:
    ld a, d
    adc e
    ld c, a
    ld a, $00
    jr nc, .adc_captured
    inc a
.adc_captured:
    ldh [nes_c_shadow], a

    ; Only overflow remains material in nes_p; C/Z/N are lazy shadows.
    ldh a, [nes_p]
    and $BF
    ld b, a

    ; overflow = ~(lhs ^ rhs) & (lhs ^ result) & $80
    ld a, d
    xor e
    cpl
    ld h, a
    ld a, d
    xor c
    and h
    and $80
    jr z, .adc_no_overflow
    ld a, b
    or $40
    ld b, a
.adc_no_overflow:
    ld a, b
    ldh [nes_p], a

    ld a, c
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ret

; SBC on the 6502 is lhs + (~rhs) + C.
; Preserve the lhs accumulator while complementing E; the old implementation
; clobbered A with ~rhs before nes_adc_a_e captured its left-hand operand.
nes_sbc_a_e:
    PROFILE_INC nes_profile_sbc
    ld d, a
    ld a, e
    cpl
    ld e, a
    ld a, d
    jp nes_adc_core

; BIT: Z comes from A&E, N from operand bit7, V from operand bit6.
nes_bit_a_e:
    PROFILE_INC nes_profile_bit
    ld d, a

    ld a, d
    and e
    ldh [nes_z_shadow], a
    ld a, e
    ldh [nes_n_shadow], a

    ldh a, [nes_p]
    and $BF
    bit 6, e
    jr z, .bit_no_v
    or $40
.bit_no_v:
    ldh [nes_p], a
    ret

; Shift/rotate helpers. Input/output A, update lazy 6502 C/N/Z.
nes_asl_a:
    ld e, a
    bit 7, e
    ld a, $00
    jr z, .asl_c_ready
    inc a
.asl_c_ready:
    ldh [nes_c_shadow], a
    ld a, e
    add a
    jp nes_set_nz_from_a

nes_lsr_a:
    ld e, a
    bit 0, e
    ld a, $00
    jr z, .lsr_c_ready
    inc a
.lsr_c_ready:
    ldh [nes_c_shadow], a
    ld a, e
    srl a
    jp nes_set_nz_from_a

nes_rol_a:
    ld d, a
    ldh a, [nes_c_shadow]
    ld c, a

    bit 7, d
    ld a, $00
    jr z, .rol_c_ready
    inc a
.rol_c_ready:
    ldh [nes_c_shadow], a

    ld a, d
    add a
    ld e, a
    ld a, c
    and $01
    or e
    jp nes_set_nz_from_a

nes_ror_a:
    ld d, a
    ldh a, [nes_c_shadow]
    ld c, a

    bit 0, d
    ld a, $00
    jr z, .ror_c_ready
    inc a
.ror_c_ready:
    ldh [nes_c_shadow], a

    ld a, d
    srl a
    ld e, a
    ld a, c
    and $01
    jr z, .ror_no_old_c
    ld a, e
    or $80
    jp nes_set_nz_from_a
.ror_no_old_c:
    ld a, e
    jp nes_set_nz_from_a

; 6502 JMP (indirect), including the NMOS page-wrap bug.
; Input HL = pointer address. Output HL = fetched target.
nes_jmp_indirect_hl:
    push hl
    call nes_cpu_read
    ld b, a
    pop hl

    ; Increment only the low byte; $xxFF wraps to $xx00.
    inc l
    call nes_cpu_read
    ldh [nes_last_indirect_hi], a
    ld h, a
    ld a, b
    ldh [nes_last_indirect_lo], a
    ld l, b
    ret

; BRK pushes return PC high/low, then status with B set, and sets I.
; Input HL = PC after BRK's padding byte.
nes_brk_hl:
    ld b, h
    ld c, l
    ld a, b
    call nes_stack_push_a
    ld a, c
    call nes_stack_push_a

    call nes_materialize_p
    or $30
    call nes_stack_push_a

    ldh a, [nes_p]
    or $04
    and $EF
    or $20
    ldh [nes_p], a
    ret

; RTI pops P then PC low/high. Output HL = exact restored PC.
nes_rti_pop_hl:
    xor a
    ld [nes_nmi_active], a
IF !DEF(NES2GBC_NO_PACING)
    ; This NES frame is complete but not yet published. If a host VBlank
    ; already elapsed while it ran, let the next NMI start right away.
    inc a
    ld [nes_pace_unpublished], a
    ld a, [nes_pace_credit]
    and a
    jr z, .no_credit
    ld a, [nes_pace_idle_hi]
    and a
    jr z, .no_credit
IF DEF(NES2GBC_CATCHUP)
    ; Spend one banked VBlank (up to NES2GBC_CATCHUP_MAX accumulate).
    ld a, [nes_pace_credit]
    dec a
    ld [nes_pace_credit], a
    ld a, $01
ELSE
    xor a
    ld [nes_pace_credit], a
    inc a
ENDC
    ld [nes_pace_armed], a
    ldh [nes_host_vblank_pending], a
.no_credit:
ENDC
    call nes_stack_pop_a
    call nes_set_p_from_a

    call nes_stack_pop_a
    ld c, a
    call nes_stack_pop_a
    ld h, a
    ld l, c
    ret

; Deliver a latched host VBlank as a translated NES NMI at compiler-selected
; safe points. Input HL = NES PC to resume if interrupted.
; Output A = 1 when caller should jump to the translated NMI handler.
nes_poll_nmi_hl:
    ldh a, [nes_host_vblank_pending]
    and a
    ret z
IF !DEF(NES2GBC_NO_PACING)
    ; An early (paced) NMI start must not preempt main-thread work that on
    ; hardware would finish before the next NMI: only start it once the game
    ; is back in its idle loop. Keep the event pending otherwise.
    ld a, [nes_pace_armed]
    and a
    jr z, .pace_any_pc
    ld a, [nes_pace_idle_lo]
    cp l
    jr nz, .pace_not_idle
    ld a, [nes_pace_idle_hi]
    cp h
    jr nz, .pace_not_idle
    xor a
    ld [nes_pace_armed], a
    jr .pace_any_pc
.pace_not_idle:
    xor a
    ret
.pace_any_pc:
ENDC

    ; Consume the host event. If NES NMI is disabled or already active, this
    ; frame is intentionally dropped instead of creating back-to-back NMIs.
    xor a
    ldh [nes_host_vblank_pending], a

    ld a, [nes_ppuctrl]
    bit 7, a
IF !DEF(NES2GBC_NO_PACING)
    jp z, .no_nmi ; out of JR range with pacing + debug flavours
ELSE
    jr z, .no_nmi
ENDC

    ld a, [nes_nmi_active]
    and a
IF !DEF(NES2GBC_NO_PACING)
    jp nz, .no_nmi
ELSE
    jr nz, .no_nmi
ENDC

    ld a, $01
    ld [nes_nmi_active], a

IF !DEF(NES2GBC_NO_PACING)
    ; Early start while the previous frame is still unpublished: snapshot it
    ; for the VBlank ISR and keep its queued nametable entries (the next frame
    ; appends to the same queue; values come from authoritative WRAM).
    ld a, [nes_pace_unpublished]
    and a
    jr z, .pace_normal
    push hl
    call nes_pace_take_snapshot
    pop hl
    ld a, $01
    ld [nes_pace_early], a
    jp .stage_seen_clear_done
.pace_normal:
    ; VBlank-triggered start: two in a row at the same resume PC identify
    ; the idle loop.
    ld a, [nes_pace_cand_lo]
    cp l
    jr nz, .pace_new_cand
    ld a, [nes_pace_cand_hi]
    cp h
    jr nz, .pace_new_cand
    ld a, l
    ld [nes_pace_idle_lo], a
    ld a, h
    ld [nes_pace_idle_hi], a
.pace_new_cand:
    ld a, l
    ld [nes_pace_cand_lo], a
    ld a, h
    ld [nes_pace_cand_hi], a
ENDC

    ; Begin a fresh all-or-nothing nametable transaction for this NES NMI.
    ; The previous transaction was published before the host VBlank that
    ; generated this NMI event.
    xor a
    ld [nes_nametable_queue_ptr_lo], a
    ld [nes_nametable_queue_overflow], a
    ld a, $D8
    ld [nes_nametable_queue_ptr_hi], a

    ; Only SMB-style stitched NMIs use the duplicate-address bitmap.
    ; Clearing all 256 bytes on every NMI was pure overhead for DK/IC/BF and
    ; lengthened the interval in which their live map writes could reach host
    ; visible scanout.
    ld a, [nes_nametable_stage_used]
    and a
    jr z, .stage_seen_clear_done

    xor a
    ld [nes_nametable_stage_used], a
    call nes_stage_new_generation

.stage_seen_clear_done:
    xor a
    ldh [nes_scroll_pair_count], a
    ; Once a two-state raster split has been proven, keep it latched. Some
    ; games do not rewrite both scroll states on every NMI.
    PROFILE_INC nes_profile_nmi

    ; Hardware interrupt stack frame: PC high, PC low, P with B clear
    ; (three nes_stack_push_a inlined; nes_materialize_p keeps HL, E ends
    ; holding the pushed P as before).
    ld b, h
    ld c, l
    ldh a, [nes_sp]
    ld l, a
    ld h, $C1
    ld [hl], b
    dec l
    ld [hl], c
    dec l
    call nes_materialize_p
    and $EF
    or $20
    ld [hl], a
    ld e, a
    dec l
    ld a, l
    ldh [nes_sp], a

    ldh a, [nes_p]
    or $04
    and $EF
    or $20
    ldh [nes_p], a

    ld a, $01
    ret

.no_nmi:
    xor a
    ret

; Start a new nametable staging transaction for the dedupe map: bump the
; generation; on wrap clear the whole map. Clobbers AF/BC (HL preserved).
; Leaves WRAM bank 1 selected.
nes_stage_new_generation:
    ld a, [nes_stage_gen]
    inc a
    ld [nes_stage_gen], a
    ret nz
    inc a
    ld [nes_stage_gen], a
    push hl
    ld a, $06
    ldh [rSVBK], a
    xor a
    ld hl, nes_nametable_stage_seen
    ld b, $800 / 16
.clear:
REPT 16
    ld [hli], a
ENDR
    dec b
    jr nz, .clear
    ld a, $01
    ldh [rSVBK], a
    pop hl
    ret

nes_unimplemented_operand_read:
    xor a
    ret

nes_unimplemented_operand_write:
    ret

IF !DEF(NES2GBC_NO_PACING)
; Live video state produced by translated NMI code and consumed by the VBlank
; commit. Swapped with nes_pace_snap around a paced publication.
MACRO PACE_SWAP_VAR ; var, snapshot index
    ld a, [\1]
    ld b, a
    ld a, [nes_pace_snap + \2]
    ld [\1], a
    ld a, b
    ld [nes_pace_snap + \2], a
ENDM

MACRO PACE_COPY_VAR ; var, snapshot index
    ld a, [\1]
    ld [nes_pace_snap + \2], a
ENDM

; DE-walking forms (DE = nes_pace_snap + index; the snapshot does not cross a
; page, so inc e). kind H = HRAM variable (ldh), W = WRAM variable.
MACRO PACE_SWAP_DE ; var, snapshot index, kind
IF STRCMP("\3", "H") == 0
    ldh a, [\1]
    ld b, a
    ld a, [de]
    ldh [\1], a
    ld a, b
ELSE
    ld hl, \1
    ld b, [hl]
    ld a, [de]
    ld [hl], a
    ld a, b
ENDC
    ld [de], a
    inc e
ENDM

MACRO PACE_COPY_DE ; var, snapshot index, kind
IF STRCMP("\3", "H") == 0
    ldh a, [\1]
ELSE
    ld a, [\1]
ENDC
    ld [de], a
    inc e
ENDM

; Snapshot -> live only (DE walk); the post-publication snapshot is dead.
MACRO PACE_LOAD_DE ; var, snapshot index, kind
    ld a, [de]
IF STRCMP("\3", "H") == 0
    ldh [\1], a
ELSE
    ld [\1], a
ENDC
    inc e
ENDM

DEF PACE_IDX_OAM_DIRTY EQU 4
DEF PACE_IDX_MASK_DIRTY EQU 8
DEF PACE_IDX_PALETTE_DIRTY EQU 16
DEF PACE_IDX_SCROLL_DIRTY EQU 17
DEF PACE_IDX_CTRL_DIRTY EQU 18
DEF PACE_IDX_REBUILD_DIRTY EQU 11
DEF PACE_IDX_OAM_EMIT EQU 14
DEF PACE_IDX_OAM_READY EQU 15

; Only state the VBlank commit reads or writes is snapshotted/swapped.
; NMI-side bookkeeping that no ISR path touches (nes_nametable_stage_used,
; nes_generic_hidden_change_count, nes_scroll_pair_count and
; nes_split_pending_x/y/ctrl, used only by the $2005/$2006/$2007/PPUCTRL
; handlers and NMI start) stays live: exchanging it twice per paced
; publication was a no-op.
MACRO PACE_FOR_VARS ; op macro
    \1 nes_ppuctrl, 0, W
    \1 nes_ppumask, 1, W
    \1 nes_ppu_scroll_x, 2, W
    \1 nes_ppu_scroll_y, 3, W
    \1 nes_oam_dirty, 4, W
    \1 nes_nametable_queue_ptr_lo, 5, W
    \1 nes_nametable_queue_ptr_hi, 6, W
    \1 nes_nametable_queue_overflow, 7, W
    \1 nes_mask_dirty, 8, W
    \1 nes_split_duplicate_streak, 9, W
    \1 nes_split_retire_grace_used, 10, W
    \1 nes_generic_map_rebuild_dirty, 11, W
    \1 nes_view_x, 12, H
    \1 nes_view_y, 13, H
    \1 nes_oam_emit_count, 14, H
    \1 nes_oam_shadow_ready, 15, H
    \1 nes_palette_dirty, 16, H
    \1 nes_scroll_dirty, 17, H
    \1 nes_ctrl_dirty, 18, H
    \1 nes_split_active, 19, H
    \1 nes_split_top_x, 20, H
    \1 nes_split_top_y, 21, H
    \1 nes_split_bottom_x, 22, H
    \1 nes_split_bottom_y, 23, H
    \1 nes_split_line, 24, H
    \1 nes_split_top_ctrl, 25, H
    \1 nes_split_bottom_ctrl, 26, H
ENDM

ASSERT 27 <= $30 ; PACE_FOR_VARS entries fit nes_pace_snap
ASSERT HIGH(nes_pace_snap) == HIGH(nes_pace_snap + 26) ; DE walk uses inc e

; Copy the just-completed frame's publishable state into the pacing snapshot.
; Clobbers AF/BC/DE/HL.
nes_pace_take_snapshot:
IF DEF(NES2GBC_CATCHUP)
    ; Catch-up can complete a second frame before the VBlank that publishes
    ; the first: the newer frame supersedes the snapshot (that older frame is
    ; never shown on its own). Its queued entries stay in the queue; merge
    ; its pending publications so nothing it changed is lost.
    ld a, [nes_pace_snap_valid]
    and a
    jr z, .merge_done
    ld hl, nes_pace_snap + PACE_IDX_MASK_DIRTY
    ld a, [nes_mask_dirty]
    or [hl]
    ld [nes_mask_dirty], a
    ld hl, nes_pace_snap + PACE_IDX_SCROLL_DIRTY
    ldh a, [nes_scroll_dirty]
    or [hl]
    ldh [nes_scroll_dirty], a
    ld hl, nes_pace_snap + PACE_IDX_CTRL_DIRTY
    ldh a, [nes_ctrl_dirty]
    or [hl]
    ldh [nes_ctrl_dirty], a
    ld hl, nes_pace_snap + PACE_IDX_REBUILD_DIRTY
    ld a, [nes_generic_map_rebuild_dirty]
    or [hl]
    ld [nes_generic_map_rebuild_dirty], a
    ; Palette: the snapshot copy equals the live shadow unless this frame
    ; changed it (then palette_dirty is set and it is copied again).
    ldh a, [nes_palette_dirty]
    and a
    jr nz, .merge_palette_done
    ld a, [nes_pace_snap_palette]
    ldh [nes_palette_dirty], a
.merge_palette_done:
    ; OAM: if this frame did no DMA, keep the older projected pace page.
    ld a, [nes_oam_dirty]
    and a
    jr nz, .merge_done
    ld a, [nes_pace_snap + PACE_IDX_OAM_DIRTY]
    and a
    jr z, .merge_done
    call .merge_keep_oam
    ret
.merge_done:
ENDC
    ; Project OAM now if needed, exactly as the commit would have.
    ld a, [nes_oam_dirty]
    and a
    jr z, .oam_ok
    ldh a, [nes_oam_shadow_ready]
    and a
    jr nz, .oam_ok
    call nes_video_build_oam_shadow
.oam_ok:
    ; Only a dirty snapshot is ever DMA'd (paced publish or inherited flag).
    ; Hand it the live projected page instead of copying 160 bytes.
    ld a, [nes_oam_dirty]
    and a
    jr z, .oam_snap_done
    call nes_oam_resolve_stale
    ld a, [nes_oam_live_page]
    ld [nes_oam_pace_page], a
    xor HIGH(nes_gbc_oam_shadow) ^ HIGH(nes_pace_oam)
    ld [nes_oam_live_page], a
    ; Non-paced DMA source follows the live page (a paced publish overrides
    ; it with the pace page and restores the live page afterwards).
    ld [nes_oam_dma_page], a
    ld a, $01
    ld [nes_oam_live_stale], a
.oam_snap_done:

    ldh a, [nes_palette_dirty]
    ld [nes_pace_snap_palette], a
    and a
    jr z, .palette_done
    ld hl, nes_gbc_palette_shadow
    ld de, nes_pace_palette
    ld b, 4
.copy_palette:
    REPT 16
    ld a, [hli]
    ld [de], a
    inc e
    ENDR
    dec b
    jr nz, .copy_palette
.palette_done:

    ld de, nes_pace_snap
    PACE_FOR_VARS PACE_COPY_DE
    ; The snapshot now owns this frame's pending publications; the running
    ; NMI starts with nothing dirty, as after an ordinary commit.
    xor a
    ld [nes_oam_dirty], a
    ld [nes_mask_dirty], a
    ldh [nes_palette_dirty], a
    ldh [nes_scroll_dirty], a
    ldh [nes_ctrl_dirty], a
    ld a, $01
    ld [nes_pace_snap_valid], a
    xor a
    ld [nes_pace_unpublished], a
    ldh [nes_scroll_pair_count], a
    ret

IF DEF(NES2GBC_CATCHUP)
; take_snapshot body for a superseding frame without its own OAM DMA: keep
; the older snapshot's OAM page, emit count and ready flag.
.merge_keep_oam:
    ld a, [nes_pace_snap + PACE_IDX_OAM_EMIT]
    push af
    ld a, [nes_pace_snap + PACE_IDX_OAM_READY]
    push af
    call .oam_snap_done
    pop af
    ld [nes_pace_snap + PACE_IDX_OAM_READY], a
    pop af
    ld [nes_pace_snap + PACE_IDX_OAM_EMIT], a
    ld a, $01
    ld [nes_pace_snap + PACE_IDX_OAM_DIRTY], a
    ret
ENDC

; After a paced publication flushed the snapshot's queue prefix
; [$D800, nes_pace_q_end), drop that prefix from the live queue (the running
; NMI appended after it) and start a new dedupe generation so later writes to
; those addresses are queued again (can only cause harmless duplicate
; entries). Runs in the ISR; WRAM bank 1 is current.
nes_pace_retire_flushed_queue:
    ld a, [nes_pace_q_end_lo]
    ld e, a
    ld a, [nes_pace_q_end_hi]
    ld d, a
    ; Empty prefix?
    cp HIGH(nes_nametable_queue)
    jr nz, .clear_bits
    ld a, e
    and a
    ret z
.clear_bits:
    ; Forget every staged mark (new generation). Live entries the running NMI
    ; already queued may get a harmless duplicate entry if written again.
    push de
    call nes_stage_new_generation
    pop de
.bits_done:
    ; Move live entries [prefix_end, ptr) down to the queue start.
    ld hl, nes_nametable_queue
    ld a, [nes_nametable_queue_ptr_lo]
    ld c, a
    ld a, [nes_nametable_queue_ptr_hi]
    ld b, a
.move_loop:
    ld a, e
    cp c
    jr nz, .move_one
    ld a, d
    cp b
    jr z, .move_done
.move_one:
    ld a, [de]
    inc de
    ld [hli], a
    jr .move_loop
.move_done:
    ld a, l
    ld [nes_nametable_queue_ptr_lo], a
    ld a, h
    ld [nes_nametable_queue_ptr_hi], a
    ret

; A stale live OAM page's true contents are the pace page (nobody writes the
; pace page but projectors that owned it as live): point live back at it.
; Keeps the non-paced DMA source in sync. Clobbers A.
nes_oam_resolve_stale:
    ld a, [nes_oam_live_stale]
    and a
    ret z
    xor a
    ld [nes_oam_live_stale], a
    ld a, [nes_oam_pace_page]
    ld [nes_oam_live_page], a
    ld [nes_oam_dma_page], a
    ret

ASSERT LOW(nes_gbc_oam_shadow) == 0 && LOW(nes_pace_oam) == 0

; Exchange live state with the snapshot (used on ISR entry and exit).
; Clobbers AF/BC/DE/HL.
nes_pace_swap:
    ld de, nes_pace_snap
    PACE_FOR_VARS PACE_SWAP_DE
    ; The palette is not swapped: nes_video_sync_palette_shadow reads the
    ; snapshot copy (nes_pace_palette) directly during a paced publication.
    ret

; Swap-back after a COMMITTED paced publication: the snapshot is retired
; (snap_valid cleared right after), so only the running NMI's live state needs
; restoring; copy it back instead of exchanging. Clobbers AF/BC/DE/HL.
nes_pace_restore_live:
    ld de, nes_pace_snap
    PACE_FOR_VARS PACE_LOAD_DE
    ret
ENDC

SECTION "NES bit reverse", ROM0, ALIGN[8]
nes_bit_reverse:
    db $00, $80, $40, $C0, $20, $A0, $60, $E0, $10, $90, $50, $D0, $30, $B0, $70, $F0
    db $08, $88, $48, $C8, $28, $A8, $68, $E8, $18, $98, $58, $D8, $38, $B8, $78, $F8
    db $04, $84, $44, $C4, $24, $A4, $64, $E4, $14, $94, $54, $D4, $34, $B4, $74, $F4
    db $0C, $8C, $4C, $CC, $2C, $AC, $6C, $EC, $1C, $9C, $5C, $DC, $3C, $BC, $7C, $FC
    db $02, $82, $42, $C2, $22, $A2, $62, $E2, $12, $92, $52, $D2, $32, $B2, $72, $F2
    db $0A, $8A, $4A, $CA, $2A, $AA, $6A, $EA, $1A, $9A, $5A, $DA, $3A, $BA, $7A, $FA
    db $06, $86, $46, $C6, $26, $A6, $66, $E6, $16, $96, $56, $D6, $36, $B6, $76, $F6
    db $0E, $8E, $4E, $CE, $2E, $AE, $6E, $EE, $1E, $9E, $5E, $DE, $3E, $BE, $7E, $FE
    db $01, $81, $41, $C1, $21, $A1, $61, $E1, $11, $91, $51, $D1, $31, $B1, $71, $F1
    db $09, $89, $49, $C9, $29, $A9, $69, $E9, $19, $99, $59, $D9, $39, $B9, $79, $F9
    db $05, $85, $45, $C5, $25, $A5, $65, $E5, $15, $95, $55, $D5, $35, $B5, $75, $F5
    db $0D, $8D, $4D, $CD, $2D, $AD, $6D, $ED, $1D, $9D, $5D, $DD, $3D, $BD, $7D, $FD
    db $03, $83, $43, $C3, $23, $A3, $63, $E3, $13, $93, $53, $D3, $33, $B3, $73, $F3
    db $0B, $8B, $4B, $CB, $2B, $AB, $6B, $EB, $1B, $9B, $5B, $DB, $3B, $BB, $7B, $FB
    db $07, $87, $47, $C7, $27, $A7, $67, $E7, $17, $97, $57, $D7, $37, $B7, $77, $F7
    db $0F, $8F, $4F, $CF, $2F, $AF, $6F, $EF, $1F, $9F, $5F, $DF, $3F, $BF, $7F, $FF
