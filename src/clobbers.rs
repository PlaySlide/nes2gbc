//! Per-runtime-helper clobber tables (stage 2 of docs/IR_REWRITE.md).
//!
//! The runtime helpers the translator calls (`nes_cpu_read`,
//! `nes_ppu_cpu_write`, `nes_stack_push_return_hl`, ...) are hand-written
//! RGBDS in `runtime/*.asm`. Instead of a hand-maintained table, the
//! translator analyses those sources (embedded at compile time) and computes,
//! for every helper, the set of GB registers it may leave modified when it
//! returns. The analysis is a conservative path walk:
//!
//! * fall-through, `jr`/`jp` to local and global labels, conditional `ret`
//!   continue the walk; `call` folds in the callee's set (memoised,
//!   recursion = everything);
//! * `IF`/`ELIF`/`ELSE`/`ENDC` explore every arm;
//! * `push rr` ... `pop rr` around a write restores the pair; an unbalanced
//!   stack at `ret`, SP arithmetic, `jp hl`, `rst`, macros, `REPT`/`FOR`,
//!   `SECTION` fall-through, data or anything else not understood clobbers
//!   everything.
//!
//! Reported effects are the GB registers A B C D E H L and direct writes of
//! the HRAM NES slots `nes_a`/`nes_x`/`nes_y` (other memory is not modelled;
//! callers keep treating helper calls as reading NES state).
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::OnceLock;

use crate::mir::{parse_line, Item, Loc, LocSet};

const SOURCES: &[&str] = &[
    include_str!("../runtime/io.asm"),
    include_str!("../runtime/apu.asm"),
    include_str!("../runtime/profile.asm"),
    include_str!("../runtime/cpu.asm"),
    include_str!("../runtime/ppu.asm"),
    include_str!("../runtime/video.asm"),
    include_str!("../runtime/input.asm"),
    include_str!("../runtime/interp.asm"),
    include_str!("../runtime/main.asm"),
];

const REGS: [Loc; 7] = [Loc::A, Loc::B, Loc::C, Loc::D, Loc::E, Loc::H, Loc::L];

/// Tracked effects: GB registers plus the HRAM NES X/Y/A slots (a helper
/// that writes `nes_x` invalidates a resident copy in B).
fn regs_all() -> LocSet {
    REGS.iter().fold(LocSet::EMPTY, |s, &r| s.union(LocSet::of(r)))
        .union(LocSet::of(Loc::NesX)).union(LocSet::of(Loc::NesY)).union(LocSet::of(Loc::NesA))
}

fn regs_of(s: LocSet) -> LocSet {
    LocSet(s.0 & regs_all().0)
}

struct Runtime {
    /// (code, item) per line of every source, concatenated; files separated by
    /// a synthetic SECTION barrier so nothing falls through between files.
    lines: Vec<(String, Item)>,
    globals: HashMap<String, usize>,
    /// Local label `.x` resolved per enclosing global: (global, local) -> line.
    locals: HashMap<(String, String), usize>,
    /// Enclosing global label per line.
    scope: Vec<String>,
    /// For IF/ELIF/ELSE lines: start lines of every arm (IF only) and ENDC line.
    arms: HashMap<usize, Vec<usize>>,
    endc: HashMap<usize, usize>,
    /// MACRO name -> first body line.
    macros: HashMap<String, usize>,
}

fn directive_word(code: &str) -> String {
    code.split_whitespace().next().unwrap_or("").to_ascii_uppercase()
}

impl Runtime {
    fn load() -> Runtime {
        let mut lines = Vec::new();
        for src in SOURCES {
            for raw in src.lines() {
                let code = raw.split(';').next().unwrap_or("").trim().to_string();
                let item = parse_line(raw);
                lines.push((code, item));
            }
            lines.push(("SECTION \"<file end>\"".to_string(), Item::Directive("SECTION".into())));
        }
        let mut globals = HashMap::new();
        let mut locals = HashMap::new();
        let mut scope = Vec::with_capacity(lines.len());
        let mut cur = String::new();
        for (i, (_, item)) in lines.iter().enumerate() {
            if let Item::Label { name, global } = item {
                if *global {
                    cur = name.clone();
                    globals.entry(name.clone()).or_insert(i);
                } else if let Some((g, l)) = name.split_once('.') {
                    let g = if g.is_empty() { cur.clone() } else { g.to_string() };
                    locals.entry((g, format!(".{l}"))).or_insert(i);
                }
            }
            scope.push(cur.clone());
        }
        // Conditional-assembly structure.
        let mut arms: HashMap<usize, Vec<usize>> = HashMap::new();
        let mut endc = HashMap::new();
        let mut stack: Vec<(usize, Vec<usize>)> = Vec::new();
        for (i, (code, item)) in lines.iter().enumerate() {
            if !matches!(item, Item::Directive(_)) { continue; }
            match directive_word(code).as_str() {
                "IF" => stack.push((i, vec![i])),
                "ELIF" | "ELSE" => { if let Some(top) = stack.last_mut() { top.1.push(i); } }
                "ENDC" => {
                    if let Some((_, members)) = stack.pop() {
                        let starts: Vec<usize> = members.iter().map(|m| m + 1).chain(std::iter::once(i + 1)).collect();
                        arms.insert(members[0], starts);
                        for m in members { endc.insert(m, i); }
                    }
                }
                _ => {}
            }
        }
        let mut macros = HashMap::new();
        for (i, (code, item)) in lines.iter().enumerate() {
            if matches!(item, Item::Directive(_)) && directive_word(code) == "MACRO" {
                if let Some(n) = code.split_whitespace().nth(1) { macros.insert(n.to_string(), i + 1); }
            }
        }
        Runtime { lines, globals, locals, scope, arms, endc, macros }
    }

    fn resolve(&self, at: usize, target: &str) -> Option<usize> {
        if target.starts_with('.') {
            return self.locals.get(&(self.scope[at].clone(), target.to_string())).copied();
        }
        if let Some((g, l)) = target.split_once('.') {
            return self.locals.get(&(g.to_string(), format!(".{l}"))).copied();
        }
        self.globals.get(target).copied()
    }
}

struct Analyzer<'a> {
    rt: &'a Runtime,
    memo: HashMap<String, LocSet>,
    active: HashSet<String>,
}

impl<'a> Analyzer<'a> {
    fn helper(&mut self, name: &str) -> LocSet {
        if let Some(&s) = self.memo.get(name) { return s; }
        if !self.active.insert(name.to_string()) {
            if std::env::var("NES2GBC_CLOBBER_DEBUG").is_ok() { eprintln!("clobber-all: recursion into {name}"); }
            return regs_all();
        }
        let res = match self.rt.globals.get(name) {
            Some(&start) => self.walk(start),
            None => regs_all(),
        };
        self.active.remove(name);
        self.memo.insert(name.to_string(), res);
        res
    }

    /// Registers possibly modified on any path from `start` to a return.
    fn walk(&mut self, start: usize) -> LocSet {
        let all = regs_all();
        let dbg = std::env::var("NES2GBC_CLOBBER_DEBUG").is_ok();
        macro_rules! bail { ($i:expr) => {{ if dbg { eprintln!("clobber-all at line {}: {:?}", $i, self.rt.lines.get($i).map(|l| &l.0)); } return all; }} }
        let mut clob = LocSet::EMPTY;
        let mut seen: HashSet<(usize, Vec<String>)> = HashSet::new();
        let mut work: Vec<(usize, Vec<String>)> = vec![(start, Vec::new())];
        while let Some((mut i, mut stack)) = work.pop() {
            loop {
                if seen.len() > 200_000 { bail!(i); }
                if !seen.insert((i, stack.clone())) { break; }
                let Some((code, item)) = self.rt.lines.get(i) else { bail!(i) };
                let saved = |stack: &Vec<String>, s: LocSet| -> LocSet {
                    let mut s = s;
                    for p in stack {
                        let ps = match p.as_str() {
                            "bc" => LocSet::of(Loc::B).union(LocSet::of(Loc::C)),
                            "de" => LocSet::of(Loc::D).union(LocSet::of(Loc::E)),
                            "hl" => LocSet::of(Loc::H).union(LocSet::of(Loc::L)),
                            "af" => LocSet::of(Loc::A),
                            _ => LocSet::EMPTY,
                        };
                        s.0 &= !ps.0;
                    }
                    s
                };
                match item {
                    Item::Blank | Item::Comment | Item::Label { .. } => { i += 1; continue; }
                    Item::Directive(_) => {
                        let w = directive_word(code);
                        match w.as_str() {
                            "IF" => {
                                for &s in self.rt.arms.get(&i).map(Vec::as_slice).unwrap_or(&[]) {
                                    work.push((s, stack.clone()));
                                }
                                break;
                            }
                            "ELIF" | "ELSE" => match self.rt.endc.get(&i) {
                                Some(&e) => { i = e + 1; continue; }
                                None => bail!(i),
                            },
                            "ENDM" => {
                                if !stack.is_empty() { bail!(i); }
                                break;
                            }
                            _ if self.rt.macros.contains_key(code.split_whitespace().next().unwrap_or("")) => {
                                let m = code.split_whitespace().next().unwrap_or("").to_string();
                                let key = format!("MACRO {m}");
                                let body = match self.memo.get(&key) {
                                    Some(&b) => b,
                                    None => {
                                        if !self.active.insert(key.clone()) { bail!(i); }
                                        let b = self.walk(self.rt.macros[&m]);
                                        self.active.remove(&key);
                                        self.memo.insert(key, b);
                                        b
                                    }
                                };
                                clob = clob.union(saved(&stack, body));
                                i += 1; continue;
                            }
                            "ENDC" | "REPT" | "FOR" | "ENDR" | "ASSERT" | "STATIC_ASSERT" | "DEF" | "EXPORT" | "PURGE" | "REDEF" | "WARN" | "PRINTLN" | "PRINT" => { i += 1; continue; }
                            _ => bail!(i),
                        }
                    }
                    Item::Inst(inst) => {
                        let mn = inst.mnemonic.as_str();
                        let op0 = inst.operands.first().map(String::as_str).unwrap_or("");
                        match mn {
                            "push" => { stack.push(op0.to_string()); i += 1; continue; }
                            "pop" => {
                                match stack.pop() {
                                    Some(p) if p == op0 => {}
                                    Some(_) => { clob = clob.union(saved(&stack, regs_of(inst.defs))); }
                                    None => bail!(i),
                                }
                                i += 1; continue;
                            }
                            _ => {}
                        }
                        if !matches!(mn, "call" | "ret" | "reti" | "rst" | "jp" | "jr") && (inst.defs.contains(Loc::Sp) || inst.uses == LocSet::ALL) {
                            bail!(i);
                        }
                        match mn {
                            "ret" | "reti" => {
                                if !stack.is_empty() { bail!(i); }
                                if inst.conditional { i += 1; continue; }
                                break;
                            }
                            "call" => {
                                let Some(t) = inst.target.clone() else { bail!(i) };
                                let callee = match self.rt.resolve(i, &t) {
                                    Some(ti) if self.rt.globals.get(&t) == Some(&ti) => self.helper(&t),
                                    Some(ti) => self.walk(ti),
                                    None => bail!(i),
                                };
                                clob = clob.union(saved(&stack, callee));
                                i += 1; continue;
                            }
                            "rst" => bail!(i),
                            "jp" | "jr" => {
                                let Some(t) = inst.target.clone() else { bail!(i) }; // jp hl
                                let Some(ti) = self.rt.resolve(i, &t) else {
                                    if t == ":+" || t == ":-" || t.starts_with(":") { bail!(i); }
                                    bail!(i);
                                };
                                work.push((ti, stack.clone()));
                                if inst.conditional { i += 1; continue; }
                                break;
                            }
                            _ => {
                                clob = clob.union(saved(&stack, regs_of(inst.defs)));
                                i += 1; continue;
                            }
                        }
                    }
                }
            }
        }
        clob
    }
}

fn table() -> &'static std::sync::Mutex<(Runtime, BTreeMap<String, LocSet>)> {
    static T: OnceLock<std::sync::Mutex<(Runtime, BTreeMap<String, LocSet>)>> = OnceLock::new();
    T.get_or_init(|| std::sync::Mutex::new((Runtime::load(), BTreeMap::new())))
}

/// GB registers (A B C D E H L) and NES A/X/Y HRAM slots helper `name` may modify. Unknown helpers and
/// anything the analysis does not understand clobber all of them.
/// Helpers that post-emission text passes (tools/hot_alu_generated.py) replace
/// with inline code that uses more scratch registers than the runtime routine.
/// Their clobber set is the union of both; `inline_table_matches_hot_alu`
/// keeps this list in sync with the pass.
pub const TEXT_INLINED: &[(&str, &str)] = &[
    ("nes_adc_a_e", "abcdh"),
    ("nes_sbc_a_e", "abcdh"),
    ("nes_bit_a_e", "ad"),
    ("nes_asl_a", "acd"),
    ("nes_lsr_a", "acd"),
    ("nes_rol_a", "acd"),
    ("nes_ror_a", "acd"),
];

fn text_inline_extra(name: &str) -> LocSet {
    let mut s = LocSet::EMPTY;
    for (h, regs) in TEXT_INLINED {
        if *h == name {
            for ch in regs.chars() {
                s = s.union(LocSet::of(match ch {
                    'a' => Loc::A, 'b' => Loc::B, 'c' => Loc::C, 'd' => Loc::D,
                    'e' => Loc::E, 'h' => Loc::H, _ => Loc::L,
                }));
            }
        }
    }
    s
}

pub fn helper_clobbers(name: &str) -> LocSet {
    helper_clobbers_runtime(name).union(text_inline_extra(name))
}

fn helper_clobbers_runtime(name: &str) -> LocSet {
    let mut g = table().lock().unwrap();
    if let Some(&s) = g.1.get(name) { return s; }
    let s = {
        let mut a = Analyzer { rt: &g.0, memo: HashMap::new(), active: HashSet::new() };
        a.helper(name)
    };
    g.1.insert(name.to_string(), s);
    s
}

/// True when `name` returns with B and C unchanged.
pub fn preserves(name: &str, r: Loc) -> bool {
    !helper_clobbers(name).contains(r)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_helpers() {
        // Uses BC as scratch: must be reported.
        assert!(helper_clobbers("nes_stack_push_return_hl").contains(Loc::B));
        assert!(helper_clobbers("nes_compare_a_e").contains(Loc::C));
        // push bc ... pop bc: preserved.
        assert!(!helper_clobbers("nes_profile_trace_pc").contains(Loc::B));
        assert!(helper_clobbers("no_such_helper") == regs_all());
    }

    #[test]
    fn inline_table_matches_hot_alu() {
        let src = include_str!("../tools/hot_alu_generated.py");
        let mut n = 0;
        for part in src.split("\"call ").skip(1) {
            let Some(end) = part.find("\": (") else { continue };
            let h = &part[..end];
            n += 1;
            assert!(TEXT_INLINED.iter().any(|(t, _)| *t == h), "{h} inlined by hot_alu but missing from TEXT_INLINED");
        }
        assert!(n >= 7);
        assert!(helper_clobbers("nes_asl_a").contains(Loc::C));
    }

    #[test]
    fn print_table() {
        for h in ["nes_cpu_read", "nes_cpu_read_hi", "nes_cpu_write", "nes_ppu_cpu_write", "nes_ppu_cpu_read",
            "nes_apu_write", "nes_stack_push_a", "nes_stack_pop_a", "nes_stack_push_return_hl",
            "nes_stack_pop_return_hl", "nes_poll_nmi_hl", "nes_adc_a_e", "nes_sbc_a_e", "nes_compare_a_e",
            "nes_bit_a_e", "nes_cpu_read_joy_hl", "nes_controller_write", "nes_jump_known_hl_a",
            "nes_jump_known_hl_a_8bit", "nes_profile_trace_pc", "nes_ror_a", "nes_oam_dma"] {
            let s = helper_clobbers(h);
            let names: Vec<&str> = [(Loc::A, "a"), (Loc::B, "b"), (Loc::C, "c"), (Loc::D, "d"), (Loc::E, "e"), (Loc::H, "h"), (Loc::L, "l")]
                .iter().filter(|(l, _)| s.contains(*l)).map(|(_, n)| *n).collect();
            eprintln!("{h}: {}", names.join(""));
        }
    }
}

/// Effects on (B, C/X, Y) of a lowered instruction sequence: returns whether
/// B and nes_x, resp. C and nes_y, are guaranteed unchanged on every path
/// through it. Calls use [`helper_clobbers`]; directives other than
/// conditional-assembly markers are opaque.
pub fn preserves_xy(m: &crate::mir::MirBuf) -> (bool, bool) {
    let (mut kb, mut kc) = (true, true);
    for line in m.lines() {
        let defs = match &line.item {
            Item::Inst(i) => match i.call_target() {
                Some(t) => helper_clobbers(t),
                None => i.defs,
            },
            Item::Directive(d) => {
                let w = d.split_whitespace().next().unwrap_or("").to_ascii_uppercase();
                if matches!(w.as_str(), "IF" | "ELIF" | "ELSE" | "ENDC") { continue; }
                LocSet::ALL
            }
            _ => continue,
        };
        if defs.contains(Loc::B) || defs.contains(Loc::NesX) { kb = false; }
        if defs.contains(Loc::C) || defs.contains(Loc::NesY) { kc = false; }
    }
    (kb, kc)
}
