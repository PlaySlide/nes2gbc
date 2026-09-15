from pathlib import Path

p = Path("src/state_superblock.rs")
s = p.read_text()

old = """    fast_arithmetic: usize,
    barriers: usize,"""
new = """    fast_arithmetic: usize,
    fast_shifts: usize,
    barriers: usize,"""
assert old in s
s = s.replace(old, new, 1)

marker = "fn fast_op_supported(op: &IrOp) -> bool {"
helper = r'''fn emit_fast_modify_value(out: &mut String, modify: ModifyOp, stats: &mut StateStats) {
    match modify {
        ModifyOp::Inc => {
            writeln!(out, "    inc a").unwrap();
            emit_update_nz(out);
        }
        ModifyOp::Dec => {
            writeln!(out, "    dec a").unwrap();
            emit_update_nz(out);
        }
        ModifyOp::Asl | ModifyOp::Lsr | ModifyOp::Rol | ModifyOp::Ror => {
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
        }
    }
}

'''
assert marker in s
s = s.replace(marker, helper + marker, 1)

old = """        IrOp::Modify {
            op: ModifyOp::Inc | ModifyOp::Dec,
            target: ModifyTarget::Accumulator,
        } => true,
        IrOp::Modify {
            op: ModifyOp::Inc | ModifyOp::Dec,
            target: ModifyTarget::Memory(mem),
        } => fast_operand_supported(mem) && fast_store_supported(mem),"""
new = """        IrOp::Modify {
            op: ModifyOp::Inc
                | ModifyOp::Dec
                | ModifyOp::Asl
                | ModifyOp::Lsr
                | ModifyOp::Rol
                | ModifyOp::Ror,
            target: ModifyTarget::Accumulator,
        } => true,
        IrOp::Modify {
            op: ModifyOp::Inc
                | ModifyOp::Dec
                | ModifyOp::Asl
                | ModifyOp::Lsr
                | ModifyOp::Rol
                | ModifyOp::Ror,
            target: ModifyTarget::Memory(mem),
        } => fast_operand_supported(mem) && fast_store_supported(mem),"""
assert old in s
s = s.replace(old, new, 1)

start = s.index("        IrOp::Modify { op: modify, target } => match target {")
end = s.index("        IrOp::Compare { reg, rhs } => {", start)
replacement = '''        IrOp::Modify { op: modify, target } => match target {
            ModifyTarget::Accumulator => {
                writeln!(out, "    ldh a, [nes_a]").unwrap();
                emit_fast_modify_value(out, modify, stats);
                writeln!(out, "    ldh [nes_a], a").unwrap();
            }
            ModifyTarget::Memory(mem) => {
                emit_operand_load(out, mem, state, stats);
                emit_fast_modify_value(out, modify, stats);
                emit_operand_store(out, mem, state, stats);
            }
        },
'''
s = s[:start] + replacement + s[end:]

old = '''        "superblock-state: avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC); barriers {}; canonical adapters {}",'''
new = '''        "superblock-state: avoided {} X + {} Y HRAM reload(s); deferred {} X + {} Y canonical store(s); materialized {} X + {} Y at barriers/side exits; cached indexed uses X={} Y={}; seeds X={} Y={}; fast ops {} ({} compares, {} ADC/SBC, {} shifts/rotates); barriers {}; canonical adapters {}",'''
assert old in s
s = s.replace(old, new, 1)
old = """        stats.fast_arithmetic,
        stats.barriers,"""
new = """        stats.fast_arithmetic,
        stats.fast_shifts,
        stats.barriers,"""
assert old in s
s = s.replace(old, new, 1)

insert = r'''
    #[test]
    fn shifts_keep_dirty_x_resident_and_emit_inline() {
        let mut prg = vec![0xEA; 0x8000];
        // LDX #4 / LDA #$81 / ASL A / LSR A / SEC / ROL A / ROR A /
        // ASL $00,X / LSR $00,X / ROL $00,X / ROR $00,X / STX $10 / RTS.
        prg[0..20].copy_from_slice(&[
            0xA2, 0x04, 0xA9, 0x81, 0x0A, 0x4A, 0x38, 0x2A, 0x6A, 0x16, 0x00, 0x56, 0x00,
            0x36, 0x00, 0x76, 0x00, 0x86, 0x10, 0x60,
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
        let first_shift = asm.find("; $8004: $0A Asl Accumulator").unwrap();
        let store_x = asm.find("; $8011: $86 Stx ZeroPage").unwrap();
        let between = &asm[first_shift..store_x];
        assert!(between.contains("superblock fast ASL"));
        assert!(between.contains("superblock fast LSR"));
        assert!(between.contains("superblock fast ROL"));
        assert!(between.contains("superblock fast ROR"));
        assert!(!between.contains("superblock materialize X"));
        assert!(asm[store_x..].contains("superblock cached X"));
    }
'''
pos = s.rfind("\n}\n")
assert pos != -1
s = s[:pos] + insert + s[pos:]

p.write_text(s)
