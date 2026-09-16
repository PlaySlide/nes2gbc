#!/usr/bin/env python3
from pathlib import Path

path = Path("src/state_superblock.rs")
s = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global s
    n = s.count(old)
    assert n == 1, f"expected one match, found {n}: {old[:120]!r}"
    s = s.replace(old, new, 1)


replace_once(
'''struct TraceState {
    a_live: bool,
    a_dirty: bool,
    x_b: bool,
    x_dirty: bool,
    y_c: bool,
    y_dirty: bool,
}
''',
'''struct TraceState {
    a_live: bool,
    a_dirty: bool,
    x_b: bool,
    x_dirty: bool,
    y_c: bool,
    y_dirty: bool,
    // Conservative phase 1: only defer Z/N while the defining value remains
    // resident in host A, and never carry that contract across a block edge.
    zn_a_pending: bool,
}
''')

replace_once(
'''    fast_rmw_addr_reuse: usize,
    barriers: usize,
    canonical_adapters: usize,
}
''',
'''    fast_rmw_addr_reuse: usize,
    zn_updates_deferred: usize,
    zn_publications_elided: usize,
    zn_materialized: usize,
    barriers: usize,
    canonical_adapters: usize,
}
''')

replace_once(
'''fn emit_update_nz(out: &mut String) {
    writeln!(out, "    ldh [nes_z_shadow], a").unwrap();
    writeln!(out, "    ldh [nes_n_shadow], a").unwrap();
}

fn ensure_a''',
'''fn emit_update_nz(out: &mut String) {
    writeln!(out, "    ldh [nes_z_shadow], a").unwrap();
    writeln!(out, "    ldh [nes_n_shadow], a").unwrap();
}

fn discard_deferred_nz_as_overwritten(state: &mut TraceState, stats: &mut StateStats) {
    if state.zn_a_pending {
        state.zn_a_pending = false;
        stats.zn_publications_elided += 1;
    }
}

fn defer_update_nz_from_a(state: &mut TraceState, stats: &mut StateStats) {
    debug_assert!(state.a_live);
    debug_assert!(!state.zn_a_pending);
    state.zn_a_pending = true;
    stats.zn_updates_deferred += 1;
}

fn materialize_deferred_nz_from_a(
    out: &mut String,
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    if !state.zn_a_pending {
        return;
    }
    debug_assert!(state.a_live);
    writeln!(out, "    ldh [nes_z_shadow], a ; materialize deferred Z from resident A").unwrap();
    writeln!(out, "    ldh [nes_n_shadow], a ; materialize deferred N from resident A").unwrap();
    state.zn_a_pending = false;
    stats.zn_materialized += 1;
}

fn ensure_a''')

replace_once(
'''fn discard_a(state: &mut TraceState) {
    state.a_live = false;
    state.a_dirty = false;
}
''',
'''fn discard_a(state: &mut TraceState) {
    debug_assert!(!state.zn_a_pending);
    state.a_live = false;
    state.a_dirty = false;
}
''')

replace_once(
'''fn clobber_a(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    materialize_a(out, state, stats);
    state.a_live = false;
}
''',
'''fn clobber_a(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    materialize_a(out, state, stats);
    materialize_deferred_nz_from_a(out, state, stats);
    state.a_live = false;
}
''')

replace_once(
'''fn sync_state(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    // A must be published before X/Y because their materialization uses host A.
    materialize_a(out, state, stats);
    sync_xy(out, state, stats);
    // Even a clean resident A cannot be trusted after X/Y publication.
    state.a_live = false;
}

fn invalidate_state(state: &mut TraceState) {
    debug_assert!(!state.a_dirty && !state.x_dirty && !state.y_dirty);
''',
'''fn sync_state(out: &mut String, state: &mut TraceState, stats: &mut StateStats) {
    // A must be published before X/Y because their materialization uses host A.
    materialize_a(out, state, stats);
    materialize_deferred_nz_from_a(out, state, stats);
    sync_xy(out, state, stats);
    // Even a clean resident A cannot be trusted after X/Y publication.
    state.a_live = false;
}

fn invalidate_state(state: &mut TraceState) {
    debug_assert!(!state.a_dirty && !state.x_dirty && !state.y_dirty && !state.zn_a_pending);
''')

replace_once(
'''fn emit_fast_op(out: &mut String, op: &IrOp, state: &mut TraceState, stats: &mut StateStats) {
    debug_assert!(fast_op_supported(op));

    match *op {
''',
'''fn fast_op_overwrites_zn(op: &IrOp) -> bool {
    match *op {
        IrOp::Load { .. }
        | IrOp::Inc(_)
        | IrOp::Dec(_)
        | IrOp::Logic { .. }
        | IrOp::Arithmetic { .. }
        | IrOp::Compare { .. }
        | IrOp::Modify { .. } => true,
        IrOp::Transfer { update_nz, .. } => update_nz,
        _ => false,
    }
}

fn emit_fast_op(out: &mut String, op: &IrOp, state: &mut TraceState, stats: &mut StateStats) {
    debug_assert!(fast_op_supported(op));

    // If this instruction defines both Z and N, any older deferred A-derived
    // Z/N value is dead before it ever needs publication.
    if fast_op_overwrites_zn(op) {
        discard_deferred_nz_as_overwritten(state, stats);
    }

    match *op {
''')

replace_once(
'''        IrOp::Load {
            dst: Register::A, ..
        }
        | IrOp::Transfer {
            dst: Register::A, ..
        } => discard_a(state),
''',
'''        IrOp::Load {
            dst: Register::A, ..
        }
        | IrOp::Transfer {
            dst: Register::A,
            update_nz: true,
            ..
        } => discard_a(state),
        IrOp::Transfer {
            dst: Register::A,
            update_nz: false,
            ..
        } => clobber_a(out, state, stats),
''')

replace_once(
'''        IrOp::Load { dst, src } => {
            emit_operand_load(out, src, state, stats);
            let _ = write_reg_from_a(out, dst, state, stats);
            emit_update_nz(out);
        }
''',
'''        IrOp::Load { dst, src } => {
            emit_operand_load(out, src, state, stats);
            let _ = write_reg_from_a(out, dst, state, stats);
            if dst == Register::A {
                defer_update_nz_from_a(state, stats);
            } else {
                emit_update_nz(out);
            }
        }
''')

replace_once(
'''            if update_nz {
                emit_update_nz(out);
            }
''',
'''            if update_nz {
                if dst == Register::A {
                    defer_update_nz_from_a(state, stats);
                } else {
                    emit_update_nz(out);
                }
            }
''')

replace_once(
'''            emit_update_nz(out);
        }
        IrOp::Logic {
''',
'''            match reg {
                Register::A => defer_update_nz_from_a(state, stats),
                Register::X | Register::Y => emit_update_nz(out),
                Register::Sp => unreachable!(),
            }
        }
        IrOp::Logic {
''')

replace_once(
'''            write_a_resident(state, stats);
            emit_update_nz(out);
        }
        IrOp::Arithmetic { op, rhs } => {
''',
'''            write_a_resident(state, stats);
            defer_update_nz_from_a(state, stats);
        }
        IrOp::Arithmetic { op, rhs } => {
''')

replace_once(
'''            writeln!(out, "    ld a, l").unwrap();
            write_a_resident(state, stats);
            emit_update_nz(out);
            stats.fast_arithmetic += 1;
''',
'''            writeln!(out, "    ld a, l").unwrap();
            write_a_resident(state, stats);
            defer_update_nz_from_a(state, stats);
            stats.fast_arithmetic += 1;
''')

replace_once(
'''        if let Some(target) = plan.next.get(&block.start).copied() {
            debug_assert_eq!(selected_list.get(idx + 1).copied(), Some(target));
            pending_continuation = Some((target, bank));
''',
'''        if let Some(target) = plan.next.get(&block.start).copied() {
            // Phase 1 deliberately keeps lazy Z/N block-local. A continuation
            // target can also be reached through its canonical adapter, and
            // that alternate predecessor does not prove Z/N == resident A.
            let before = out.len();
            materialize_deferred_nz_from_a(&mut out, &mut state, &mut stats);
            section_pc += approx_code_bytes(&out[before..]);
            debug_assert_eq!(selected_list.get(idx + 1).copied(), Some(target));
            pending_continuation = Some((target, bank));
''')

replace_once(
'''        "superblock-state: A avoided {} reload(s), deferred {} store(s), materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); barriers {}; canonical adapters {}",
''',
'''        "superblock-state: A avoided {} reload(s), deferred {} store(s), materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); lazy A-Z/N deferred {}, elided {}, materialized {}; barriers {}; canonical adapters {}",
''')

replace_once(
'''        stats.fast_rmw_addr_reuse,
        stats.barriers,
        stats.canonical_adapters,
''',
'''        stats.fast_rmw_addr_reuse,
        stats.zn_updates_deferred,
        stats.zn_publications_elided,
        stats.zn_materialized,
        stats.barriers,
        stats.canonical_adapters,
''')

replace_once(
'''        assert!(asm.contains("ldh a, [nes_a] ; canonical adapter A"));
    }
}
''',
'''        assert!(asm.contains("ldh a, [nes_a] ; canonical adapter A"));
    }

    #[test]
    fn resident_a_defers_and_elides_redundant_zn_publication() {
        let mut prg = vec![0xEA; 0x8000];
        // Two consecutive A flag producers in one block. Only the second Z/N
        // value should be published before RTS forces a canonical barrier.
        prg[0..5].copy_from_slice(&[0xA9, 0x01, 0xA9, 0x02, 0x60]);
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
        assert_eq!(
            asm.matches("materialize deferred Z from resident A").count(),
            1
        );
        assert_eq!(
            asm.matches("materialize deferred N from resident A").count(),
            1
        );
    }
}
''')

path.write_text(s, encoding="utf-8")
print("applied conservative resident-A lazy Z/N phase 1")
