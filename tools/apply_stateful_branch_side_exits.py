#!/usr/bin/env python3
from pathlib import Path

p = Path("src/state_superblock.rs")
s = p.read_text()

old = '''    a_materialized: usize,
    x_seed_loads: usize,
'''
new = '''    a_materialized: usize,
    a_side_exit_materialized: usize,
    x_seed_loads: usize,
'''
assert old in s
s = s.replace(old, new, 1)

old = '''    x_materialized: usize,
    y_materialized: usize,
    x_index_uses: usize,
'''
new = '''    x_materialized: usize,
    y_materialized: usize,
    x_side_exit_materialized: usize,
    y_side_exit_materialized: usize,
    x_index_uses: usize,
'''
assert old in s
s = s.replace(old, new, 1)

old = '''    barriers: usize,
    canonical_adapters: usize,
'''
new = '''    barriers: usize,
    branch_side_exits: usize,
    canonical_adapters: usize,
'''
assert old in s
s = s.replace(old, new, 1)

marker = '''fn invalidate_state(state: &mut TraceState) {
'''
assert marker in s
insert = r'''fn emit_stateful_branch(
    out: &mut String,
    ops: &[IrOp],
    state: TraceState,
    stats: &mut StateStats,
    current_bank: u16,
    banks: &BTreeMap<u16, u16>,
) -> bool {
    if ops.len() != 1 {
        return false;
    }
    let IrOp::Branch { flag, when, target } = ops[0] else {
        return false;
    };
    if !banks.contains_key(&target) {
        return false;
    }

    // Branch testing uses host A as scratch.  If architectural A is resident,
    // keep it in D so the preferred fallthrough retains the exact TraceState
    // contract without publishing canonical nes_a.
    if state.a_live {
        writeln!(out, "    ld d, a ; preserve resident A across stateful branch").unwrap();
    }

    match flag {
        Flag::Carry => {
            writeln!(out, "    ldh a, [nes_c_shadow]").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
        }
        Flag::Zero => {
            writeln!(out, "    ldh a, [nes_z_shadow]").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, "    jr {}, :+", if when { "nz" } else { "z" }).unwrap();
        }
        Flag::Negative => {
            writeln!(out, "    ldh a, [nes_n_shadow]").unwrap();
            writeln!(out, "    bit 7, a").unwrap();
            writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
        }
        _ => {
            writeln!(out, "    ldh a, [nes_p]").unwrap();
            writeln!(out, "    and ${:02X}", flag_mask(flag)).unwrap();
            writeln!(out, "    jr {}, :+", if when { "z" } else { "nz" }).unwrap();
        }
    }

    // Taken branch is a side exit to a canonical entry.  Publish only the
    // state that is actually dirty.  The preferred fallthrough below pays no
    // canonical-store cost and keeps the same resident state alive.
    if state.a_dirty {
        debug_assert!(state.a_live);
        writeln!(out, "    ld a, d ; side-exit restore resident A").unwrap();
        writeln!(out, "    ldh [nes_a], a ; superblock side-exit materialize A").unwrap();
        stats.a_side_exit_materialized += 1;
    }
    if state.x_dirty {
        debug_assert!(state.x_b);
        writeln!(out, "    ld a, b ; superblock side-exit materialize X").unwrap();
        writeln!(out, "    ldh [nes_x], a").unwrap();
        stats.x_side_exit_materialized += 1;
    }
    if state.y_dirty {
        debug_assert!(state.y_c);
        writeln!(out, "    ld a, c ; superblock side-exit materialize Y").unwrap();
        writeln!(out, "    ldh [nes_y], a").unwrap();
        stats.y_side_exit_materialized += 1;
    }

    let emitted = emit_static_target(out, target, current_bank, banks, None, 0);
    debug_assert!(emitted);
    writeln!(out, ":").unwrap();

    if state.a_live {
        writeln!(out, "    ld a, d ; restore resident A on branch fallthrough").unwrap();
    }
    stats.branch_side_exits += 1;
    true
}

'''
s = s.replace(marker, insert + marker, 1)

old = '''                    let mut probe = String::new();
                    if emit_static_control(&mut probe, &ops, bank, &banks, None, 0) {
'''
new = '''                    let stateful_branch = matches!(
                        ops.as_slice(),
                        [IrOp::Branch { target, .. }] if banks.contains_key(target)
                    );
                    if stateful_branch {
                        if !pending.is_empty() {
                            let before = out.len();
                            emit_barrier_ops(&mut out, &pending, &mut state, &mut stats);
                            section_pc += approx_code_bytes(&out[before..]);
                            pending.clear();
                        }
                        write_insn_comment(&mut out, instruction);
                        let before = out.len();
                        let emitted = emit_stateful_branch(
                            &mut out, &ops, state, &mut stats, bank, &banks,
                        );
                        debug_assert!(emitted);
                        section_pc += approx_code_bytes(&out[before..]);
                        continue;
                    }

                    let mut probe = String::new();
                    if emit_static_control(&mut probe, &ops, bank, &banks, None, 0) {
'''
assert old in s
s = s.replace(old, new, 1)

old = '''        "superblock-state: A avoided {} reload(s), deferred {} store(s), materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); barriers {}; canonical adapters {}",
        stats.a_reload_avoided,
        stats.a_stores_deferred,
        stats.a_materialized,
        stats.a_seed_loads,
        stats.x_reload_avoided,
        stats.y_reload_avoided,
        stats.x_stores_deferred,
        stats.y_stores_deferred,
        stats.x_materialized,
        stats.y_materialized,
        stats.x_index_uses,
        stats.y_index_uses,
        stats.x_seed_loads,
        stats.y_seed_loads,
        stats.fast_ops,
        stats.fast_compares,
        stats.fast_arithmetic,
        stats.fast_shifts,
        stats.fast_rmw_addr_reuse,
        stats.barriers,
        stats.canonical_adapters,
'''
new = '''        "superblock-state: A avoided {} reload(s), deferred {} store(s), eager materialized {}, side-exit materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); eager materializations X={} Y={}; conditional side-exit materializations X={} Y={} across {} branch side exit(s); cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); barriers {}; canonical adapters {}",
        stats.a_reload_avoided,
        stats.a_stores_deferred,
        stats.a_materialized,
        stats.a_side_exit_materialized,
        stats.a_seed_loads,
        stats.x_reload_avoided,
        stats.y_reload_avoided,
        stats.x_stores_deferred,
        stats.y_stores_deferred,
        stats.x_materialized,
        stats.y_materialized,
        stats.x_side_exit_materialized,
        stats.y_side_exit_materialized,
        stats.branch_side_exits,
        stats.x_index_uses,
        stats.y_index_uses,
        stats.x_seed_loads,
        stats.y_seed_loads,
        stats.fast_ops,
        stats.fast_compares,
        stats.fast_arithmetic,
        stats.fast_shifts,
        stats.fast_rmw_addr_reuse,
        stats.barriers,
        stats.canonical_adapters,
'''
assert old in s
s = s.replace(old, new, 1)

old = '''    #[test]
    fn a_stays_dirty_across_store_logic_and_private_trace_edge() {
'''
new = r'''    #[test]
    fn conditional_side_exit_materializes_dirty_state_only_on_taken_path() {
        let mut prg = vec![0xEA; 0x8000];
        // LDX #$04 / LDA #$3F / BNE $8008 / STA $00 / RTS / NOP / RTS.
        // Both A and X are dirty at the branch.  The taken side exit must
        // publish both; the preferred fallthrough must restore resident A and
        // keep both canonical stores deferred.
        prg[0..10].copy_from_slice(&[
            0xA2, 0x04, 0xA9, 0x3F, 0xD0, 0x02, 0x85, 0x00, 0x60, 0x60,
        ]);
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
        let branch = asm.find("; $8004: $D0 Bne Relative").unwrap();
        let fallthrough = asm.find("nes_8006_trace:").unwrap();
        let between = &asm[branch..fallthrough];
        assert!(between.contains("preserve resident A across stateful branch"));
        assert!(between.contains("superblock side-exit materialize A"));
        assert!(between.contains("superblock side-exit materialize X"));
        assert!(between.contains("restore resident A on branch fallthrough"));
        assert!(!between.contains("superblock materialize X"));
        assert!(!between.contains("superblock materialize A"));
        let fallthrough_body = &asm[fallthrough..];
        assert!(fallthrough_body.contains("ld [$C000], a"));
    }

    #[test]
    fn a_stays_dirty_across_store_logic_and_private_trace_edge() {
'''
assert old in s
s = s.replace(old, new, 1)

p.write_text(s)
