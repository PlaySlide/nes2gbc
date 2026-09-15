from pathlib import Path

p = Path('src/state_superblock.rs')
s = p.read_text()

s = s.replace(
'''    fast_arithmetic: usize,\n    fast_shifts: usize,\n    barriers: usize,''',
'''    fast_arithmetic: usize,\n    fast_shifts: usize,\n    fast_rmw_addr_reuse: usize,\n    barriers: usize,''',
1,
)

old = r'''        ModifyOp::Asl | ModifyOp::Lsr | ModifyOp::Rol | ModifyOp::Ror => {
            if matches!(modify, ModifyOp::Rol | ModifyOp::Ror) {
                // RL/RR consume host carry, so seed it from lazy 6502 C.
                // B/C remain untouched because they belong to resident X/Y.
                writeln!(out, "    ld d, a ; superblock rotate input").unwrap();
                writeln!(out, "    ldh a, [nes_c_shadow]").unwrap();
                writeln!(out, "    and a").unwrap();
                writeln!(out, "    jr z, :+").unwrap();
                writeln!(out, "    scf").unwrap();
                writeln!(out, "    jr :++").unwrap();
                writeln!(out, ":").unwrap();
                writeln!(out, "    and a").unwrap();
                writeln!(out, ":").unwrap();
                writeln!(out, "    ld a, d").unwrap();
            }

            let (host_op, name) = match modify {
                ModifyOp::Asl => ("sla", "ASL"),
                ModifyOp::Lsr => ("srl", "LSR"),
                ModifyOp::Rol => ("rl", "ROL"),
                ModifyOp::Ror => ("rr", "ROR"),
                _ => unreachable!(),
            };
            writeln!(out, "    {host_op} a ; superblock fast {name}").unwrap();
            writeln!(out, "    ld e, a ; superblock shift/rotate result").unwrap();

            // Capture the shifted-out bit before any host-flag clobber.
            writeln!(out, "    ld a, $00").unwrap();
            writeln!(out, "    jr nc, :+").unwrap();
            writeln!(out, "    inc a").unwrap();
            writeln!(out, ":").unwrap();
            writeln!(out, "    ldh [nes_c_shadow], a").unwrap();

            writeln!(out, "    ld a, e").unwrap();
            emit_update_nz(out);
            stats.fast_shifts += 1;
        }'''
new = r'''        ModifyOp::Asl | ModifyOp::Lsr | ModifyOp::Rol | ModifyOp::Ror => {
            if matches!(modify, ModifyOp::Rol | ModifyOp::Ror) {
                // nes_c_shadow is normalized to 0/1. RRA copies bit 0 into
                // host carry without branches; LD below preserves that carry.
                // B/C remain untouched because they belong to resident X/Y.
                writeln!(out, "    ld d, a ; superblock rotate input").unwrap();
                writeln!(out, "    ldh a, [nes_c_shadow]").unwrap();
                writeln!(out, "    rra ; seed host carry from 6502 C").unwrap();
                writeln!(out, "    ld a, d").unwrap();
            }

            let name = match modify {
                ModifyOp::Asl => {
                    // ADD A,A is the one-byte/one-M-cycle LR35902 form of ASL.
                    writeln!(out, "    add a ; superblock fast ASL").unwrap();
                    "ASL"
                }
                ModifyOp::Lsr => {
                    writeln!(out, "    srl a ; superblock fast LSR").unwrap();
                    "LSR"
                }
                ModifyOp::Rol => {
                    writeln!(out, "    rl a ; superblock fast ROL").unwrap();
                    "ROL"
                }
                ModifyOp::Ror => {
                    writeln!(out, "    rr a ; superblock fast ROR").unwrap();
                    "ROR"
                }
                _ => unreachable!(),
            };
            let _ = name;
            writeln!(out, "    ld e, a ; superblock shift/rotate result").unwrap();

            // Branchless carry capture: LD preserves carry and RL moves C into
            // bit 0, yielding the canonical normalized 0/1 shadow.
            writeln!(out, "    ld a, $00").unwrap();
            writeln!(out, "    rl a ; capture shift/rotate carry").unwrap();
            writeln!(out, "    ldh [nes_c_shadow], a").unwrap();

            writeln!(out, "    ld a, e").unwrap();
            emit_update_nz(out);
            stats.fast_shifts += 1;
        }'''
assert old in s
s = s.replace(old, new, 1)

marker = 'fn fast_op_supported(op: &IrOp) -> bool {'
helper = r'''fn emit_fast_modify_memory(
    out: &mut String,
    mem: Operand,
    modify: ModifyOp,
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    match mem {
        Operand::ZeroPage(zp) => {
            let addr = NES_RAM_BASE + zp as u16;
            writeln!(out, "    ld a, [${addr:04X}]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [${addr:04X}], a").unwrap();
        }
        Operand::Absolute(addr) => {
            let mapped = direct_ram_addr(addr).unwrap();
            writeln!(out, "    ld a, [${mapped:04X}]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [${mapped:04X}], a").unwrap();
        }
        Operand::ZeroPageX(zp) => {
            ensure_x(out, state, stats);
            writeln!(out, "    ld a, b ; superblock cached X index").unwrap();
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            writeln!(out, "    ld a, [hl]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [hl], a ; reuse indexed RMW address").unwrap();
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::ZeroPageY(zp) => {
            ensure_y(out, state, stats);
            writeln!(out, "    ld a, c ; superblock cached Y index").unwrap();
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            writeln!(out, "    add ${zp:02X}").unwrap();
            writeln!(out, "    ld l, a").unwrap();
            writeln!(out, "    ld h, $C0").unwrap();
            writeln!(out, "    ld a, [hl]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [hl], a ; reuse indexed RMW address").unwrap();
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::AbsoluteX(addr) => {
            ensure_x(out, state, stats);
            writeln!(out, "    ld hl, ${:04X}", NES_RAM_BASE + addr).unwrap();
            writeln!(out, "    ld a, b ; superblock cached X index").unwrap();
            stats.x_reload_avoided += 1;
            stats.x_index_uses += 1;
            emit_add_a_to_hl(out);
            writeln!(out, "    ld a, [hl]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [hl], a ; reuse indexed RMW address").unwrap();
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::AbsoluteY(addr) => {
            ensure_y(out, state, stats);
            writeln!(out, "    ld hl, ${:04X}", NES_RAM_BASE + addr).unwrap();
            writeln!(out, "    ld a, c ; superblock cached Y index").unwrap();
            stats.y_reload_avoided += 1;
            stats.y_index_uses += 1;
            emit_add_a_to_hl(out);
            writeln!(out, "    ld a, [hl]").unwrap();
            emit_fast_modify_value(out, modify, stats);
            writeln!(out, "    ld [hl], a ; reuse indexed RMW address").unwrap();
            stats.fast_rmw_addr_reuse += 1;
        }
        Operand::Immediate(_) | Operand::IndexedIndirect(_) | Operand::IndirectIndexed(_) => {
            unreachable!()
        }
    }
}

'''
assert marker in s
s = s.replace(marker, helper + marker, 1)

old = r'''            ModifyTarget::Memory(mem) => {
                emit_operand_load(out, mem, state, stats);
                emit_fast_modify_value(out, modify, stats);
                emit_operand_store(out, mem, state, stats);
            }'''
new = r'''            ModifyTarget::Memory(mem) => {
                emit_fast_modify_memory(out, mem, modify, state, stats);
            }'''
assert old in s
s = s.replace(old, new, 1)

s = s.replace(
'''fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates); barriers {}''',
'''fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); barriers {}''',
1,
)
s = s.replace(
'''        stats.fast_shifts,\n        stats.barriers,''',
'''        stats.fast_shifts,\n        stats.fast_rmw_addr_reuse,\n        stats.barriers,''',
1,
)

old = r'''        assert!(between.contains("superblock fast ROR"));
        assert!(!between.contains("superblock materialize X"));
        assert!(asm[store_x..].contains("superblock cached X"));'''
new = r'''        assert!(between.contains("superblock fast ROR"));
        assert!(between.contains("add a ; superblock fast ASL"));
        assert!(between.contains("rra ; seed host carry from 6502 C"));
        assert!(between.contains("rl a ; capture shift/rotate carry"));
        assert_eq!(between.matches("superblock cached X index").count(), 4);
        assert_eq!(between.matches("reuse indexed RMW address").count(), 4);
        assert!(!between.contains("push af"));
        assert!(!between.contains("superblock materialize X"));
        assert!(asm[store_x..].contains("superblock cached X"));'''
assert old in s
s = s.replace(old, new, 1)

p.write_text(s)
