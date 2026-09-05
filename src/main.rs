use std::{env, fs, path::PathBuf, process::ExitCode};

use nes2gbc::{cfg, cpu6502, ines, recompile};

fn main() -> ExitCode {
    let mut args = env::args_os();
    let program = args.next().unwrap_or_default();
    let Some(path) = args.next() else {
        eprintln!("usage: {} <rom.nes> [--emit-asm output.asm] [--max-blocks N]", PathBuf::from(program).display());
        return ExitCode::from(2);
    };

    let mut emit_asm: Option<PathBuf> = None;
    let mut max_blocks: Option<usize> = None;

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

    let graph = match cfg::discover_from_vectors(cart.mapper, cart.prg_rom, vectors) {
        Ok(graph) => graph,
        Err(err) => {
            eprintln!("CFG discovery stopped: {err}");
            return ExitCode::FAILURE;
        }
    };

    let instruction_count: usize = graph.blocks.values().map(|block| block.instructions.len()).sum();
    let indirect_jumps = graph.blocks.values().flat_map(|block| &block.edges)
        .filter(|edge| matches!(edge.kind, cfg::EdgeKind::IndirectJump { .. })).count();

    println!("CFG blocks: {}", graph.blocks.len());
    println!("CFG instructions: {instruction_count}");
    println!("Unresolved indirect jumps: {indirect_jumps}");
    println!("Analysis diagnostics: {}", graph.diagnostics.len());

    for diagnostic in graph.diagnostics.iter().take(8) {
        println!("  ${:04X}: {}", diagnostic.pc, diagnostic.error);
    }

    if let Some(out_path) = emit_asm {
        let asm = recompile::emit_cfg(
            &graph,
            recompile::EmitOptions { reset: vectors.reset, max_blocks },
        );
        if let Err(err) = fs::write(&out_path, asm) {
            eprintln!("error writing {}: {err}", out_path.display());
            return ExitCode::FAILURE;
        }
        println!("Generated LR35902 assembly: {}", out_path.display());
    }

    ExitCode::SUCCESS
}
