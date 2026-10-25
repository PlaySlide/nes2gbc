use std::collections::BTreeSet;
use std::{collections::BTreeMap, env, fs, path::PathBuf, process::ExitCode};

use nes2gbc::{assets, banked, cfg, cpu6502, idle, ines, overlay, recompile, superblock};

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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FollowHint {
    /// Follow NES OAM sprite number n (PocketNES sprite follow).
    Sprite(u8),
    /// Follow the screen-Y byte at NES RAM $aaaa (PocketNES FOLLOWMEM).
    Mem(u16),
    /// Fixed crop (view_x, view_y) while the HUD/playfield split is active.
    /// For games whose player is background-drawn or whose OAM slots rotate,
    /// where no sprite is a valid follow target (data: mode `C xxyy`).
    Fixed(u8, u8),
}

const POCKETNES_FOLLOW_DB: &str = include_str!("../data/pocketnes_follow.txt");

fn pocketnes_follow_hint(crc: u32) -> Option<FollowHint> {
    // Later payload revisions of PocketNES-listed games, same player slot.
    match crc {
        // Balloon Fight (USA, later payload) and Donkey Kong Classics (U).
        0x4013_49A8 => return Some(FollowHint::Sprite(8)),
        0x703E_1948 => return Some(FollowHint::Sprite(0)),
        _ => {}
    }
    for line in POCKETNES_FOLLOW_DB.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut f = line.split_whitespace();
        let (Some(c), Some(m), Some(v)) = (f.next(), f.next(), f.next()) else { continue };
        if u32::from_str_radix(c, 16).ok() != Some(crc) {
            continue;
        }
        let v = u16::from_str_radix(v, 16).ok()?;
        if m == "C" && env::var_os("NES2GBC_NO_FIXED_CAMERA").is_some() {
            continue;
        }
        return match m {
            "C" => Some(FollowHint::Fixed((v >> 8) as u8, v as u8)),
            "S" => Some(FollowHint::Sprite(v as u8)),
            "M" => Some(FollowHint::Mem(v)),
            _ => None,
        };
    }
    None
}

const ADDR_SCROLL_DB: &str = include_str!("../data/addr_scroll.txt");

/// Games that set the playfield scroll mid-frame with a PPUADDR ($2006) pair
/// after the sprite-0 hit (loopy t->v copy), e.g. Zelda room transitions.
/// Data-driven: `<payload crc32> <hud height in NES lines (hex)>` in data/addr_scroll.txt.
fn addr_scroll_hint(crc: u32) -> Option<u8> {
    if env::var_os("NES2GBC_NO_ADDR_SCROLL").is_some() {
        return None;
    }
    for l in ADDR_SCROLL_DB.lines() {
        let l = l.split('#').next().unwrap_or("").trim();
        let mut f = l.split_whitespace();
        let (Some(c), Some(h)) = (f.next(), f.next()) else { continue };
        if u32::from_str_radix(c, 16).ok() == Some(crc) {
            return u8::from_str_radix(h, 16).ok();
        }
    }
    None
}

const HUD_BOTTOM_DB: &str = include_str!("../data/hud_bottom.txt");

/// Games whose HUD is the *bottom* band of the screen (playfield first, HUD
/// after a sprite-0 split), e.g. Excitebike. The last scroll pair written in an
/// NMI is the HUD, the pair before it the playfield. Data-driven:
/// `<payload crc32> <HUD top NES line (hex)>` in data/hud_bottom.txt.
fn hud_bottom_hint(crc: u32) -> Option<u8> {
    if env::var_os("NES2GBC_NO_HUD_BOTTOM").is_some() {
        return None;
    }
    for l in HUD_BOTTOM_DB.lines() {
        let l = l.split('#').next().unwrap_or("").trim();
        let mut f = l.split_whitespace();
        let (Some(c), Some(h)) = (f.next(), f.next()) else { continue };
        if u32::from_str_radix(c, 16).ok() == Some(crc) {
            return u8::from_str_radix(h, 16).ok();
        }
    }
    None
}

const MAPPER_OVERRIDE_DB: &str = include_str!("../data/mapper_override.txt");

/// Data-driven header mapper override: `<payload crc32> <mapper> [submapper]`
/// per line in data/mapper_override.txt. NES2GBC_NO_MAPPER_OVERRIDE=1 disables
/// it; NES2GBC_MAPPER=<n> forces a mapper (debug).
fn mapper_override(crc: u32) -> Option<(u16, u8)> {
    if let Some(m) = env::var("NES2GBC_MAPPER").ok().and_then(|v| v.parse::<u16>().ok()) {
        return Some((m, 0));
    }
    if env::var_os("NES2GBC_NO_MAPPER_OVERRIDE").is_some() {
        return None;
    }
    MAPPER_OVERRIDE_DB.lines().find_map(|l| {
        let l = l.split('#').next().unwrap_or("").trim();
        let mut it = l.split_whitespace();
        let c = u32::from_str_radix(it.next()?, 16).ok()?;
        let m = it.next()?.parse::<u16>().ok()?;
        let sub = it.next().and_then(|v| v.parse::<u8>().ok()).unwrap_or(0);
        (c == crc).then_some((m, sub))
    })
}

const ZAPPER_DB: &str = include_str!("../data/zapper.txt");

/// Zapper (light gun) games: A button = trigger on port 2 ($4017 bit4) and the
/// light sensor (bit3) reports "light" for a few frames after a shot, so a
/// shot hits whatever the game flashes. Data-driven: payload CRC32 per line in
/// data/zapper.txt. NES2GBC_ZAPPER=1 forces it on, NES2GBC_NO_ZAPPER=1 off.
fn zapper_hint(crc: u32) -> bool {
    if env::var_os("NES2GBC_NO_ZAPPER").is_some() {
        return false;
    }
    if env::var("NES2GBC_ZAPPER").map(|v| v == "1").unwrap_or(false) {
        return true;
    }
    ZAPPER_DB.lines().any(|l| {
        let l = l.split('#').next().unwrap_or("").trim();
        u32::from_str_radix(l, 16).ok() == Some(crc)
    })
}

fn emit_follow_hint_init(asm: &mut String, hint: Option<FollowHint>) {
    asm.push_str("\n; PocketNES-derived follow-camera hint\n");
    asm.push_str("SECTION \"Generated follow-camera metadata\", ROM0\n");
    if matches!(hint, Some(FollowHint::Fixed(..))) {
        asm.push_str("; NES2GBC_FIXED_CAMERA 1\n");
    }
    asm.push_str("nes_generated_follow_init:\n");
    // nes_view_follow_hint_mode: 0 = heuristic, 1 = sprite number, 2 = RAM Y.
    let (mode, value) = match hint {
        None => (0u8, 0u16),
        Some(FollowHint::Sprite(n)) => (1, n as u16),
        Some(FollowHint::Mem(a)) => (2, a & 0x07FF),
        Some(FollowHint::Fixed(x, y)) => (3, ((y as u16) << 8) | x as u16),
    };
    asm.push_str(&format!("    ld a, ${mode:02X}\n"));
    asm.push_str("    ld [nes_view_follow_hint_mode], a\n");
    asm.push_str(&format!("    ld a, ${:02X}\n", value & 0xFF));
    asm.push_str("    ld [nes_view_follow_hint_lo], a\n");
    let hi = if mode == 3 { value >> 8 } else { (value >> 8) | 0xC0 };
    asm.push_str(&format!("    ld a, ${hi:02X}\n"));
    asm.push_str("    ld [nes_view_follow_hint_hi], a\n");
    if let Some(FollowHint::Sprite(slot)) = hint {
        asm.push_str(&format!("    ld a, ${slot:02X}\n"));
        asm.push_str("    ld [nes_view_follow_slot], a\n");
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

    let mut cart = match ines::parse(&bytes) {
        Ok(cart) => cart,
        Err(err) => {
            eprintln!("error parsing {}: {err}", path.display());
            return ExitCode::FAILURE;
        }
    };

    let payload_crc = crc32_ieee(&bytes[16..]);
    if let Some((m, sub)) = mapper_override(payload_crc) {
        if m != cart.mapper || sub != cart.submapper {
            println!("Mapper override: header {} sub {} -> {} sub {}", cart.mapper, cart.submapper, m, sub);
            cart.mapper = m;
            cart.submapper = sub;
        }
    }
    let follow_hint = pocketnes_follow_hint(payload_crc);
    let zapper = zapper_hint(payload_crc);
    nes2gbc::lr35902::ZAPPER.store(zapper, std::sync::atomic::Ordering::Relaxed);

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
    match follow_hint {
        Some(FollowHint::Sprite(slot)) => println!("PocketNES follow hint: OAM sprite {slot}"),
        Some(FollowHint::Mem(a)) => println!("PocketNES follow hint: RAM ${a:04X} (screen Y)"),
        Some(FollowHint::Fixed(x, y)) => println!("Fixed camera hint: view ({x}, {y}) during HUD split"),
        None => println!("PocketNES follow hint: none; using generic acquisition"),
    }

    let Some(vectors) = cpu6502::vectors_from_prg(cart.prg_rom) else {
        eprintln!("error: PRG ROM is too small to contain 6502 vectors");
        return ExitCode::FAILURE;
    };

    println!("NMI vector:   ${:04X}", vectors.nmi);
    println!("RESET vector: ${:04X}", vectors.reset);
    println!("IRQ vector:   ${:04X}", vectors.irq_brk);

    let mut switch_lo_views: Vec<(u8, cfg::ControlFlowGraph)> = Vec::new();
    let graph = if cart.mapper == 1 || cart.mapper == 2 {
        if cart.prg_rom.len() < 0x8000 || cart.prg_rom.len() % 0x4000 != 0 {
            eprintln!(
                "CFG discovery stopped: mapper 2 expects a whole number of 16 KiB PRG banks (at least two)"
            );
            return ExitCode::FAILURE;
        }
        let bank_count = cart.prg_rom.len() / 0x4000;
        if bank_count > 0x100 {
            eprintln!("CFG discovery stopped: mapper 2 has too many PRG banks ({bank_count})");
            return ExitCode::FAILURE;
        }
        let fixed = &cart.prg_rom[(bank_count - 1) * 0x4000..bank_count * 0x4000];
        let mut prg_views = Vec::with_capacity(bank_count);
        for bank in 0..bank_count {
            let mut view = Vec::with_capacity(0x8000);
            view.extend_from_slice(&cart.prg_rom[bank * 0x4000..(bank + 1) * 0x4000]);
            view.extend_from_slice(fixed);
            prg_views.push(view);
        }

        // Converge per-bank CFGs without assuming that every low-window
        // address is code in every physical bank (that explodes on data and
        // overflows the translated-code banks on real 128 KiB games):
        //
        // * $C000-$FFFF is the same fixed bank in every view, so any fixed
        //   block proven reachable in one view is explored in all views. This
        //   exposes fixed-bank trampolines (`LDA #n / STA $C000 / JSR $8xxx`)
        //   that are only reachable from some other bank's code.
        // * A low-window target is seeded into a *specific* other bank only
        //   when the transferring block selects that bank with a constant
        //   UxROM write immediately before the JMP/JSR.
        cfg::BANKED_VIEWS.store(true, std::sync::atomic::Ordering::Relaxed);
        let prg_bank_mask = (bank_count.next_power_of_two() - 1) as u8;
        let mut shared_fixed_entries = BTreeSet::<u16>::new();
        let mut bank_seeds: Vec<BTreeSet<u16>> = vec![BTreeSet::new(); bank_count];
        let mut converged_views = Vec::new();
        let mut rounds = 0usize;
        let mut overlay_seeded = false;
        loop {
            rounds += 1;
            converged_views.clear();
            let mut next_fixed = shared_fixed_entries.clone();
            let mut next_seeds = bank_seeds.clone();

            for (bank, view) in prg_views.iter().enumerate() {
                let mut entries = vec![vectors.reset, vectors.nmi, vectors.irq_brk];
                entries.extend(shared_fixed_entries.iter().copied());
                entries.extend(bank_seeds[bank].iter().copied());
                let g = match cfg::discover(0, view, &entries) {
                    Ok(graph) => graph,
                    Err(err) => {
                        eprintln!(
                            "CFG discovery stopped in mapper-{} PRG bank {bank}: {err}",
                            cart.mapper
                        );
                        return ExitCode::FAILURE;
                    }
                };
                for (&pc, block) in &g.blocks {
                    if pc >= 0xC000 && !block.instructions.is_empty() {
                        next_fixed.insert(pc);
                    }
                }
                if cart.mapper == 2 {
                    for (target_bank, target) in banked::constant_bank_switch_targets(&g, prg_bank_mask) {
                        if (target_bank as usize) < bank_count {
                            next_seeds[target_bank as usize].insert(target);
                        }
                    }
                }
                converged_views.push((bank as u8, g));
            }

            let changed = next_fixed != shared_fixed_entries || next_seeds != bank_seeds;
            shared_fixed_entries = next_fixed;
            bank_seeds = next_seeds;
            if !changed || rounds >= 16 {
                // Code copied into PRG RAM may call $8000-$BFFF routines that
                // every bank duplicates byte-for-byte (Zelda's MMC1 bank
                // switch at $BFAC/$BF98). Seed those once into bank 0; the
                // emitter shares that translation with the identical banks.
                if !overlay_seeded && env::var_os("NES2GBC_NO_RAM_OVERLAY").is_none() {
                    overlay_seeded = true;
                    let mut added = false;
                    for t in overlay::shared_low_targets(&converged_views, cart.prg_rom) {
                        added |= bank_seeds[0].insert(t);
                    }
                    if added {
                        continue;
                    }
                }
                break;
            }
        }
        let shared_low_entries: usize = bank_seeds.iter().map(|s| s.len()).sum();

        switch_lo_views = converged_views;
        println!(
            "Mapper {} mode-3 PRG views analyzed: {bank_count}; banked CFG converged in {rounds} round(s) with {} shared fixed entrie(s) and {} bank-switch seed(s)",
            cart.mapper,
            shared_fixed_entries.len(),
            shared_low_entries
        );
        switch_lo_views[0].1.clone()
    } else {
        match cfg::discover_from_vectors(cart.mapper, cart.prg_rom, vectors) {
            Ok(graph) => graph,
            Err(err) => {
                eprintln!("CFG discovery stopped: {err}");
                return ExitCode::FAILURE;
            }
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

        let emit_options = recompile::EmitOptions {
            reset: vectors.reset,
            max_blocks,
            debug_trace,
        };
        recompile::set_layout(cart.mapper as u16, cart.prg_rom.len(), cart.chr_rom.len());
        let ram_overlay = if (cart.mapper == 1 || cart.mapper == 2)
            && env::var_os("NES2GBC_NO_RAM_OVERLAY").is_none()
        {
            overlay::detect(&switch_lo_views, cart.prg_rom)
        } else {
            None
        };
        let mut asm = if cart.mapper == 1 || cart.mapper == 2 {
            banked::emit_mapper2_cfgs(&switch_lo_views, emit_options, ram_overlay.as_ref(), cart.prg_rom)
        } else {
            superblock::emit_cfg_with_interrupts(
                &graph,
                emit_options,
                vectors.nmi,
                vectors.irq_brk,
            )
        };
        asm.push_str("\n");
        asm.push_str(&recompile::emit_runtime_config(&recompile::RuntimeConfig {
            mapper: cart.mapper,
            submapper: cart.submapper,
            mirroring: cart.mirroring,
            prg_len: cart.prg_rom.len(),
            chr_len: cart.chr_rom.len(),
            nmi: vectors.nmi,
            irq: vectors.irq_brk,
            prg_file: &prg_name,
            chr_file: &chr_name,
            chr_gbc_file: &chr_gbc_name,
        }));

        emit_follow_hint_init(&mut asm, follow_hint);
        if let Some(n) = addr_scroll_hint(payload_crc) {
            asm.push_str(&format!("\n; NES2GBC_ADDR_SCROLL {n}\n"));
        }
        if let Some(n) = hud_bottom_hint(payload_crc) {
            asm.push_str(&format!("\n; NES2GBC_HUD_BOTTOM {n}\n"));
        }
        if zapper {
            asm.push_str("\n; NES2GBC_ZAPPER 1\n");
        }

        // Sidecar 6502 CFG dump (blocks, instructions, typed edges) for the
        // interprocedural asm passes (tools/rts_return_sets.py).
        let mut cfg_txt = String::new();
        cfg_txt.push_str(&format!("V {:04X} {:04X} {:04X}\n", vectors.reset, vectors.nmi, vectors.irq_brk));
        for block in graph.blocks.values() {
            cfg_txt.push_str(&format!("B {:04X}\n", block.start));
            for ins in &block.instructions {
                cfg_txt.push_str(&format!("I {:04X} {:?} {:?}\n", ins.pc, ins.def.mnemonic, ins.def.mode));
            }
            for edge in &block.edges {
                let kind = match edge.kind {
                    cfg::EdgeKind::Fallthrough => "fall",
                    cfg::EdgeKind::BranchTaken => "branch",
                    cfg::EdgeKind::Jump => "jump",
                    cfg::EdgeKind::Call => "call",
                    cfg::EdgeKind::CallReturn => "ret",
                    cfg::EdgeKind::IndirectJump { .. } => "ind",
                };
                match edge.target {
                    Some(t) => cfg_txt.push_str(&format!("E {kind} {t:04X}\n")),
                    None => cfg_txt.push_str(&format!("E {kind} -\n")),
                }
            }
        }
        // Banked builds: every PRG view (switchable bank + fixed bank) and the
        // PRG-RAM overlay, so return-set analysis sees callers from all banks.
        if !switch_lo_views.is_empty() {
            let mut views_txt = String::new();
            views_txt.push_str(&format!("V {:04X} {:04X} {:04X}\n", vectors.reset, vectors.nmi, vectors.irq_brk));
            views_txt.push_str(&format!("M {}\n", cart.mapper));
            let mut dump = |tag: String, blocks: &mut dyn Iterator<Item = &cfg::BasicBlock>| {
                views_txt.push_str(&tag);
                for block in blocks {
                    views_txt.push_str(&format!("B {:04X}\n", block.start));
                    for ins in &block.instructions {
                        let operand = match ins.def.mode {
                            cpu6502::AddressingMode::Absolute | cpu6502::AddressingMode::AbsoluteX
                            | cpu6502::AddressingMode::AbsoluteY | cpu6502::AddressingMode::Indirect =>
                                format!(" {:04X}", ins.operand),
                            _ => String::new(),
                        };
                        views_txt.push_str(&format!("I {:04X} {:?} {:?}{operand}\n", ins.pc, ins.def.mnemonic, ins.def.mode));
                    }
                    for edge in &block.edges {
                        let kind = match edge.kind {
                            cfg::EdgeKind::Fallthrough => "fall",
                            cfg::EdgeKind::BranchTaken => "branch",
                            cfg::EdgeKind::Jump => "jump",
                            cfg::EdgeKind::Call => "call",
                            cfg::EdgeKind::CallReturn => "ret",
                            cfg::EdgeKind::IndirectJump { .. } => "ind",
                        };
                        match edge.target {
                            Some(t) => views_txt.push_str(&format!("E {kind} {t:04X}\n")),
                            None => views_txt.push_str(&format!("E {kind} -\n")),
                        }
                    }
                }
            };
            for (bank, g) in &switch_lo_views {
                dump(format!("W {bank:02X}\n"), &mut g.blocks.values());
            }
            if let Some(ov) = ram_overlay.as_ref() {
                dump("W RAM\n".to_string(), &mut ov.blocks.values());
            }
            // Overlay image (expected PRG-RAM contents), so return-set analysis
            // can decode RAM continuations that have no block.
            if let Some(ov) = ram_overlay.as_ref() {
                let hex: String = ov.bytes.iter().map(|b| format!("{b:02X}")).collect();
                views_txt.push_str(&format!("O {:04X} {hex}\n", ov.dst));
            }
            let views_path = parent.join(format!("{stem}.cfgviews.txt"));
            if let Err(err) = fs::write(&views_path, views_txt) {
                eprintln!("error writing {}: {err}", views_path.display());
                return ExitCode::FAILURE;
            }
        }
        // Provable idle spins (src/idle.rs) as asm labels for the opt-out
        // NES2GBC_HALT_IDLE post-pass (tools/halt_idle_loops.py).
        let mut idle_labels: BTreeSet<String> = BTreeSet::new();
        if switch_lo_views.is_empty() {
            for pc in idle::idle_spin_heads(&graph) {
                idle_labels.insert(format!("nes_{pc:04X}"));
            }
        } else {
            for (bank, g) in &switch_lo_views {
                for pc in idle::idle_spin_heads(g) {
                    idle_labels.insert(if pc >= 0xC000 { format!("nes_{pc:04X}") } else { format!("nes_m2_b{bank:02X}_{pc:04X}") });
                }
            }
        }
        let idle_txt: String = idle_labels.iter().map(|l| format!("{l}\n")).collect();
        let idle_path = parent.join(format!("{stem}.idle.txt"));
        if let Err(err) = fs::write(&idle_path, idle_txt) {
            eprintln!("error writing {}: {err}", idle_path.display());
            return ExitCode::FAILURE;
        }
        let cfg_path = parent.join(format!("{stem}.cfg.txt"));
        if let Err(err) = fs::write(&cfg_path, cfg_txt) {
            eprintln!("error writing {}: {err}", cfg_path.display());
            return ExitCode::FAILURE;
        }

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

        // Banked mappers can execute code their reset code copies into RAM
        // (Zelda runs $6C90-$7EFF from PRG RAM). Enable the runtime 6502
        // interpreter for PCs below $8000 there; mapper 0/3 builds stay
        // byte-identical. Idempotent so repeated direct runs don't redefine.
        if cart.mapper == 1 || cart.mapper == 2 {
            let config_path = parent.join("generated_config.inc");
            let line = "DEF NES2GBC_RAM_INTERP EQU 1 ; banked mapper: interpret RAM-resident code";
            let ovl_line = "DEF NES2GBC_RAM_OVERLAY EQU 1 ; translated PRG-RAM code overlay";
            let existing = fs::read_to_string(&config_path).unwrap_or_default();
            let mut text: String = existing
                .lines()
                .filter(|l| {
                    !l.starts_with("DEF NES2GBC_RAM_OVERLAY ")
                        && !l.starts_with("DEF NES2GBC_DISPATCH_ADD ")
                        && !l.starts_with("DEF NES2GBC_CHR4K ")
                })
                .map(|l| format!("{l}\n"))
                .collect();
            let mut changed = text != existing;
            if !text.lines().any(|l| l.starts_with("DEF NES2GBC_RAM_INTERP ")) {
                text.push_str(line);
                text.push('\n');
                changed = true;
            }
            if recompile::dispatch_bank_start() != 32 {
                // Dispatch tables moved above a large cartridge's PRG/CHR banks.
                text.push_str(&format!(
                    "DEF NES2GBC_DISPATCH_ADD EQU {} ; dispatch-table bank = high nibble + this\n",
                    recompile::dispatch_bank_start() - 8
                ));
                changed = true;
            }
            if cart.mapper == 1
                && !cart.chr_rom.is_empty()
                && !std::env::var("NES2GBC_NO_CHR4K").map(|v| v == "1").unwrap_or(false)
            {
                // MMC1 split 4 KiB CHR-ROM banking (opt out: NES2GBC_NO_CHR4K=1).
                text.push_str("DEF NES2GBC_CHR4K EQU 1 ; MMC1 split 4 KiB CHR-ROM banking\n");
                changed = true;
            }
            if ram_overlay.is_some() {
                text.push_str(ovl_line);
                text.push('\n');
                changed = true;
            }
            if changed {
                if let Err(err) = fs::write(&config_path, text) {
                    eprintln!("error writing {}: {err}", config_path.display());
                    return ExitCode::FAILURE;
                }
            }
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
    fn known_pocketnes_follow_hints_are_seeded() {
        assert_eq!(addr_scroll_hint(0xEAF7_ED72), Some(0x40));
        assert_eq!(addr_scroll_hint(0xD5C6_4257), None);
        assert_eq!(hud_bottom_hint(0x49AE_B3A6), Some(0xC7));
        assert_eq!(hud_bottom_hint(0xEAF7_ED72), None);
        assert_eq!(pocketnes_follow_hint(0x4013_49A8), Some(FollowHint::Sprite(8)));
        assert_eq!(pocketnes_follow_hint(0x703E_1948), Some(FollowHint::Sprite(0)));
        assert_eq!(pocketnes_follow_hint(0x4864_C304), Some(FollowHint::Sprite(8)));
        assert_eq!(pocketnes_follow_hint(0xFB98_D46E), Some(FollowHint::Sprite(0)));
        assert_eq!(pocketnes_follow_hint(0xD445_F698), Some(FollowHint::Sprite(8)));
        assert_eq!(pocketnes_follow_hint(0xD5C6_4257), Some(FollowHint::Fixed(0x30, 0x47)));
        assert_eq!(pocketnes_follow_hint(0x6EE4_BB0A), Some(FollowHint::Mem(0x0600)));
        assert_eq!(pocketnes_follow_hint(0xF603_5030), Some(FollowHint::Mem(0x031A)));
        assert_eq!(pocketnes_follow_hint(0xB668_C7FC), Some(FollowHint::Mem(0x01F5)));
        assert_eq!(pocketnes_follow_hint(0xEAF7_ED72), Some(FollowHint::Sprite(18)));
        assert_eq!(pocketnes_follow_hint(0), None);
    }
}
