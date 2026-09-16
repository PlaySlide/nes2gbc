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
'''    zn_updates_deferred: usize,
    zn_publications_elided: usize,
    zn_materialized: usize,
    barriers: usize,
''',
'''    zn_updates_deferred: usize,
    zn_publications_elided: usize,
    zn_materialized: usize,
    zn_direct_branches: usize,
    zn_branch_z_stores_elided: usize,
    zn_branch_n_stores_elided: usize,
    barriers: usize,
''')

insert_after = '''fn emit_barrier_ops(
    out: &mut String,
    ops: &[IrOp],
    state: &mut TraceState,
    stats: &mut StateStats,
) {
    if ops.is_empty() {
        return;
    }
    sync_state(out, state, stats);
    invalidate_state(state);
    out.push_str(&lr35902::emit_ops(ops));
    stats.barriers += 1;
}
'''

addition = r'''

fn mnemonic_writes_zn(m: crate::cpu6502::Mnemonic) -> bool {
    use crate::cpu6502::Mnemonic::*;
    matches!(
        m,
        Lda | Ldx
            | Ldy
            | Tax
            | Tay
            | Txa
            | Tya
            | Tsx
            | Pla
            | And
            | Ora
            | Eor
            | Adc
            | Sbc
            | Cmp
            | Cpx
            | Cpy
            | Bit
            | Inc
            | Dec
            | Inx
            | Iny
            | Dex
            | Dey
            | Asl
            | Lsr
            | Rol
            | Ror
            | Plp
            | Rti
    )
}

fn mnemonic_reads_z(m: crate::cpu6502::Mnemonic) -> bool {
    use crate::cpu6502::Mnemonic::*;
    matches!(m, Beq | Bne | Php | Brk)
}

fn mnemonic_reads_n(m: crate::cpu6502::Mnemonic) -> bool {
    use crate::cpu6502::Mnemonic::*;
    matches!(m, Bmi | Bpl | Php | Brk)
}

fn zn_flag_live_out(
    graph: &ControlFlowGraph,
    selected: &BTreeSet<u16>,
    poll_points: &BTreeSet<u16>,
    nmi_exclusive: &BTreeSet<u16>,
    zero: bool,
) -> BTreeMap<u16, bool> {
    let mut uses = BTreeMap::new();
    let mut defs = BTreeMap::new();
    let mut unknown_exit = BTreeMap::new();
    let mut succ: BTreeMap<u16, Vec<u16>> = BTreeMap::new();

    for &addr in selected {
        let Some(block) = graph.blocks.get(&addr) else {
            continue;
        };
        let mut seen_def = false;
        let mut use_before_def = poll_points.contains(&addr) && !nmi_exclusive.contains(&addr);
        let mut any_def = false;
        for insn in &block.instructions {
            let m = insn.def.mnemonic;
            let reads = if zero {
                mnemonic_reads_z(m)
            } else {
                mnemonic_reads_n(m)
            };
            if reads && !seen_def {
                use_before_def = true;
            }
            if mnemonic_writes_zn(m) {
                seen_def = true;
                any_def = true;
            }
        }

        let dynamic = block.instructions.last().is_none_or(|last| {
            use crate::cpu6502::{AddressingMode, Mnemonic};
            matches!(last.def.mnemonic, Mnemonic::Jsr | Mnemonic::Rts | Mnemonic::Brk)
                || (last.def.mnemonic == Mnemonic::Jmp
                    && last.def.mode == AddressingMode::Indirect)
        });
        let nexts = block
            .edges
            .iter()
            .filter_map(|edge| edge.target)
            .filter(|target| selected.contains(target))
            .collect::<Vec<_>>();

        uses.insert(addr, use_before_def);
        defs.insert(addr, any_def);
        unknown_exit.insert(addr, dynamic);
        succ.insert(addr, nexts);
    }

    let mut live_in: BTreeMap<u16, bool> = selected.iter().map(|&a| (a, false)).collect();
    let mut live_out: BTreeMap<u16, bool> = selected.iter().map(|&a| (a, false)).collect();
    loop {
        let mut changed = false;
        for &addr in selected.iter().rev() {
            let out = unknown_exit.get(&addr).copied().unwrap_or(true)
                || succ.get(&addr).is_none_or(|nexts| {
                    nexts
                        .iter()
                        .any(|target| live_in.get(target).copied().unwrap_or(true))
                });
            let inn = uses.get(&addr).copied().unwrap_or(true)
                || (out && !defs.get(&addr).copied().unwrap_or(false));
            if live_out.get(&addr).copied() != Some(out) {
                live_out.insert(addr, out);
                changed = true;
            }
            if live_in.get(&addr).copied() != Some(inn) {
                live_in.insert(addr, inn);
                changed = true;
            }
        }
        if !changed {
            break;
        }
    }
    live_out
}
'''
replace_once(insert_after, insert_after + addition)

replace_once(
'''    let nmi_exclusive = nmi_exclusive_blocks(graph, &selected, options.reset, nmi, irq);
    let plan = plan_superblocks(graph, &selected, &banks, &poll_points, &nmi_exclusive);
''',
'''    let nmi_exclusive = nmi_exclusive_blocks(graph, &selected, options.reset, nmi, irq);
    let z_live_out = zn_flag_live_out(graph, &selected, &poll_points, &nmi_exclusive, true);
    let n_live_out = zn_flag_live_out(graph, &selected, &poll_points, &nmi_exclusive, false);
    let plan = plan_superblocks(graph, &selected, &banks, &poll_points, &nmi_exclusive);
''')

needle = '''                    let mut probe = String::new();
                    if emit_static_control(&mut probe, &ops, bank, &banks, None, 0) {
'''
replacement = r'''                    let resident_zn_branch = if pending.is_empty() && state.zn_a_pending {
                        match ops.as_slice() {
                            [IrOp::Branch { flag, when, target }]
                                if matches!(flag, Flag::Zero | Flag::Negative)
                                    && banks.contains_key(target) =>
                            {
                                Some((*flag, *when, *target))
                            }
                            _ => None,
                        }
                    } else {
                        None
                    };

                    if let Some((flag, when, target)) = resident_zn_branch {
                        let before = out.len();
                        // The branch consumes the still-resident producer value directly.
                        // Canonical A/X/Y remain synchronized for the taken side exit.
                        materialize_a(&mut out, &mut state, &mut stats);
                        debug_assert!(state.a_live && state.zn_a_pending);

                        let z_needed = z_live_out.get(&block.start).copied().unwrap_or(true);
                        let n_needed = n_live_out.get(&block.start).copied().unwrap_or(true);
                        if z_needed {
                            writeln!(
                                out,
                                "    ldh [nes_z_shadow], a ; preserve live Z across direct branch"
                            )
                            .unwrap();
                        } else {
                            writeln!(out, "    ; dead post-branch Z publication elided").unwrap();
                            stats.zn_branch_z_stores_elided += 1;
                        }
                        if n_needed {
                            writeln!(
                                out,
                                "    ldh [nes_n_shadow], a ; preserve live N across direct branch"
                            )
                            .unwrap();
                        } else {
                            writeln!(out, "    ; dead post-branch N publication elided").unwrap();
                            stats.zn_branch_n_stores_elided += 1;
                        }
                        state.zn_a_pending = false;
                        if z_needed || n_needed {
                            stats.zn_materialized += 1;
                        } else {
                            stats.zn_publications_elided += 1;
                        }

                        // X/Y publication uses host A. Preserve the branch value only
                        // when that publication can actually clobber it.
                        let save_a = state.x_dirty || state.y_dirty;
                        if save_a {
                            writeln!(out, "    ld d, a ; preserve resident branch value").unwrap();
                        }
                        sync_xy(&mut out, &mut state, &mut stats);
                        if save_a {
                            writeln!(out, "    ld a, d ; restore resident branch value").unwrap();
                        }
                        state.a_live = true;
                        state.a_dirty = false;

                        write_insn_comment(&mut out, instruction);
                        match flag {
                            Flag::Zero => {
                                writeln!(out, "    and a ; direct resident Z branch").unwrap();
                                writeln!(
                                    out,
                                    "    jr {}, :+",
                                    if when { "nz" } else { "z" }
                                )
                                .unwrap();
                            }
                            Flag::Negative => {
                                writeln!(out, "    bit 7, a ; direct resident N branch").unwrap();
                                writeln!(
                                    out,
                                    "    jr {}, :+",
                                    if when { "z" } else { "nz" }
                                )
                                .unwrap();
                            }
                            _ => unreachable!(),
                        }
                        let target_pc = section_pc + approx_code_bytes(&out[before..]);
                        let _ = emit_static_target(
                            &mut out,
                            target,
                            bank,
                            &banks,
                            Some(&section_offs),
                            target_pc,
                        );
                        writeln!(out, ":").unwrap();
                        stats.zn_direct_branches += 1;
                        section_pc += approx_code_bytes(&out[before..]);
                        continue;
                    }

                    let mut probe = String::new();
                    if emit_static_control(&mut probe, &ops, bank, &banks, None, 0) {
'''
replace_once(needle, replacement)

replace_once(
'''        "superblock-state: A avoided {} reload(s), deferred {} store(s), materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); lazy A-Z/N deferred {}, elided {}, materialized {}; barriers {}; canonical adapters {}",
''',
'''        "superblock-state: A avoided {} reload(s), deferred {} store(s), materialized {}, seeds {}; avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates, {} indexed RMW addr reuses); lazy A-Z/N deferred {}, elided {}, materialized {}; direct Z/N branches {}, dead branch stores Z={} N={}; barriers {}; canonical adapters {}",
''')

replace_once(
'''        stats.zn_updates_deferred,
        stats.zn_publications_elided,
        stats.zn_materialized,
        stats.barriers,
''',
'''        stats.zn_updates_deferred,
        stats.zn_publications_elided,
        stats.zn_materialized,
        stats.zn_direct_branches,
        stats.zn_branch_z_stores_elided,
        stats.zn_branch_n_stores_elided,
        stats.barriers,
''')

# Add a focused codegen test: both branch successors overwrite Z/N before use,
# so the resident producer can feed BEQ directly and neither shadow store is needed.
replace_once(
'''    #[test]
    fn a_stays_dirty_across_store_logic_and_private_trace_edge() {
''',
r'''    #[test]
    fn resident_a_branch_consumes_zn_directly_when_successors_kill_flags() {
        let mut prg = vec![0xEA; 0x8000];
        // LDA #0 / BEQ $8008 / LDA #1 / RTS / NOP / LDA #2 / RTS.
        // Both successors replace Z/N before any later read.
        prg[0..11].copy_from_slice(&[
            0xA9, 0x00, 0xF0, 0x04, 0xA9, 0x01, 0x60, 0xEA, 0xA9, 0x02, 0x60,
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
        assert!(asm.contains("and a ; direct resident Z branch"));
        assert!(asm.contains("dead post-branch Z publication elided"));
        assert!(asm.contains("dead post-branch N publication elided"));
    }

    #[test]
    fn a_stays_dirty_across_store_logic_and_private_trace_edge() {
''')

path.write_text(s, encoding="utf-8")
