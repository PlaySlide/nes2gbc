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

    ; Tight NES loops repeatedly branch to the same translated PC. Avoid a full
    ; dispatch-table bank switch/lookup when the requested PC matches the most
    ; recently resolved target.
    ld a, [nes_dispatch_cache_valid]
    and a
    jr z, .cache_miss
    ld a, [nes_dispatch_cache_pc_hi]
    cp h
    jr nz, .cache_miss
    ld a, [nes_dispatch_cache_pc_lo]
    cp l
    jr nz, .cache_miss

    ld a, [nes_dispatch_cache_bank]
    ld b, a
    ld a, [nes_current_code_bank]
    cp b
    jr z, .cache_bank_ready
    ld a, b
    ld [nes_current_code_bank], a
    ld [$2000], a
    xor a
    ld [$3000], a
.cache_bank_ready:
    ld a, [nes_dispatch_cache_addr_hi]
    ld h, a
    ld a, [nes_dispatch_cache_addr_lo]
    ld l, a
    jp hl

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

    ld a, b
    ld [nes_current_code_bank], a
    ld [$2000], a
    xor a
    ld [$3000], a

    ld h, d
    ld l, e
    jp hl

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
IF DEF(NES2GBC_DEBUG_TRACE)
    ld a, $FF
    ld [nes_debug_fault], a
ENDC
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

    ; Minimal APU / controller register reads for now.
    cp $40
    jp nz, .unsupported
    ld a, l
    cp $11
    jp z, .read_4011
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

    ; Minimal APU / controller register reads for now.
    cp $40
    jr nz, .unsupported
    ld a, l
    cp $11
    jr z, .read_4011
    cp $16
    jr z, .read_4016
    cp $17
    jr z, .read_4017
    jr .unsupported
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

.prg:
    PROFILE_INC nes_profile_read_prg
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

.prg_banked_rom:
    ; Preserve NES address, select the ROMX bank, then map offset to $4000-$7FFF.
    ld d, h
    ld e, l

    ld a, d
    cp $C0
    ld a, $01
    jr c, .prg_select
    ld a, $02

.prg_select:
    ld [$2000], a
    ld a, d
    and $3F
    or $40
    ld h, a
    ld l, e
    ld a, [hl]
IF DEF(NES2GBC_DEBUG_TRACE)
    ld [nes_debug_bus_value], a
ENDC
    push af
    call nes_restore_code_bank
    pop af
    ret

.read_4011:
    PROFILE_INC nes_profile_read_io
    ld a, [nes_dac]
    ret

.read_4016:
    PROFILE_INC nes_profile_read_io
    jp nes_controller_read

.read_4017:
    PROFILE_INC nes_profile_read_io
    ; No second controller yet.
    ld a, $01
    ret

.unsupported:
    PROFILE_INC nes_profile_read_other
    xor a
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

    cp $40
    jp nz, .unsupported
    ld a, l
    cp $11
    jp z, .write_4011
    cp $14
    jp z, .write_4014
    cp $16
    jp z, .write_4016
    jp .unsupported
ELSE
    cp $20
    jr c, .ram
    cp $40
    jr c, .ppu
    cp $80
    jr nc, .mapper

    cp $40
    jr nz, .unsupported
    ld a, l
    cp $11
    jr z, .write_4011
    cp $14
    jr z, .write_4014
    cp $16
    jr z, .write_4016
    jr .unsupported
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

.mapper:
    PROFILE_INC nes_profile_write_mapper
    ; CNROM writes anywhere in $8000-$FFFF select the 8 KiB CHR bank.
    ld a, [nes_mapper]
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

.write_4011:
    PROFILE_INC nes_profile_write_io
    ld a, e
    ld [nes_dac], a
    ret

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
    ld h, a
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
    ld a, [nes_host_vblank_pending]
    and a
    ret z

    ; Consume the host event. If NES NMI is disabled or already active, this
    ; frame is intentionally dropped instead of creating back-to-back NMIs.
    xor a
    ld [nes_host_vblank_pending], a

    ld a, [nes_ppuctrl]
    bit 7, a
    jr z, .no_nmi

    ld a, [nes_nmi_active]
    and a
    jr nz, .no_nmi

    ld a, $01
    ld [nes_nmi_active], a
    PROFILE_INC nes_profile_nmi

    ; Hardware interrupt stack frame: PC high, PC low, P with B clear.
    ld b, h
    ld c, l
    ld a, b
    call nes_stack_push_a
    ld a, c
    call nes_stack_push_a

    call nes_materialize_p
    and $EF
    or $20
    call nes_stack_push_a

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

nes_unimplemented_operand_read:
    xor a
    ret

nes_unimplemented_operand_write:
    ret
