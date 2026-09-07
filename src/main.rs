use std::{collections::BTreeMap, env, fs, path::PathBuf, process::ExitCode};

use nes2gbc::{assets, cfg, cpu6502, ines, recompile};

fn print_hot_profile(graph: &cfg::ControlFlowGraph) {
    let mut mnemonics: BTreeMap<String, usize> = BTreeMap::new();
    let mut modes: BTreeMap<String, usize> = BTreeMap::new();
    let mut indexed = 0usize;
    let mut branches = 0usize;
    let mut loads_stores = 0usize;
    let mut arithmetic = 0usize;
    let mut rmw = 0usize;
    let mut stack_control = 0usize;

    for instruction in graph.blocks.values().flat_map(|block| &block.instructions) {
        *mnemonics.entry(format!("{:?}", instruction.def.mnemonic)).or_default() += 1;
        *modes.entry(format!("{:?}", instruction.def.mode)).or_default() += 1;

        use cpu6502::AddressingMode as M;
        use cpu6502::Mnemonic as O;

        if matches!(
            instruction.def.mode,
            M::ZeroPageX | M::ZeroPageY | M::AbsoluteX | M::AbsoluteY |
            M::IndexedIndirect | M::IndirectIndexed
        ) {
            indexed += 1;
        }

        match instruction.def.mnemonic {
            O::Bcc | O::Bcs | O::Beq | O::Bmi | O::Bne | O::Bpl | O::Bvc | O::Bvs => branches += 1,
            O::Lda | O::Ldx | O::Ldy | O::Sta | O::Stx | O::Sty => loads_stores += 1,
            O::Adc | O::Sbc | O::Cmp | O::Cpx | O::Cpy | O::And | O::Ora | O::Eor | O::Bit => arithmetic += 1,
            O::Asl | O::Lsr | O::Rol | O::Ror | O::Inc | O::Dec | O::Inx | O::Iny | O::Dex | O::Dey => rmw += 1,
            O::Jmp | O::Jsr | O::Rts | O::Rti | O::Brk | O::Pha | O::Php | O::Pla | O::Plp => stack_control += 1,
            _ => {}
        }
    }

    let total: usize = mnemonics.values().sum();
    let mut top_mnemonics: Vec<_> = mnemonics.into_iter().collect();
    top_mnemonics.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    let mut top_modes: Vec<_> = modes.into_iter().collect();
    top_modes.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));

    println!("Static hot-op profile:");
    println!("  load/store: {loads_stores} ({:.1}%)", loads_stores as f64 * 100.0 / total.max(1) as f64);
    println!("  branch: {branches} ({:.1}%)", branches as f64 * 100.0 / total.max(1) as f64);
    println!("  ALU/compare/logic: {arithmetic} ({:.1}%)", arithmetic as f64 * 100.0 / total.max(1) as f64);
    println!("  shift/inc/dec: {rmw} ({:.1}%)", rmw as f64 * 100.0 / total.max(1) as f64);
    println!("  stack/control: {stack_control} ({:.1}%)", stack_control as f64 * 100.0 / total.max(1) as f64);
    println!("  indexed addressing: {indexed} ({:.1}%)", indexed as f64 * 100.0 / total.max(1) as f64);

    print!("  top mnemonics:");
    for (name, count) in top_mnemonics.into_iter().take(10) {
        print!(" {name}={count}");
    }
    println!();

    print!("  top addressing modes:");
    for (name, count) in top_modes.into_iter().take(8) {
        print!(" {name}={count}");
    }
    println!();
}


// PocketNES menu-maker compatibility records key ROMs by CRC32 of the payload
// after the 16-byte iNES header. Keep this tiny table factual and let the
// runtime's generic acquisition heuristic handle everything else.
fn crc32_ieee(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &byte in data {
        crc ^= byte as u32;
        for _ in 0..8 {
            let mask = 0u32.wrapping_sub(crc & 1);
            crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
        }
    }
    !crc
}

fn pocketnes_follow_slot(crc: u32) -> Option<u8> {
    match crc {
        // PocketNES database: Balloon Fight (E)/(JU) -> OAM slot 8.
        // 401349A8 is the later USA payload CRC and uses the same player slot.
        0xE541_38A9 | 0x2B46_2010 | 0x4013_49A8 => Some(8),
        // PocketNES database: Donkey Kong (JU) / Donkey Kong Classics (U).
        0x6F97_C721 | 0x703E_1948 => Some(0),
        // PocketNES database: Donkey Kong Jr. (JU).
        0x4864_C304 => Some(8),
        _ => None,
    }
}

fn emit_follow_hint_init(asm: &mut String, follow_slot: Option<u8>) {
    asm.push_str("\n; PocketNES-derived initial follow-camera hint\n");
    asm.push_str("SECTION \"Generated follow-camera metadata\", ROM0\n");
    asm.push_str("nes_generated_follow_init:\n");
    if let Some(slot) = follow_slot {
        asm.push_str(&format!("    ld a, ${slot:02X}\n"));
        asm.push_str("    ld [nes_view_follow_slot], a\n");
        asm.push_str("    ld a, $01\n");
        asm.push_str("    ld [nes_view_follow_valid], a\n");
    }
    asm.push_str("    ret\n");
}

fn main() -> ExitCode {
    let mut args = env::args_os();
    let program = args.next().unwrap_or_default();
    let Some(path) = args.next() else {
        eprintln!("usage: {} <rom.nes> [--emit-asm output.asm] [--max-blocks N] [--debug-trace]", PathBuf::from(program).display());
        return ExitCode::from(2);
    };

    let mut emit_asm: Option<PathBuf> = None;
    let mut max_blocks: Option<usize> = None;
    let mut debug_trace = false;

    let rest: Vec<_> = args.collect();
    let mut i = 0;
    while i < rest.len() {
        match rest[i].to_string_lossy().as_ref() {
            "--emit-asm" => {
                i += 1;
                if i >= rest.len() {
                    eprintln!("error: --emit-asm requires a path");
                    return ExitCode::from(2);
                }
                emit_asm = Some(PathBuf::from(&rest[i]));
            }
            "--max-blocks" => {
                i += 1;
                if i >= rest.len() {
                    eprintln!("error: --max-blocks requires a number");
                    return ExitCode::from(2);
                }
                match rest[i].to_string_lossy().parse::<usize>() {
                    Ok(n) if n > 0 => max_blocks = Some(n),
                    _ => {
                        eprintln!("error: --max-blocks must be a positive integer");
                        return ExitCode::from(2);
                    }
                }
            }
            "--debug-trace" => {
                debug_trace = true;
            }
            other => {
                eprintln!("error: unknown argument {other}");
                return ExitCode::from(2);
            }
        }
        i += 1;
    }

    let path = PathBuf::from(path);
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(err) => {
            eprintln!("error reading {}: {err}", path.display());
            return ExitCode::FAILURE;
        }
    };

    let cart = match ines::parse(&bytes) {
        Ok(cart) => cart,
        Err(err) => {
            eprintln!("error parsing {}: {err}", path.display());
            return ExitCode::FAILURE;
        }
    };

    let payload_crc = crc32_ieee(&bytes[16..]);
    let follow_slot = pocketnes_follow_slot(payload_crc);

    println!("ROM: {}", path.display());
    println!("Header: {:?}", cart.format);
    println!("Mapper: {}", cart.mapper);
    if cart.submapper != 0 {
        println!("Submapper: {}", cart.submapper);
    }
    println!("PRG ROM: {} KiB", cart.prg_rom.len() / 1024);
    println!("CHR ROM: {} KiB", cart.chr_rom.len() / 1024);
    println!("Mirroring: {:?}", cart.mirroring);
    println!("Battery: {}", if cart.battery { "yes" } else { "no" });
    println!("Trainer: {}", if cart.trainer.is_some() { "yes" } else { "no" });

    println!("PocketNES payload CRC32: {payload_crc:08X}");
    match follow_slot {
        Some(slot) => println!("PocketNES follow hint: OAM slot {slot}"),
        None => println!("PocketNES follow hint: none; using generic acquisition"),
    }

    let Some(vectors) = cpu6502::vectors_from_prg(cart.prg_rom) else {
        eprintln!("error: PRG ROM is too small to contain 6502 vectors");
        return ExitCode::FAILURE;
    };

    println!("NMI vector:   ${:04X}", vectors.nmi);
    println!("RESET vector: ${:04X}", vectors.reset);
    println!("IRQ vector:   ${:04X}", vectors.irq_brk);

    let graph = match cfg::discover_from_vectors(cart.mapper, cart.prg_rom, vectors) {
        Ok(graph) => graph,
        Err(err) => {
            eprintln!("CFG discovery stopped: {err}");
            return ExitCode::FAILURE;
        }
    };

    let instruction_count: usize = graph.blocks.values().map(|block| block.instructions.len()).sum();
    let indirect_jumps = graph.blocks.values().flat_map(|block| &block.edges)
        .filter(|edge| matches!(edge.kind, cfg::EdgeKind::IndirectJump { .. }) && edge.target.is_none()).count();
    let resolved_indirect_targets = graph.blocks.values().flat_map(|block| &block.edges)
        .filter(|edge| matches!(edge.kind, cfg::EdgeKind::IndirectJump { .. }) && edge.target.is_some()).count();

    println!("CFG blocks: {}", graph.blocks.len());
    println!("CFG instructions: {instruction_count}");
    println!("Unresolved indirect jumps: {indirect_jumps}");
    println!("Resolved indirect jump targets: {resolved_indirect_targets}");
    println!("Analysis diagnostics: {}", graph.diagnostics.len());
    print_hot_profile(&graph);

    for diagnostic in graph.diagnostics.iter().take(8) {
        println!("  ${:04X}: {}", diagnostic.pc, diagnostic.error);
    }

    if let Some(out_path) = emit_asm {
        let parent = out_path.parent().unwrap_or_else(|| std::path::Path::new("."));
        let stem = out_path.file_stem().and_then(|s| s.to_str()).unwrap_or("generated");
        let prg_name = format!("{stem}.prg.bin");
        let chr_name = format!("{stem}.chr.bin");
        let chr_gbc_name = format!("{stem}.chr.gbc.bin");
        let prg_path = parent.join(&prg_name);
        let chr_path = parent.join(&chr_name);
        let chr_gbc_path = parent.join(&chr_gbc_name);

        let mut asm = recompile::emit_cfg(
            &graph,
            recompile::EmitOptions { reset: vectors.reset, max_blocks, debug_trace },
        );
        asm.push_str("\n");
        asm.push_str(&recompile::emit_runtime_config(&recompile::RuntimeConfig {
            mapper: cart.mapper,
            mirroring: cart.mirroring,
            prg_len: cart.prg_rom.len(),
            chr_len: cart.chr_rom.len(),
            nmi: vectors.nmi,
            irq: vectors.irq_brk,
            prg_file: &prg_name,
            chr_file: &chr_name,
            chr_gbc_file: &chr_gbc_name,
        }));

        emit_follow_hint_init(&mut asm, follow_slot);

        if let Err(err) = fs::write(&out_path, asm) {
            eprintln!("error writing {}: {err}", out_path.display());
            return ExitCode::FAILURE;
        }
        if let Err(err) = fs::write(&prg_path, cart.prg_rom) {
            eprintln!("error writing {}: {err}", prg_path.display());
            return ExitCode::FAILURE;
        }
        if let Err(err) = fs::write(&chr_path, cart.chr_rom) {
            eprintln!("error writing {}: {err}", chr_path.display());
            return ExitCode::FAILURE;
        }
        let converted_chr = assets::convert_chr_to_gbc(cart.chr_rom);
        if let Err(err) = fs::write(&chr_gbc_path, converted_chr) {
            eprintln!("error writing {}: {err}", chr_gbc_path.display());
            return ExitCode::FAILURE;
        }

        println!("Generated LR35902 assembly: {}", out_path.display());
        println!("Embedded PRG data: {}", prg_path.display());
        println!("Embedded CHR data: {}", chr_path.display());
        println!("Converted GBC tile data: {}", chr_gbc_path.display());
    }

    ExitCode::SUCCESS
}

#[cfg(test)]
mod follow_hint_tests {
    use super::*;

    #[test]
    fn crc32_matches_standard_vector() {
        assert_eq!(crc32_ieee(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn known_pocketnes_follow_slots_are_seeded() {
        assert_eq!(pocketnes_follow_slot(0x4013_49A8), Some(8));
        assert_eq!(pocketnes_follow_slot(0x703E_1948), Some(0));
        assert_eq!(pocketnes_follow_slot(0x4864_C304), Some(8));
        assert_eq!(pocketnes_follow_slot(0), None);
    }
}
