#!/usr/bin/env python3
from pathlib import Path

p = Path("src/state_superblock.rs")
s = p.read_text()

old = '''    #[test]
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
'''
new = '''    #[test]
    fn nmi_private_loop_keeps_resident_x_on_backedge() {
        let mut prg = vec![0xEA; 0x8000];
        // Reset: RTS. NMI: LDX #4 / JMP $8020. Loop: DEX / BNE $8020 / RTS.
        // $8020 is both multiply reached and a backward-loop poll point.  The
        // explicit first JMP gives it a real trace boundary, so proven NMI-only
        // control can enter it privately and the BNE backedge can reuse the
        // exact resident-X contract without spilling X.
        prg[0] = 0x60;
        prg[0x10..0x15].copy_from_slice(&[0xA2, 0x04, 0x4C, 0x20, 0x80]);
        prg[0x20..0x24].copy_from_slice(&[0xCA, 0xD0, 0xFD, 0x60]);
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
        assert!(asm.contains("nes_8020_trace:"));
        assert!(asm.contains("NES canonical superblock entry 8020"));
        assert!(asm.contains("canonical adapter X"));
        let branch = asm.find("; $8021: $D0 Bne Relative").unwrap();
        let after = &asm[branch..];
        let private = after.find("jp nz, nes_8020_trace").unwrap();
        assert!(!after[..private].contains("superblock materialize X"));
    }
'''
assert old in s
p.write_text(s.replace(old, new, 1))
