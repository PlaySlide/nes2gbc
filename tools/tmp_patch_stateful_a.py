from pathlib import Path

p = Path('src/state_superblock.rs')
s = p.read_text()

s = s.replace(
'''struct TraceState {\n    x_b: bool,''',
'''struct TraceState {\n    a_live: bool,\n    a_dirty: bool,\n    x_b: bool,''',
1,
)

s = s.replace(
'''struct StateStats {\n    x_seed_loads: usize,''',
'''struct StateStats {\n    a_seed_loads: usize,\n    a_reload_avoided: usize,\n    a_stores_deferred: usize,\n    a_materialized: usize,\n    x_seed_loads: usize,''',
1,
)

marker = '''fn ensure_x(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {'''
helper = r'''fn ensure_a(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    if state.a_live {
        stats.a_reload_avoided += 1;
        return;
    }
    writeln!(out, "    ldh a, [nes_a] ; superblock A cache seed").unwrap();
    state.a_live = true;
    state.a_dirty = false;
    stats.a_seed_loads += 1;
}

fn write_a_resident(state: &mut TraceState, stats: &mut StateStats) {
    state.a_live = true;
    state.a_dirty = true;
    stats.a_stores_deferred += 1;
}

fn discard_a(state: &mut TraceState) {
    state.a_live = false;
    state.a_dirty = false;
}

fn materialize_a(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    if !state.a_dirty {
        return;
    }
    debug_assert!(state.a_live);
    writeln!(out, "    ldh [nes_a], a ; superblock materialize A").unwrap();
    state.a_dirty = false;
    stats.a_materialized += 1;
}

fn clobber_a(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    materialize_a(out, state, stats);
    state.a_live = false;
}

'''
assert marker in s
s = s.replace(marker, helper + marker, 1)

old = r'''fn sync_xy(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    if state.x_dirty {'''
new = r'''fn sync_xy(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    if state.x_dirty {'''
assert old in s
# Leave sync_xy itself intact; add full-state wrapper after it.

marker = '''fn invalidate_xy(state: &mut TraceState) {'''
helper = r'''fn sync_state(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    // A must be published before X/Y because their materialization uses host A.
    materialize_a(out, state, stats);
    sync_xy(out, state, stats);
    // Even a clean resident A cannot be trusted after X/Y publication.
    state.a_live = false;
}

'''
assert marker in s
s = s.replace(marker, helper + marker, 1)

s = s.replace(
'''fn invalidate_xy(state: &mut TraceState) {\n    debug_assert!(!state.x_dirty && !state.y_dirty);\n    state.x_b = false;\n    state.y_c = false;\n}''',
'''fn invalidate_state(state: &mut TraceState) {\n    debug_assert!(!state.a_dirty && !state.x_dirty && !state.y_dirty);\n    state.a_live = false;\n    state.x_b = false;\n    state.y_c = false;\n}''',
1,
)

s = s.replace(
'''        Register::A => writeln!(out, "    ldh a, [nes_a]").unwrap(),''',
'''        Register::A => ensure_a(out, state, stats),''',
1,
)

s = s.replace(
'''        Register::A => writeln!(out, "    ldh [nes_a], a").unwrap(),''',
'''        Register::A => write_a_resident(state, stats),''',
1,
)

# Add a conservative prelude before fast-op emission. Any op that uses host A
# as scratch while architectural A must survive first canonicalizes it. Ops
# that overwrite architectural A may discard the old dirty value without a store.
needle = '''fn emit_fast_op(out: &mut String, op: &IrOp, state: &mut TraceState, stats: &mut StateStats) {\n    debug_assert!(fast_op_supported(op));\n    match *op {'''
replacement = r'''fn emit_fast_op(out: &mut String, op: &IrOp, state: &mut TraceState, stats: &mut StateStats) {
    debug_assert!(fast_op_supported(op));

    match *op {
        IrOp::SetFlag { .. }
        | IrOp::Load { dst: Register::X | Register::Y, .. }
        | IrOp::Store { src: Register::X | Register::Y, .. }
        | IrOp::Transfer { src: Register::X | Register::Y, dst: Register::X | Register::Y, .. }
        | IrOp::Inc(Register::X | Register::Y)
        | IrOp::Dec(Register::X | Register::Y)
        | IrOp::Modify { target: ModifyTarget::Memory(_), .. }
        | IrOp::Compare { .. } => clobber_a(out, state, stats),
        IrOp::Load { dst: Register::A, .. }
        | IrOp::Transfer { dst: Register::A, .. } => discard_a(state),
        _ => {}
    }

    match *op {'''
assert needle in s
s = s.replace(needle, replacement, 1)

# A INC/DEC: consume resident A and defer canonical publication.
s = s.replace(
'''                Register::A => {\n                    writeln!(out, "    ldh a, [nes_a]").unwrap();\n                    writeln!(out, "    {} a", if inc { "inc" } else { "dec" }).unwrap();\n                    writeln!(out, "    ldh [nes_a], a").unwrap();\n                }''',
'''                Register::A => {\n                    ensure_a(out, state, stats);\n                    writeln!(out, "    {} a", if inc { "inc" } else { "dec" }).unwrap();\n                    write_a_resident(state, stats);\n                }''',
1,
)

# Immediate logic: resident A in/out.
s = s.replace(
'''        } => {\n            writeln!(out, "    ldh a, [nes_a]").unwrap();\n            match op {\n                LogicOp::And => writeln!(out, "    and ${imm:02X}").unwrap(),\n                LogicOp::Ora => writeln!(out, "    or ${imm:02X}").unwrap(),\n                LogicOp::Eor => writeln!(out, "    xor ${imm:02X}").unwrap(),\n            }\n            writeln!(out, "    ldh [nes_a], a").unwrap();\n            emit_update_nz(out);\n        }''',
'''        } => {\n            ensure_a(out, state, stats);\n            match op {\n                LogicOp::And => writeln!(out, "    and ${imm:02X}").unwrap(),\n                LogicOp::Ora => writeln!(out, "    or ${imm:02X}").unwrap(),\n                LogicOp::Eor => writeln!(out, "    xor ${imm:02X}").unwrap(),\n            }\n            write_a_resident(state, stats);\n            emit_update_nz(out);\n        }''',
1,
)

# Arithmetic: capture resident lhs before loading rhs, and leave the result resident.
old = r'''        IrOp::Arithmetic { op, rhs } => {
            // Keep B/C reserved for resident X/Y. D/E/H/L are scratch here;
            // any effective-address use is complete before arithmetic begins.
            match rhs {
                Operand::Immediate(imm) => {
                    writeln!(out, "    ld e, ${imm:02X}").unwrap();
                }
                _ => {
                    emit_operand_load(out, rhs, state, stats);
                    writeln!(out, "    ld e, a ; superblock arithmetic RHS").unwrap();
                }
            }
            if op == ArithmeticOp::Sbc {
                // 6502 SBC is A + (~rhs) + C; using complemented E lets the
                // ADC overflow identity below match SBC exactly as well.
                writeln!(out, "    ld a, e").unwrap();
                writeln!(out, "    cpl").unwrap();
                writeln!(out, "    ld e, a").unwrap();
            }

            writeln!(out, "    ldh a, [nes_a]").unwrap();
            writeln!(out, "    ld d, a ; superblock arithmetic lhs").unwrap();'''
new = r'''        IrOp::Arithmetic { op, rhs } => {
            // Capture architectural A before operand/address work can use host A.
            // B/C remain reserved for resident X/Y; D holds the 6502 lhs.
            ensure_a(out, state, stats);
            writeln!(out, "    ld d, a ; superblock resident arithmetic lhs").unwrap();
            match rhs {
                Operand::Immediate(imm) => {
                    writeln!(out, "    ld e, ${imm:02X}").unwrap();
                }
                _ => {
                    emit_operand_load(out, rhs, state, stats);
                    writeln!(out, "    ld e, a ; superblock arithmetic RHS").unwrap();
                }
            }
            if op == ArithmeticOp::Sbc {
                // 6502 SBC is A + (~rhs) + C; using complemented E lets the
                // ADC overflow identity below match SBC exactly as well.
                writeln!(out, "    ld a, e").unwrap();
                writeln!(out, "    cpl").unwrap();
                writeln!(out, "    ld e, a").unwrap();
            }

            writeln!(out, "    ld a, d").unwrap();'''
assert old in s
s = s.replace(old, new, 1)

s = s.replace(
'''            writeln!(out, "    ld a, l").unwrap();\n            writeln!(out, "    ldh [nes_a], a").unwrap();\n            emit_update_nz(out);\n            stats.fast_arithmetic += 1;''',
'''            writeln!(out, "    ld a, l").unwrap();\n            write_a_resident(state, stats);\n            emit_update_nz(out);\n            stats.fast_arithmetic += 1;''',
1,
)

# Accumulator shifts/RMW stay resident.
s = s.replace(
'''            ModifyTarget::Accumulator => {\n                writeln!(out, "    ldh a, [nes_a]").unwrap();\n                emit_fast_modify_value(out, modify, stats);\n                writeln!(out, "    ldh [nes_a], a").unwrap();\n            }''',
'''            ModifyTarget::Accumulator => {\n                ensure_a(out, state, stats);\n                emit_fast_modify_value(out, modify, stats);\n                write_a_resident(state, stats);\n            }''',
1,
)

# Barriers and side exits must publish A before legacy code/control can observe it.
s = s.replace(
'''    sync_xy(out, state, stats);\n    invalidate_xy(state);\n    out.push_str(&lr35902::emit_ops(ops));''',
'''    sync_state(out, state, stats);\n    invalidate_state(state);\n    out.push_str(&lr35902::emit_ops(ops));''',
1,
)

# All remaining explicit side-exit/cold-path syncs become full state syncs.
s = s.replace('sync_xy(&mut out, &mut state, &mut stats);', 'sync_state(&mut out, &mut state, &mut stats);')
s = s.replace('invalidate_xy(&mut state);', 'invalidate_state(&mut state);')

# Update header comment.
s = s.replace(
'''    writeln!(out, "; X lives in B, Y in C across safe trace edges; canonical HRAM is materialized at barriers").unwrap();''',
'''    writeln!(out, "; A lives in host A, X in B, Y in C across safe trace edges; dirty canonical HRAM is materialized at barriers").unwrap();''',
1,
)

# PROFILE_TRACE instrumentation on a continuing private edge must preserve host A.
old = r'''        writeln!(out, "IF DEF(NES2GBC_PROFILE_TRACE)").unwrap();
        writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
        writeln!(out, "    call nes_profile_trace_pc").unwrap();
        writeln!(out, "ENDC").unwrap();'''
new = r'''        writeln!(out, "IF DEF(NES2GBC_PROFILE_TRACE)").unwrap();
        if continuing && state.a_live {
            writeln!(out, "    push af ; preserve resident A across profile trace").unwrap();
        }
        writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
        writeln!(out, "    call nes_profile_trace_pc").unwrap();
        if continuing && state.a_live {
            writeln!(out, "    pop af").unwrap();
        }
        writeln!(out, "ENDC").unwrap();'''
assert old in s
s = s.replace(old, new, 1)

# Debug trace also uses host A; preserve resident A on a continuing edge.
old = r'''        if options.debug_trace {
            let before = out.len();
            writeln!(out, "    ld a, ${:02X}", (block.start >> 8) as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_hi], a").unwrap();
            writeln!(out, "    ld a, ${:02X}", block.start as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_lo], a").unwrap();
            section_pc += approx_code_bytes(&out[before..]);
        }'''
new = r'''        if options.debug_trace {
            let before = out.len();
            if continuing && state.a_live {
                writeln!(out, "    push af ; preserve resident A across debug trace").unwrap();
            }
            writeln!(out, "    ld a, ${:02X}", (block.start >> 8) as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_hi], a").unwrap();
            writeln!(out, "    ld a, ${:02X}", block.start as u8).unwrap();
            writeln!(out, "    ld [nes_debug_pc_lo], a").unwrap();
            if continuing && state.a_live {
                writeln!(out, "    pop af").unwrap();
            }
            section_pc += approx_code_bytes(&out[before..]);
        }'''
assert old in s
s = s.replace(old, new, 1)

# Canonical adapters seed X/Y first and A last, so host A satisfies the contract.
old = r'''        if contract.y_c {
            writeln!(out, "    ldh a, [nes_y]").unwrap();
            writeln!(out, "    ld c, a ; canonical adapter Y").unwrap();
        }
        writeln!(out, "    jp nes_{addr:04X}_trace").unwrap();'''
new = r'''        if contract.y_c {
            writeln!(out, "    ldh a, [nes_y]").unwrap();
            writeln!(out, "    ld c, a ; canonical adapter Y").unwrap();
        }
        if contract.a_live {
            writeln!(out, "    ldh a, [nes_a] ; canonical adapter A").unwrap();
        }
        writeln!(out, "    jp nes_{addr:04X}_trace").unwrap();'''
assert old in s
s = s.replace(old, new, 1)

# Diagnostic now exposes the actual A-residency traffic.
old = '''        "superblock-state: avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); barriers {}; canonical adapters {}",'''
new = '''        "superblock-state: A avoided {} reload(s), deferred {} store(s), materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); barriers {}; canonical adapters {}",'''
assert old in s
s = s.replace(old, new, 1)
old = '''        stats.x_reload_avoided,\n        stats.y_reload_avoided,'''
new = '''        stats.a_reload_avoided,\n        stats.a_stores_deferred,\n        stats.a_materialized,\n        stats.a_seed_loads,\n        stats.x_reload_avoided,\n        stats.y_reload_avoided,'''
assert old in s
s = s.replace(old, new, 1)

# Add focused regression: A remains dirty through LDA/STA/AND, crosses a private
# JMP edge, and is only canonicalized at the real RTS barrier. External entry
# to the private target seeds host A from canonical HRAM.
insert = r'''
    #[test]
    fn a_stays_dirty_across_store_logic_and_private_trace_edge() {
        let mut prg = vec![0xEA; 0x8000];
        // LDA #$3F / STA $00 / AND #$0F / JMP $8010 ; target STA $01 / RTS.
        prg[0..9].copy_from_slice(&[0xA9, 0x3F, 0x85, 0x00, 0x29, 0x0F, 0x4C, 0x10, 0x80]);
        prg[0x10..0x13].copy_from_slice(&[0x85, 0x01, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000]).unwrap();
        let asm = emit_cfg_with_interrupts(
            &graph,
            EmitOptions {
                reset: 0x8000,
                max_blocks: Some(8),
                debug_trace: false,
            },
            0x8000,
            0x8000,
        );
        let lda = asm.find("; $8000: $A9 Lda Immediate").unwrap();
        let target = asm.find("nes_8010_trace:").unwrap();
        let rts = asm.find("; $8012: $60 Rts Implied").unwrap();
        let hot = &asm[lda..rts];
        assert!(hot.contains("superblock: same-bank unique-entry JMP elided"));
        assert!(!hot.contains("superblock materialize A"));
        assert!(!hot.contains("ldh a, [nes_a]"));
        assert!(asm[target..rts].contains("ld [$C001], a"));
        let after_rts = &asm[rts..];
        assert!(after_rts.contains("superblock materialize A"));
        assert!(asm.contains("ldh a, [nes_a] ; canonical adapter A"));
    }
'''
pos = s.rfind('\n}\n')
assert pos != -1
s = s[:pos] + insert + s[pos:]

p.write_text(s)
