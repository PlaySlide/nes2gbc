use std::{env, fs, path::PathBuf, process::ExitCode};

use nes2gbc::{cfg, cpu6502, ines};

fn main() -> ExitCode {
    let mut args = env::args_os();
    let program = args.next().unwrap_or_default();
    let Some(path) = args.next() else {
        eprintln!("usage: {} <rom.nes>", PathBuf::from(program).display());
        return ExitCode::from(2);
    };

    if args.next().is_some() {
        eprintln!("error: expected exactly one ROM path");
        return ExitCode::from(2);
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

    let Some(vectors) = cpu6502::vectors_from_prg(cart.prg_rom) else {
        eprintln!("error: PRG ROM is too small to contain 6502 vectors");
        return ExitCode::FAILURE;
    };

    println!("NMI vector:   ${:04X}", vectors.nmi);
    println!("RESET vector: ${:04X}", vectors.reset);
    println!("IRQ vector:   ${:04X}", vectors.irq_brk);

    match cfg::discover_from_vectors(cart.mapper, cart.prg_rom, vectors) {
        Ok(graph) => {
            let instruction_count: usize = graph
                .blocks
                .values()
                .map(|block| block.instructions.len())
                .sum();
            let indirect_jumps = graph
                .blocks
                .values()
                .flat_map(|block| &block.edges)
                .filter(|edge| matches!(edge.kind, cfg::EdgeKind::IndirectJump { .. }))
                .count();
            println!("CFG blocks: {0}", graph.blocks.len());
            println!("CFG instructions: {instruction_count}");
            println!("Unresolved indirect jumps: {indirect_jumps}");
            println!("Analysis diagnostics: {}", graph.diagnostics.len());
            for diagnostic in graph.diagnostics.iter().take(8) {
                println!("  ${:04X}: {}", diagnostic.pc, diagnostic.error);
            }
        }
        Err(err) => {
            eprintln!("CFG discovery stopped: {err}");
            return ExitCode::FAILURE;
        }
    }

    ExitCode::SUCCESS
}
