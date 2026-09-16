#!/usr/bin/env python3
from pathlib import Path

p = Path("src/state_superblock.rs")
s = p.read_text()

# This helper is intentionally written against the post-revert baseline.
assert "a_side_exit_materialized" not in s, "side-exit experiment must be reverted first"
assert "nmi_private_chained_edges" not in s

old = '''struct SuperblockPlan {
    order: Vec<u16>,
    next: BTreeMap<u16, u16>,
    multi_block_traces: usize,
    chained_edges: usize,
    elided_jumps: usize,
    disabled_for_unresolved_indirect: bool,
}
'''
new = '''struct SuperblockPlan {
    order: Vec<u16>,
    next: BTreeMap<u16, u16>,
    multi_block_traces: usize,
    chained_edges: usize,
    nmi_private_chained_edges: usize,
    elided_jumps: usize,
    disabled_for_unresolved_indirect: bool,
}
'''
assert old in s
s = s.replace(old, new, 1)

old = '''fn plan_superblocks(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    banks: &BTreeMap<u16, u16>,
    poll_points: &BTreeSet<u16>,
) -> SuperblockPlan {
'''
new = '''fn plan_superblocks(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    banks: &BTreeMap<u16, u16>,
    poll_points: &BTreeSet<u16>,
    nmi_exclusive: &BTreeSet<u16>,
) -> SuperblockPlan {
'''
assert old in s
s = s.replace(old, new, 1)

old = '''            if !selected.contains(&target)
                || claimed.contains(&target)
                || banks.get(&current) != banks.get(&target)
                || incoming.get(&target).copied().unwrap_or(0) != 1
                || entry_points.contains(&target)
                || poll_points.contains(&target)
            {
                break;
            }
            plan.next.insert(current, target);
            plan.chained_edges += 1;
'''
new = '''            let nmi_private_edge =
                nmi_exclusive.contains(&current) && nmi_exclusive.contains(&target);
            if !selected.contains(&target)
                || claimed.contains(&target)
                || banks.get(&current) != banks.get(&target)
                || (!nmi_private_edge && incoming.get(&target).copied().unwrap_or(0) != 1)
                || entry_points.contains(&target)
                || (!nmi_private_edge && poll_points.contains(&target))
            {
                break;
            }
            plan.next.insert(current, target);
            plan.chained_edges += 1;
            if nmi_private_edge {
                plan.nmi_private_chained_edges += 1;
            }
'''
assert old in s
s = s.replace(old, new, 1)

old = '''    fast_rmw_addr_reuse: usize,
    barriers: usize,
    canonical_adapters: usize,
}
'''
new = '''    fast_rmw_addr_reuse: usize,
    barriers: usize,
    nmi_private_branches: usize,
    nmi_private_jumps: usize,
    canonical_adapters: usize,
}
'''
assert old in s
s = s.replace(old, new, 1)

marker = '''fn invalidate_state(state: &mut TraceState) {
'''
assert marker in s
helper = r'''fn emit_nmi_private_control(
    out: &mut String,
    ops: &[IrOp],
    state: TraceState,
    source: u16,
    current_bank: u16,
    nmi_exclusive: &BTreeSet<u16>,
    entry_contracts: &BTreeMap<u16, (u16, TraceState)>,
    stats: &mut StateStats,
) -> bool {
    if !nmi_exclusive.contains(&source) || ops.len() != 1 {
        return false;
    }

    let target = match ops[0] {
        IrOp::Branch { target, .. } | IrOp::Jump(target) => target,
        _ => return false,
    };
    if !nmi_exclusive.contains(&target) {
        return false;
    }
    let Some(&(target_bank, contract)) = entry_contracts.get(&target) else {
        return false;
    };
    if target_bank != current_bank || contract != state {
        return false;
    }

    match ops[0] {
        IrOp::Jump(_) => {
            writeln!(
                out,
                "    jp nes_{target:04X}_trace ; NMI-private exact-contract jump"
            )
            .unwrap();
            stats.nmi_private_jumps += 1;
            true
        }
        IrOp::Branch { flag, when, .. } => {
            // Branch testing needs host A as scratch.  Keep this first pass
            // strictly zero-cost for resident A: only specialize contracts in
            // which A is not live. X/Y remain resident in B/C untouched.
            if state.a_live {
                return false;
            }
            match flag {
                Flag::Carry => {
                    writeln!(out, "    ldh a, [nes_c_shadow]").unwrap();
                    writeln!(out, "    and a").unwrap();
                    writeln!(
                        out,
                        "    jp {}, nes_{target:04X}_trace ; NMI-private exact-contract branch",
                        if when { "nz" } else { "z" }
                    )
                    .unwrap();
                }
                Flag::Zero => {
                    writeln!(out, "    ldh a, [nes_z_shadow]").unwrap();
                    writeln!(out, "    and a").unwrap();
                    writeln!(
                        out,
                        "    jp {}, nes_{target:04X}_trace ; NMI-private exact-contract branch",
                        if when { "z" } else { "nz" }
                    )
                    .unwrap();
                }
                Flag::Negative => {
                    writeln!(out, "    ldh a, [nes_n_shadow]").unwrap();
                    writeln!(out, "    bit 7, a").unwrap();
                    writeln!(
                        out,
                        "    jp {}, nes_{target:04X}_trace ; NMI-private exact-contract branch",
                        if when { "nz" } else { "z" }
                    )
                    .unwrap();
                }
                _ => {
                    writeln!(out, "    ldh a, [nes_p]").unwrap();
                    writeln!(out, "    and ${:02X}", flag_mask(flag)).unwrap();
                    writeln!(
                        out,
                        "    jp {}, nes_{target:04X}_trace ; NMI-private exact-contract branch",
                        if when { "nz" } else { "z" }
                    )
                    .unwrap();
                }
            }
            stats.nmi_private_branches += 1;
            true
        }
        _ => false,
    }
}

'''
s = s.replace(marker, helper + marker, 1)

old = '''    let poll_points = nmi_poll_points(graph, &selected);
    let nmi_exclusive = nmi_exclusive_blocks(graph, &selected, options.reset, nmi, irq);
    let plan = plan_superblocks(graph, &selected, &banks, &poll_points);
'''
new = '''    let poll_points = nmi_poll_points(graph, &selected);
    let nmi_exclusive = nmi_exclusive_blocks(graph, &selected, options.reset, nmi, irq);
    let plan = plan_superblocks(graph, &selected, &banks, &poll_points, &nmi_exclusive);
'''
assert old in s
s = s.replace(old, new, 1)

old = '''            "superblock: formed {} multi-block trace(s), chained {} unique-entry same-bank edge(s), elided {} unconditional JMP(s)",
            plan.multi_block_traces, plan.chained_edges, plan.elided_jumps
'''
new = '''            "superblock: formed {} multi-block trace(s), chained {} same-bank edge(s) ({} NMI-private multi-entry/dead-poll), elided {} unconditional JMP(s)",
            plan.multi_block_traces,
            plan.chained_edges,
            plan.nmi_private_chained_edges,
            plan.elided_jumps
'''
assert old in s
s = s.replace(old, new, 1)

old = '''        if poll_points.contains(&block.start) {
            debug_assert!(!continuing);
            let exclusive = nmi_exclusive.contains(&block.start);
            if exclusive {
                writeln!(
                    out,
                    "    ; NMI-exclusive safe-point retained as analysis barrier"
                )
                .unwrap();
                writeln!(out, "IF 0").unwrap();
            }
            let before = out.len();
            writeln!(out, "    ldh a, [nes_host_vblank_pending]").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, "    jr z, :+").unwrap();
            writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
            writeln!(out, "    call nes_poll_nmi_hl").unwrap();
            writeln!(out, "    and a").unwrap();
            writeln!(out, "    jp nz, nes_nmi_entry").unwrap();
            writeln!(out, ":").unwrap();
            section_pc += approx_code_bytes(&out[before..]);
            if exclusive {
                writeln!(out, "ENDC").unwrap();
            }
        }
'''
new = '''        if poll_points.contains(&block.start) {
            let exclusive = nmi_exclusive.contains(&block.start);
            if continuing {
                // A proven NMI-only trace may cross a backward-loop safe point:
                // nested NES NMIs are impossible, so this poll is genuinely dead.
                debug_assert!(exclusive);
                writeln!(
                    out,
                    "    ; NMI-private trace crosses dead safe-point poll"
                )
                .unwrap();
            } else {
                if exclusive {
                    writeln!(
                        out,
                        "    ; NMI-exclusive safe-point retained as analysis barrier"
                    )
                    .unwrap();
                    writeln!(out, "IF 0").unwrap();
                }
                let before = out.len();
                writeln!(out, "    ldh a, [nes_host_vblank_pending]").unwrap();
                writeln!(out, "    and a").unwrap();
                writeln!(out, "    jr z, :+").unwrap();
                writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();
                writeln!(out, "    call nes_poll_nmi_hl").unwrap();
                writeln!(out, "    and a").unwrap();
                writeln!(out, "    jp nz, nes_nmi_entry").unwrap();
                writeln!(out, ":").unwrap();
                section_pc += approx_code_bytes(&out[before..]);
                if exclusive {
                    writeln!(out, "ENDC").unwrap();
                }
            }
        }
'''
assert old in s
s = s.replace(old, new, 1)

old = '''                    let mut probe = String::new();
                    if emit_static_control(&mut probe, &ops, bank, &banks, None, 0) {
                        if !pending.is_empty() {
                            let before = out.len();
                            emit_barrier_ops(&mut out, &pending, &mut state, &mut stats);
                            section_pc += approx_code_bytes(&out[before..]);
                            pending.clear();
                        }
                        sync_state(&mut out, &mut state, &mut stats);
                        write_insn_comment(&mut out, instruction);
                        let before = out.len();
                        let _ = emit_static_control(
                            &mut out,
                            &ops,
                            bank,
                            &banks,
                            Some(&section_offs),
                            section_pc,
                        );
                        section_pc += approx_code_bytes(&out[before..]);
                        continue;
                    }
'''
new = '''                    let mut probe = String::new();
                    if emit_static_control(&mut probe, &ops, bank, &banks, None, 0) {
                        if !pending.is_empty() {
                            let before = out.len();
                            emit_barrier_ops(&mut out, &pending, &mut state, &mut stats);
                            section_pc += approx_code_bytes(&out[before..]);
                            pending.clear();
                        }
                        write_insn_comment(&mut out, instruction);
                        let before = out.len();
                        if emit_nmi_private_control(
                            &mut out,
                            &ops,
                            state,
                            block.start,
                            bank,
                            &nmi_exclusive,
                            &entry_contracts,
                            &mut stats,
                        ) {
                            section_pc += approx_code_bytes(&out[before..]);
                            continue;
                        }
                        sync_state(&mut out, &mut state, &mut stats);
                        let _ = emit_static_control(
                            &mut out,
                            &ops,
                            bank,
                            &banks,
                            Some(&section_offs),
                            section_pc,
                        );
                        section_pc += approx_code_bytes(&out[before..]);
                        continue;
                    }
'''
assert old in s
s = s.replace(old, new, 1)

old = '''        "superblock-state: A avoided {} reload(s), deferred {} store(s), materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); barriers {}; canonical adapters {}",
'''
new = '''        "superblock-state: A avoided {} reload(s), deferred {} store(s), materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); barriers {}; NMI-private exact edges branches={} jumps={}; canonical adapters {}",
'''
assert old in s
s = s.replace(old, new, 1)

old = '''        stats.fast_rmw_addr_reuse,
        stats.barriers,
        stats.canonical_adapters,
'''
new = '''        stats.fast_rmw_addr_reuse,
        stats.barriers,
        stats.nmi_private_branches,
        stats.nmi_private_jumps,
        stats.canonical_adapters,
'''
assert old in s
s = s.replace(old, new, 1)

old = '''        let plan = plan_superblocks(&graph, &selected, &banks, &polls);
        assert!(!plan.next.values().any(|&target| target == 0x8005));
    }

    #[test]
    fn compare_keeps_dirty_x_resident_until_real_control_barrier() {
'''
new = '''        let plan = plan_superblocks(&graph, &selected, &banks, &polls, &BTreeSet::new());
        assert!(!plan.next.values().any(|&target| target == 0x8005));
    }

    #[test]
    fn nmi_private_loop_keeps_resident_x_on_backedge() {
        let mut prg = vec![0xEA; 0x8000];
        // Reset: RTS. NMI: LDX #4 / DEX / BNE DEX / RTS.
        // $8012 is both multiply reached and a backward-loop poll point.  In
        // proven NMI-only code it should become a private trace entry, and the
        // BNE backedge should jump directly to that exact resident-X contract.
        prg[0] = 0x60;
        prg[0x10..0x16].copy_from_slice(&[0xA2, 0x04, 0xCA, 0xD0, 0xFD, 0x60]);
        let graph = cfg::discover(0, &prg, &[0x8000, 0x8010]).unwrap();
        let asm = emit_cfg_with_interrupts(
            &graph,
            EmitOptions {
                reset: 0x8000,
                max_blocks: Some(16),
                debug_trace: false,
            },
            0x8010,
            0x8000,
        );
        assert!(asm.contains("nes_8012_trace:"));
        assert!(asm.contains("NES canonical superblock entry 8012"));
        assert!(asm.contains("canonical adapter X"));
        let branch = asm.find("; $8013: $D0 Bne Relative").unwrap();
        let after = &asm[branch..];
        let private = after.find("jp nz, nes_8012_trace").unwrap();
        assert!(!after[..private].contains("superblock materialize X"));
    }

    #[test]
    fn compare_keeps_dirty_x_resident_until_real_control_barrier() {
'''
assert old in s
s = s.replace(old, new, 1)

p.write_text(s)
