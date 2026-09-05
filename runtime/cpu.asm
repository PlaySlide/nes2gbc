; Canonical NES CPU state and helper routines.
; Correctness-first implementation. Later passes may cache A/X/Y/P in host registers.

DEF NES_RAM_BASE EQU $C000
DEF rLY EQU $FF44

SECTION "NES CPU state", WRAM0[$C800]
nes_a:  ds 1
nes_x:  ds 1
nes_y:  ds 1
nes_sp: ds 1
nes_p:  ds 1

SECTION "NES CPU helpers", ROM0

; Input: A = result value. Output: A preserved, Z/N bits in nes_p updated.
nes_set_nz_from_a:
    ld e, a
    ld a, [nes_p]
    and $7D
    ld d, a
    ld a, e
    and a
    jr nz, .not_zero
    ld a, d
    or $02
    ld d, a
.not_zero:
    bit 7, e
    jr z, .not_negative
    ld a, d
    or $80
    ld d, a
.not_negative:
    ld a, d
    ld [nes_p], a
    ld a, e
    ret

; Compare canonical accumulator-like value in A against E.
; Updates C/Z/N exactly as 6502 CMP-family subtraction.
nes_compare_a_e:
    ld d, a
    sub e
    ld c, a

    ld a, [nes_p]
    and $7C
    ld b, a

    ; 6502 carry means no borrow, i.e. original >= rhs.
    ld a, d
    cp e
    jr c, .carry_done
    ld a, b
    or $01
    ld b, a
.carry_done:

    ld a, c
    and a
    jr nz, .zero_done
    ld a, b
    or $02
    ld b, a
.zero_done:

    bit 7, c
    jr z, .negative_done
    ld a, b
    or $80
    ld b, a
.negative_done:
    ld a, b
    ld [nes_p], a
    ret

; Virtual 6502 stack lives in mirrored RAM page $0100 at $C100.
; Push stores then decrements SP.
nes_stack_push_a:
    ld e, a
    ld a, [nes_sp]
    ld l, a
    ld h, $C1
    ld a, e
    ld [hl], a
    ld a, [nes_sp]
    dec a
    ld [nes_sp], a
    ret

; Pop increments SP then reads.
nes_stack_pop_a:
    ld a, [nes_sp]
    inc a
    ld [nes_sp], a
    ld l, a
    ld h, $C1
    ld a, [hl]
    ret

; 6502 JSR pushes high byte then low byte of PC-1/return address.
; Input: HL = 6502 return address (address of last JSR operand byte).
nes_stack_push_return_hl:
    ld a, h
    call nes_stack_push_a
    ld a, l
    call nes_stack_push_a
    ret

; Output: HL = stacked 6502 return address. RTS increments it before dispatch.
nes_stack_pop_return_hl:
    call nes_stack_pop_a
    ld l, a
    call nes_stack_pop_a
    ld h, a
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

; Generic CPU read. Input HL = NES CPU address, output A = value.
nes_cpu_read:
    ld a, h
    cp $20
    jr c, .ram
    cp $40
    jr c, .ppu
    cp $80
    jr nc, .prg

    ; Minimal APU / controller register reads for now.
    cp $41
    jr nz, .unsupported
    ld a, l
    cp $11
    jr z, .read_4011
    jr .unsupported

.ram:
    call nes_map_cpu_addr_hl
    ld a, [hl]
    ret

.ppu:
    ld a, l
    and $07
    ld l, a
    jp nes_ppu_cpu_read

.prg:
    ; Preserve NES address, select the ROMX bank, then map offset to $4000-$7FFF.
    ld d, h
    ld e, l

    ld a, [nes_prg_16k_mirror]
    and a
    jr nz, .prg_bank_1

    ld a, d
    cp $C0
    ld a, $01
    jr c, .prg_select
    ld a, $02
    jr .prg_select

.prg_bank_1:
    ld a, $01

.prg_select:
    ld [$2000], a
    ld a, d
    and $3F
    or $40
    ld h, a
    ld l, e
    ld a, [hl]
    ret

.read_4011:
    ld a, [nes_dac]
    ret

.unsupported:
    xor a
    ret

; Generic CPU write. Input HL = NES CPU address, A = value.
nes_cpu_write:
    ld e, a
    ld a, h
    cp $20
    jr c, .ram
    cp $40
    jr c, .ppu
    cp $80
    jr nc, .mapper

    cp $41
    jr nz, .unsupported
    ld a, l
    cp $11
    jr z, .write_4011
    jr .unsupported

.ram:
    call nes_map_cpu_addr_hl
    ld a, e
    ld [hl], a
    ret

.ppu:
    ld a, l
    and $07
    ld l, a
    jp nes_ppu_cpu_write

.mapper:
    ; CNROM writes anywhere in $8000-$FFFF select the 8 KiB CHR bank.
    ld a, [nes_mapper]
    cp $03
    ret nz
    ld a, [nes_chr_bank_mask]
    and e
    ld [nes_chr_bank], a
    ret

.write_4011:
    ld a, e
    ld [nes_dac], a
    ret

.unsupported:
    ret


; Input: A = lhs, E = rhs. Uses 6502 carry-in from nes_p.
; Output: A = result, updates C/V/N/Z in nes_p.
nes_adc_a_e:
    ld d, a
    ld a, [nes_p]
    and $01
    jr z, .adc_clear_c
    scf
    jr .adc_go
.adc_clear_c:
    and a
.adc_go:
    ld a, d
    adc e
    ld c, a

    ld a, [nes_p]
    and $3C
    ld b, a
    jr nc, .adc_no_carry
    ld a, b
    or $01
    ld b, a
.adc_no_carry:

    ld a, c
    and a
    jr nz, .adc_not_zero
    ld a, b
    or $02
    ld b, a
.adc_not_zero:
    bit 7, c
    jr z, .adc_not_negative
    ld a, b
    or $80
    ld b, a
.adc_not_negative:

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
    ld [nes_p], a
    ld a, c
    ret

; SBC on the 6502 is A + (~rhs) + C.
nes_sbc_a_e:
    ld a, e
    cpl
    ld e, a
    jp nes_adc_a_e

nes_unimplemented_operand_read:
    xor a
    ret

nes_unimplemented_operand_write:
    ret
