#!/usr/bin/env python3
from pathlib import Path

p = Path('src/state_superblock.rs')
s = p.read_text()

s = s.replace(
'''    chained_edges: usize,\n    elided_jumps: usize,\n''',
'''    chained_edges: usize,\n    nmi_private_chained_edges: usize,\n    elided_jumps: usize,\n''', 1)

s = s.replace(
'''    banks: &BTreeMap<u16, u16>,\n    poll_points: &BTreeSet<u16>,\n) -> SuperblockPlan {\n''',
'''    banks: &BTreeMap<u16, u16>,\n    poll_points: &BTreeSet<u16>,\n    nmi_exclusive: &BTreeSet<u16>,\n) -> SuperblockPlan {\n''', 1)

s = s.replace(
'''            let Some((target, is_jump)) = preferred_successor(block) else {\n                break;\n            };\n            if !selected.contains(&target)\n                || claimed.contains(&target)\n                || banks.get(&current) != banks.get(&target)\n                || incoming.get(&target).copied().unwrap_or(0) != 1\n                || entry_points.contains(&target)\n                || poll_points.contains(&target)\n            {\n''',
'''            let Some((target, is_jump)) = preferred_successor(block) else {\n                break;\n            };\n            let nmi_private_edge =\n                nmi_exclusive.contains(&current) && nmi_exclusive.contains(&target);\n            if !selected.contains(&target)\n                || claimed.contains(&target)\n                || banks.get(&current) != banks.get(&target)\n                || (!nmi_private_edge && incoming.get(&target).copied().unwrap_or(0) != 1)\n                || entry_points.contains(&target)\n                || (!nmi_private_edge && poll_points.contains(&target))\n            {\n''', 1)

s = s.replace(
'''            plan.next.insert(current, target);\n            plan.chained_edges += 1;\n            if is_jump {\n''',
'''            plan.next.insert(current, target);\n            plan.chained_edges += 1;\n            if nmi_private_edge {\n                plan.nmi_private_chained_edges += 1;\n            }\n            if is_jump {\n''', 1)

s = s.replace(
'''    let plan = plan_superblocks(graph, &selected, &banks, &poll_points);\n''',
'''    let plan = plan_superblocks(graph, &selected, &banks, &poll_points, &nmi_exclusive);\n''', 1)

s = s.replace(
'''            "superblock: formed {} multi-block trace(s), chained {} unique-entry same-bank edge(s), elided {} unconditional JMP(s)",\n            plan.multi_block_traces, plan.chained_edges, plan.elided_jumps\n''',
'''            "superblock: formed {} multi-block trace(s), chained {} same-bank edge(s) ({} NMI-private multi-entry/dead-poll), elided {} unconditional JMP(s)",\n            plan.multi_block_traces,\n            plan.chained_edges,\n            plan.nmi_private_chained_edges,\n            plan.elided_jumps\n''', 1)

old_poll = '''        if poll_points.contains(&block.start) {\n            debug_assert!(!continuing);\n            let exclusive = nmi_exclusive.contains(&block.start);\n            if exclusive {\n                writeln!(\n                    out,\n                    "    ; NMI-exclusive safe-point retained as analysis barrier"\n                )\n                .unwrap();\n                writeln!(out, "IF 0").unwrap();\n            }\n            let before = out.len();\n            writeln!(out, "    ldh a, [nes_host_vblank_pending]").unwrap();\n            writeln!(out, "    and a").unwrap();\n            writeln!(out, "    jr z, :+").unwrap();\n            writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();\n            writeln!(out, "    call nes_poll_nmi_hl").unwrap();\n            writeln!(out, "    and a").unwrap();\n            writeln!(out, "    jp nz, nes_nmi_entry").unwrap();\n            writeln!(out, ":").unwrap();\n            section_pc += approx_code_bytes(&out[before..]);\n            if exclusive {\n                writeln!(out, "ENDC").unwrap();\n            }\n        }\n'''
new_poll = '''        if poll_points.contains(&block.start) {\n            let exclusive = nmi_exclusive.contains(&block.start);\n            if continuing {\n                // Inside a proven NMI-only trace, nested NES NMIs are impossible.\n                // Crossing this safe point changes no architectural behavior.\n                debug_assert!(exclusive);\n                writeln!(out, "    ; NMI-private trace crosses dead safe-point poll").unwrap();\n            } else {\n                if exclusive {\n                    writeln!(\n                        out,\n                        "    ; NMI-exclusive safe-point retained as analysis barrier"\n                    )\n                    .unwrap();\n                    writeln!(out, "IF 0").unwrap();\n                }\n                let before = out.len();\n                writeln!(out, "    ldh a, [nes_host_vblank_pending]").unwrap();\n                writeln!(out, "    and a").unwrap();\n                writeln!(out, "    jr z, :+").unwrap();\n                writeln!(out, "    ld hl, ${:04X}", block.start).unwrap();\n                writeln!(out, "    call nes_poll_nmi_hl").unwrap();\n                writeln!(out, "    and a").unwrap();\n                writeln!(out, "    jp nz, nes_nmi_entry").unwrap();\n                writeln!(out, ":").unwrap();\n                section_pc += approx_code_bytes(&out[before..]);\n                if exclusive {\n                    writeln!(out, "ENDC").unwrap();\n                }\n            }\n        }\n'''
assert old_poll in s
s = s.replace(old_poll, new_poll, 1)

s = s.replace(
'''        let plan = plan_superblocks(&graph, &selected, &banks, &polls);\n''',
'''        let plan = plan_superblocks(&graph, &selected, &banks, &polls, &BTreeSet::new());\n''', 1)

p.write_text(s)
