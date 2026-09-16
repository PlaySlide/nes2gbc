#!/usr/bin/env python3
from pathlib import Path
import re

p = Path('src/state_superblock.rs')
s = p.read_text()

# Restore conservative superblock planning: unique-entry targets and poll
# boundaries stay barriers even inside NMI-exclusive code.
s = s.replace('    nmi_private_chained_edges: usize,\n', '', 1)
s = s.replace('    poll_points: &BTreeSet<u16>,\n    nmi_exclusive: &BTreeSet<u16>,\n) -> SuperblockPlan {',
              '    poll_points: &BTreeSet<u16>,\n) -> SuperblockPlan {', 1)
s = s.replace('''            let nmi_private_edge =\n                nmi_exclusive.contains(&current) && nmi_exclusive.contains(&target);\n''', '', 1)
s = s.replace('''                || (!nmi_private_edge && incoming.get(&target).copied().unwrap_or(0) != 1)\n                || entry_points.contains(&target)\n                || (!nmi_private_edge && poll_points.contains(&target))\n''',
              '''                || incoming.get(&target).copied().unwrap_or(0) != 1\n                || entry_points.contains(&target)\n                || poll_points.contains(&target)\n''', 1)
s = s.replace('''            if nmi_private_edge {\n                plan.nmi_private_chained_edges += 1;\n            }\n''', '', 1)
s = s.replace('''    let plan = plan_superblocks(graph, &selected, &banks, &poll_points, &nmi_exclusive);\n''',
              '''    let plan = plan_superblocks(graph, &selected, &banks, &poll_points);\n''', 1)
s = s.replace('''            "superblock: formed {} multi-block trace(s), chained {} same-bank edge(s) ({} NMI-private multi-entry/dead-poll), elided {} unconditional JMP(s)",\n            plan.multi_block_traces,\n            plan.chained_edges,\n            plan.nmi_private_chained_edges,\n            plan.elided_jumps\n''',
              '''            "superblock: formed {} multi-block trace(s), chained {} unique-entry same-bank edge(s), elided {} unconditional JMP(s)",\n            plan.multi_block_traces, plan.chained_edges, plan.elided_jumps\n''', 1)

old_poll = '''        if poll_points.contains(&block.start) {\n            let exclusive = nmi_exclusive.contains(&block.start);\n            if continuing {\n                // A proven NMI-only trace may cross a backward-loop safe point:\n                // nested NES NMIs are impossible, so this poll is genuinely dead.\n                debug_assert!(exclusive);\n                writeln!(out, "    ; NMI-private trace crosses dead safe-point poll").unwrap();\n            } else {\n                if exclusive {\n                    writeln!(\n                        out,\n                        "    ; NMI-exclusive safe-point retained as analysis barrier"\n                    )\n                    .unwrap();\n                    writeln!(out, "IF 0").unwrap();\n                }\n                let before = out.len();\n                writeln!(out, "    ldh a, [nes_host_vblank_pending]").unwrap();\n                writeln!(out, "    and a").unwrap();\n                writeln!(out, "    jr z, :+").unwrap();\n                writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();\n                writeln!(out, "    call nes_poll_nmi_hl").unwrap();\n                writeln!(out, "    and a").unwrap();\n                writeln!(out, "    jp nz, nes_nmi_entry").unwrap();\n                writeln!(out, ":").unwrap();\n                section_pc += approx_code_bytes(&out[before..]);\n                if exclusive {\n                    writeln!(out, "ENDC").unwrap();\n                }\n            }\n        }\n'''
new_poll = '''        if poll_points.contains(&block.start) {\n            debug_assert!(!continuing);\n            let exclusive = nmi_exclusive.contains(&block.start);\n            if exclusive {\n                writeln!(\n                    out,\n                    "    ; NMI-exclusive safe-point retained as analysis barrier"\n                )\n                .unwrap();\n                writeln!(out, "IF 0").unwrap();\n            }\n            let before = out.len();\n            writeln!(out, "    ldh a, [nes_host_vblank_pending]").unwrap();\n            writeln!(out, "    and a").unwrap();\n            writeln!(out, "    jr z, :+").unwrap();\n            writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();\n            writeln!(out, "    call nes_poll_nmi_hl").unwrap();\n            writeln!(out, "    and a").unwrap();\n            writeln!(out, "    jp nz, nes_nmi_entry").unwrap();\n            writeln!(out, ":").unwrap();\n            section_pc += approx_code_bytes(&out[before..]);\n            if exclusive {\n                writeln!(out, "ENDC").unwrap();\n            }\n        }\n'''
assert old_poll in s
s = s.replace(old_poll, new_poll, 1)

# Update the existing planner regression call back to the conservative signature.
s = s.replace('let plan = plan_superblocks(&graph, &selected, &banks, &polls, &BTreeSet::new());',
              'let plan = plan_superblocks(&graph, &selected, &banks, &polls);', 1)

# Replace the old multi-entry loop test with a direct unit test for the part we
# are retaining: exact-contract private control to an already-existing trace.
pat = re.compile(r'    #\[test\]\n    fn nmi_private_loop_keeps_resident_x_on_backedge\(\) \{.*?\n    \}\n\n', re.S)
replacement = '''    #[test]\n    fn nmi_private_exact_branch_reuses_existing_trace_contract() {\n        let mut out = String::new();\n        let state = TraceState {\n            a_live: false,\n            a_dirty: false,\n            x_b: true,\n            x_dirty: true,\n            y_c: false,\n            y_dirty: false,\n        };\n        let mut entries = BTreeMap::new();\n        entries.insert(0x8020, (3, state));\n        let exclusive = BTreeSet::from([0x8010, 0x8020]);\n        let mut stats = StateStats::default();\n        let emitted = emit_nmi_private_control(\n            &mut out,\n            &[IrOp::Branch {\n                flag: Flag::Zero,\n                when: false,\n                target: 0x8020,\n            }],\n            state,\n            0x8010,\n            3,\n            &exclusive,\n            &entries,\n            &mut stats,\n        );\n        assert!(emitted);\n        assert!(out.contains("jp nz, nes_8020_trace"));\n        assert!(!out.contains("materialize X"));\n        assert_eq!(stats.nmi_private_branches, 1);\n    }\n\n'''
s, n = pat.subn(replacement, s, count=1)
assert n == 1, 'failed to replace NMI-private loop regression'

p.write_text(s)
