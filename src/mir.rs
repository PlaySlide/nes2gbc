//! Machine-level IR for the LR35902 backend (stage 1 of docs/IR_REWRITE.md).
//!
//! Every translator path (recompile.rs, state_superblock.rs, lr35902.rs)
//! now emits into a [`MirBuf`] instead of a bare `String`. `MirBuf` keeps the
//! exact text each emitter produced (so the printed program is
//! byte-identical to the pre-IR output) and, line by line, a structured
//! [`Item`]: instructions with an explicit def/use set over GB registers,
//! flags, memory and the HRAM NES-state slots, labels, and directives.
//! [`MirBuf::blocks`] recovers the block/edge structure.
//!
//! Stage 2 builds on this: global liveness over [`Block`] edges, carrying
//! X/Y in B/C across edges with HRAM-reload stubs at dynamic entries, and
//! per-helper clobber tables ([`Inst::call_target`]). Until then the
//! structured view is analysis-only; nothing is rewritten from it.
use std::fmt;

/// One storage location an instruction can read or write.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Loc {
    A, B, C, D, E, H, L,
    FlagZ, FlagN, FlagH, FlagC,
    Sp,
    /// Any non-HRAM-slot memory (WRAM, VRAM, I/O, ROM bank registers).
    Mem,
    /// HRAM NES state slots (see runtime/io.asm).
    NesA, NesX, NesY, NesSp, NesP, ZShadow, NShadow, CShadow,
    /// `nes_host_vblank_pending` (NMI poll flag).
    VblankPending,
}

/// Bit set over [`Loc`].
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct LocSet(pub u32);

impl LocSet {
    pub const EMPTY: LocSet = LocSet(0);
    pub const ALL: LocSet = LocSet(u32::MAX);
    pub const fn of(l: Loc) -> LocSet { LocSet(1 << l as u8) }
    pub const fn flags() -> LocSet {
        LocSet((1 << Loc::FlagZ as u8) | (1 << Loc::FlagN as u8) | (1 << Loc::FlagH as u8) | (1 << Loc::FlagC as u8))
    }
    pub fn contains(self, l: Loc) -> bool { self.0 & (1 << l as u8) != 0 }
    pub fn insert(&mut self, l: Loc) { self.0 |= 1 << l as u8 }
    pub fn union(self, o: LocSet) -> LocSet { LocSet(self.0 | o.0) }
    pub fn is_empty(self) -> bool { self.0 == 0 }
}

/// A decoded LR35902 instruction with its effects.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Inst {
    pub mnemonic: String,
    pub operands: Vec<String>,
    pub uses: LocSet,
    pub defs: LocSet,
    /// Control transfer: Some(target label/expression) for jp/jr/call/rst.
    pub target: Option<String>,
    pub conditional: bool,
    /// Ends straight-line flow (unconditional jp/jr/ret/reti, jp hl).
    pub terminator: bool,
}

impl Inst {
    /// Runtime helper called by this instruction, if any (stage 2 looks
    /// its clobber set up by this name).
    pub fn call_target(&self) -> Option<&str> {
        if self.mnemonic == "call" { self.target.as_deref() } else { None }
    }
}

/// Structured view of one emitted line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Item {
    Blank,
    Comment,
    /// `name:` (global), `.name:` / `a.b:` (local), `:` (anonymous).
    Label { name: String, global: bool },
    Inst(Inst),
    /// Assembler directives and macros (SECTION, IF/ELSE/ENDC, db/dw, DEF,
    /// PROFILE_INC, ...). Treated as opaque: unknown effects.
    Directive(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Line {
    pub raw: String,
    pub item: Item,
}

/// How a translated NES block is entered at a given label.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryKind {
    /// Canonical entry: NES state is in HRAM (dispatch tables, polls,
    /// RTS returns, cross-section jumps).
    Canonical,
    /// `_trace` continuation of the previous block: the emitter's resident
    /// registers (A/X/Y in A/B/C) are live on entry.
    Trace,
    /// Overlay entry that skips the PRG-RAM byte check (`nes_r_XXXX_nc`).
    Unchecked,
}

/// Emitter-supplied identity of a NES block label.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BlockMeta {
    pub pc: u16,
    /// Switchable PRG bank for banked views; None for fixed/NROM code.
    pub bank: Option<u16>,
    pub kind: EntryKind,
    /// The block carries an NMI safe-point poll.
    pub poll: bool,
}

/// A straight-line region between global labels, with its successors.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block {
    pub label: Option<String>,
    /// Line index range in [`MirBuf::lines`].
    pub start: usize,
    pub end: usize,
    /// Explicit control-flow targets (labels) referenced from the block.
    pub succs: Vec<String>,
    /// True when the last instruction can fall through to the next block.
    pub falls_through: bool,
    /// NES block identity, when the label was emitted with [`MirBuf::nes_label`].
    pub meta: Option<BlockMeta>,
}

/// Emission buffer: exact text plus parsed structure.
#[derive(Debug, Default, Clone)]
pub struct MirBuf {
    text: String,
    lines: Vec<Line>,
    /// Byte offset in `text` where the next unparsed line starts.
    parsed_to: usize,
    /// Start offset of each parsed line.
    starts: Vec<usize>,
    built: usize,
    /// (line index, meta) for NES block labels.
    metas: Vec<(usize, BlockMeta)>,
}

impl MirBuf {
    pub fn new() -> Self { Self::default() }
    pub fn len(&self) -> usize { self.text.len() }
    pub fn is_empty(&self) -> bool { self.text.is_empty() }
    pub fn as_str(&self) -> &str { &self.text }
    pub fn push_str(&mut self, s: &str) {
        self.text.push_str(s);
        self.parse_new();
    }
    pub fn into_string(self) -> String { self.text }

    /// Typed instruction builder: construct the MIR instruction and render
    /// its text (`    mn op1, op2[ ; comment]`). Emitters use this instead of
    /// formatting assembly text; the rendered line is what the pre-IR
    /// `writeln!` produced, so output stays byte-identical.
    pub fn ins(&mut self, mn: &str, ops: &[&str], comment: Option<&str>) {
        let mut line = String::with_capacity(40);
        line.push_str("    ");
        line.push_str(mn);
        for (k, op) in ops.iter().enumerate() {
            line.push_str(if k == 0 { " " } else { ", " });
            line.push_str(op);
        }
        if let Some(c) = comment {
            line.push_str(" ; ");
            line.push_str(c);
        }
        if self.parsed_to != self.text.len() {
            // Mid-line (a caller is still assembling text): stay textual.
            line.push('\n');
            self.push_str(&line);
            return;
        }
        let owned: Vec<String> = ops.iter().map(|o| o.to_string()).collect();
        let item = match decode(mn, &owned) {
            Some(i) => Item::Inst(i),
            None => parse_line(&line),
        };
        let start = self.text.len();
        self.text.push_str(&line);
        self.text.push('\n');
        self.parsed_to = self.text.len();
        self.starts.push(start);
        self.lines.push(Line { raw: line, item });
        self.built += 1;
    }

    /// Label builder (`name:`; `:` for an anonymous label).
    pub fn label(&mut self, name: &str) {
        let mut line = String::with_capacity(name.len() + 2);
        line.push_str(name);
        line.push(':');
        line.push('\n');
        self.push_str(&line);
    }

    /// Label of a translated NES block, recording its identity for
    /// [`MirBuf::blocks`].
    pub fn nes_label(&mut self, name: &str, meta: BlockMeta) {
        self.label(name);
        let idx = self.lines.len() - 1;
        self.metas.push((idx, meta));
    }

    pub fn block_metas(&self) -> &[(usize, BlockMeta)] { &self.metas }

    /// Append another buffer, keeping its constructed items and metadata.
    pub fn append(&mut self, other: MirBuf) {
        if self.parsed_to != self.text.len() || other.parsed_to != other.text.len() {
            self.push_str(&other.text);
            return;
        }
        let base_off = self.text.len();
        let base_line = self.lines.len();
        self.text.push_str(&other.text);
        self.parsed_to = self.text.len();
        self.starts.extend(other.starts.iter().map(|s| s + base_off));
        self.lines.extend(other.lines);
        self.metas.extend(other.metas.into_iter().map(|(i, m)| (i + base_line, m)));
        self.built += other.built;
    }

    /// Copy of this buffer without line `idx` (all lines must be complete).
    pub fn without_line(&self, idx: usize) -> MirBuf {
        assert_eq!(self.parsed_to, self.text.len());
        let mut out = MirBuf::new();
        for (i, line) in self.lines.iter().enumerate() {
            if i == idx { continue; }
            let start = out.text.len();
            out.text.push_str(&line.raw);
            out.text.push('\n');
            out.starts.push(start);
            out.lines.push(line.clone());
            if let Some((_, m)) = self.metas.iter().find(|(k, _)| *k == i) {
                out.metas.push((out.lines.len() - 1, *m));
            }
        }
        out.parsed_to = out.text.len();
        out.built = self.built;
        out
    }

    /// Lines constructed through [`MirBuf::ins`] (vs parsed from text).
    pub fn built_count(&self) -> usize { self.built }
    /// Drop everything from byte offset `n` on (like `String::truncate`).
    pub fn truncate(&mut self, n: usize) {
        self.text.truncate(n);
        while let (Some(&st), Some(l)) = (self.starts.last(), self.lines.last()) {
            if st + l.raw.len() + 1 <= n { break; }
            self.starts.pop();
            self.lines.pop();
        }
        let keep = self.lines.len();
        self.metas.retain(|(i, _)| *i < keep);
        self.parsed_to = match (self.starts.last(), self.lines.last()) {
            (Some(&st), Some(l)) => st + l.raw.len() + 1,
            _ => 0,
        };
        self.parse_new();
    }
    /// Completed lines (a trailing line without '\n' is not included).
    pub fn lines(&self) -> &[Line] { &self.lines }
    pub fn insts(&self) -> impl Iterator<Item = &Inst> {
        self.lines.iter().filter_map(|l| if let Item::Inst(i) = &l.item { Some(i) } else { None })
    }

    fn parse_new(&mut self) {
        while let Some(off) = self.text[self.parsed_to..].find('\n') {
            let end = self.parsed_to + off;
            let raw = self.text[self.parsed_to..end].to_string();
            self.parsed_to = end + 1;
            let item = parse_line(&raw);
            self.starts.push(end - raw.len());
            self.lines.push(Line { raw, item });
        }
    }

    /// Split into blocks at global labels.
    pub fn blocks(&self) -> Vec<Block> {
        let mut out: Vec<Block> = Vec::new();
        let mut cur = Block { label: None, start: 0, end: 0, succs: Vec::new(), falls_through: true, meta: None };
        let meta_at = |i: usize| self.metas.iter().find(|(k, _)| *k == i).map(|(_, m)| *m);
        for (i, line) in self.lines.iter().enumerate() {
            match &line.item {
                Item::Label { name, global: true } => {
                    cur.end = i;
                    if cur.end > cur.start || cur.label.is_some() {
                        out.push(cur);
                    }
                    cur = Block { label: Some(name.clone()), start: i, end: i, succs: Vec::new(), falls_through: true, meta: meta_at(i) };
                }
                Item::Inst(inst) => {
                    if let Some(t) = &inst.target {
                        if inst.mnemonic != "call" && inst.mnemonic != "rst" && !cur.succs.contains(t) {
                            cur.succs.push(t.clone());
                        }
                    }
                    cur.falls_through = !inst.terminator;
                }
                _ => {}
            }
        }
        cur.end = self.lines.len();
        if cur.end > cur.start || cur.label.is_some() {
            out.push(cur);
        }
        out
    }
}

/// Stage-2 sizing report (NES2GBC_MIR_STATS=1): for every global block,
/// count the HRAM NES-register reloads (`ldh a, [nes_a|x|y|sp]`) that run
/// before the block's first write of the same slot. These are the reloads
/// stage 2 removes when a predecessor keeps the value in a GB register.
pub fn report_entry_reloads(tag: &str, m: &MirBuf) {
    if std::env::var("NES2GBC_MIR_STATS").map(|v| v == "0").unwrap_or(true) {
        return;
    }
    let slots = [Loc::NesA, Loc::NesX, Loc::NesY, Loc::NesSp];
    let (mut blocks, mut reloads, mut insts) = (0usize, [0usize; 4], 0usize);
    for b in m.blocks() {
        if b.label.is_none() { continue; }
        blocks += 1;
        let mut written = LocSet::EMPTY;
        let mut seen = LocSet::EMPTY;
        for line in &m.lines()[b.start..b.end] {
            let Item::Inst(i) = &line.item else { continue };
            insts += 1;
            for (k, s) in slots.iter().enumerate() {
                if i.uses.contains(*s) && !written.contains(*s) && !seen.contains(*s) && i.uses != LocSet::ALL {
                    reloads[k] += 1;
                    seen.insert(*s);
                }
            }
            if i.defs != LocSet::ALL { written = written.union(i.defs); }
        }
    }
    let parsed: Vec<&str> = m.lines().iter().skip(0).filter(|l| matches!(l.item, Item::Inst(_))).map(|l| l.raw.as_str()).collect();
    eprintln!(
        "mir-stats[{tag}]: {blocks} blocks, {insts} insts ({} built, {} parsed from text); entry reloads A {} X {} Y {} SP {}",
        m.built_count(), parsed.len().saturating_sub(m.built_count()),
        reloads[0], reloads[1], reloads[2], reloads[3]
    );
}

impl fmt::Write for MirBuf {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        self.push_str(s);
        Ok(())
    }
}

impl std::ops::Deref for MirBuf {
    type Target = str;
    fn deref(&self) -> &str { &self.text }
}

impl fmt::Display for MirBuf {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str(&self.text) }
}

fn reg(name: &str) -> Option<Loc> {
    Some(match name {
        "a" => Loc::A, "b" => Loc::B, "c" => Loc::C, "d" => Loc::D,
        "e" => Loc::E, "h" => Loc::H, "l" => Loc::L, _ => return None,
    })
}

fn pair(name: &str) -> Option<LocSet> {
    let (x, y) = match name {
        "bc" => (Loc::B, Loc::C), "de" => (Loc::D, Loc::E), "hl" => (Loc::H, Loc::L),
        "af" => return Some(LocSet::of(Loc::A).union(LocSet::flags())),
        "sp" => return Some(LocSet::of(Loc::Sp)),
        _ => return None,
    };
    Some(LocSet::of(x).union(LocSet::of(y)))
}

fn hram_slot(addr: &str) -> Option<Loc> {
    Some(match addr {
        "nes_a" => Loc::NesA, "nes_x" => Loc::NesX, "nes_y" => Loc::NesY, "nes_sp" => Loc::NesSp,
        "nes_p" => Loc::NesP, "nes_z_shadow" => Loc::ZShadow, "nes_n_shadow" => Loc::NShadow,
        "nes_c_shadow" => Loc::CShadow, "nes_host_vblank_pending" => Loc::VblankPending,
        _ => return None,
    })
}

/// Effects of reading/writing operand `op`: (uses for address, location).
fn operand(op: &str) -> (LocSet, LocSet) {
    let op = op.trim();
    if let Some(r) = reg(op) {
        return (LocSet::EMPTY, LocSet::of(r));
    }
    if let Some(inner) = op.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
        let inner = inner.trim();
        match inner {
            "hl" | "hli" | "hld" | "hl+" | "hl-" => return (pair("hl").unwrap(), LocSet::of(Loc::Mem)),
            "bc" | "de" => return (pair(inner).unwrap(), LocSet::of(Loc::Mem)),
            "c" => return (LocSet::of(Loc::C), LocSet::of(Loc::Mem)),
            _ => {}
        }
        if let Some(s) = hram_slot(inner) {
            return (LocSet::EMPTY, LocSet::of(s));
        }
        return (LocSet::EMPTY, LocSet::of(Loc::Mem));
    }
    (LocSet::EMPTY, LocSet::EMPTY) // immediate / label
}

const CONDS: [&str; 4] = ["z", "nz", "c", "nc"];

fn cond_uses(c: &str) -> LocSet {
    match c {
        "z" | "nz" => LocSet::of(Loc::FlagZ),
        _ => LocSet::of(Loc::FlagC),
    }
}

pub fn parse_line(raw: &str) -> Item {
    let code = raw.split(';').next().unwrap_or("").trim();
    if code.is_empty() {
        return if raw.trim().is_empty() { Item::Blank } else { Item::Comment };
    }
    if let Some(name) = code.strip_suffix(':').or_else(|| code.strip_suffix("::")) {
        let name = name.trim_end_matches(':');
        if !name.contains(char::is_whitespace) {
            let global = !name.is_empty() && !name.starts_with('.') && !name.contains('.');
            return Item::Label { name: name.to_string(), global };
        }
    }
    let (mn, rest) = match code.split_once(char::is_whitespace) {
        Some((m, r)) => (m, r.trim()),
        None => (code, ""),
    };
    let ops: Vec<String> = if rest.is_empty() { Vec::new() } else { split_operands(rest) };
    match decode(mn, &ops) {
        Some(inst) => Item::Inst(inst),
        None => Item::Directive(code.to_string()),
    }
}

fn split_operands(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut depth = 0i32;
    let mut cur = String::new();
    for ch in s.chars() {
        match ch {
            '[' | '(' => { depth += 1; cur.push(ch) }
            ']' | ')' => { depth -= 1; cur.push(ch) }
            ',' if depth == 0 => { out.push(cur.trim().to_string()); cur.clear() }
            _ => cur.push(ch),
        }
    }
    if !cur.trim().is_empty() { out.push(cur.trim().to_string()); }
    out
}

fn decode(mn: &str, ops: &[String]) -> Option<Inst> {
    let f = LocSet::flags();
    let mut i = Inst {
        mnemonic: mn.to_string(), operands: ops.to_vec(), uses: LocSet::EMPTY, defs: LocSet::EMPTY,
        target: None, conditional: false, terminator: false,
    };
    let o = |k: usize| ops.get(k).map(String::as_str).unwrap_or("");
    let a = LocSet::of(Loc::A);
    match (mn, ops.len()) {
        ("ld", 2) | ("ldh", 2) | ("ldi", 2) | ("ldd", 2) => {
            let (d, s) = (o(0), o(1));
            if d == "hl" && s.starts_with("sp") {
                i.uses = LocSet::of(Loc::Sp); i.defs = pair("hl").unwrap().union(f);
                return Some(i);
            }
            if let Some(p) = pair(d) {
                let (su, sl) = if let Some(q) = pair(s) { (LocSet::EMPTY, q) } else { operand(s) };
                i.uses = su.union(sl); i.defs = p;
                return Some(i);
            }
            let (du, dl) = operand(d);
            let (su, sl) = operand(s);
            i.uses = du.union(su).union(sl);
            i.defs = dl;
            let inc_hl = |x: &str| matches!(x, "[hli]" | "[hld]" | "[hl+]" | "[hl-]") || mn != "ld" && mn != "ldh";
            if inc_hl(d) || inc_hl(s) { i.defs = i.defs.union(pair("hl").unwrap()); }
            Some(i)
        }
        ("add" | "adc" | "sub" | "sbc" | "and" | "or" | "xor" | "cp", 1 | 2) => {
            if mn == "add" && ops.len() == 2 && (o(0) == "hl" || o(0) == "sp") {
                let p = pair(o(0)).unwrap();
                let q = pair(o(1)).unwrap_or(LocSet::EMPTY);
                i.uses = p.union(q); i.defs = p.union(f);
                return Some(i);
            }
            let src = if ops.len() == 2 { o(1) } else { o(0) };
            let (su, sl) = operand(src);
            i.uses = a.union(su).union(sl);
            if mn == "adc" || mn == "sbc" { i.uses.insert(Loc::FlagC); }
            i.defs = if mn == "cp" { f } else { a.union(f) };
            Some(i)
        }
        ("inc" | "dec", 1) => {
            if let Some(p) = pair(o(0)) { i.uses = p; i.defs = p; return Some(i); }
            let (u, l) = operand(o(0));
            i.uses = u.union(l);
            let mut fl = f; fl.0 &= !LocSet::of(Loc::FlagC).0;
            i.defs = l.union(fl);
            Some(i)
        }
        ("rlca" | "rrca" | "rla" | "rra", 0) => {
            i.uses = a; if mn == "rla" || mn == "rra" { i.uses.insert(Loc::FlagC); }
            i.defs = a.union(f); Some(i)
        }
        ("rlc" | "rrc" | "rl" | "rr" | "sla" | "sra" | "srl" | "swap", 1) => {
            let (u, l) = operand(o(0));
            i.uses = u.union(l); if mn == "rl" || mn == "rr" { i.uses.insert(Loc::FlagC); }
            i.defs = l.union(f); Some(i)
        }
        ("bit", 2) => {
            let (u, l) = operand(o(1));
            i.uses = u.union(l);
            let mut fl = f; fl.0 &= !LocSet::of(Loc::FlagC).0;
            i.defs = fl; Some(i)
        }
        ("res" | "set", 2) => { let (u, l) = operand(o(1)); i.uses = u.union(l); i.defs = l; Some(i) }
        ("push", 1) => { i.uses = pair(o(0))?.union(LocSet::of(Loc::Sp)); i.defs = LocSet::of(Loc::Sp).union(LocSet::of(Loc::Mem)); Some(i) }
        ("pop", 1) => { i.uses = LocSet::of(Loc::Sp).union(LocSet::of(Loc::Mem)); i.defs = pair(o(0))?.union(LocSet::of(Loc::Sp)); Some(i) }
        ("jp" | "jr" | "call", 1 | 2) => {
            if ops.len() == 2 {
                if !CONDS.contains(&o(0)) { return None; }
                i.conditional = true; i.uses = cond_uses(o(0)); i.target = Some(o(1).to_string());
            } else if o(0) == "hl" || o(0) == "[hl]" {
                i.uses = pair("hl").unwrap(); i.terminator = true;
                return Some(i);
            } else {
                i.target = Some(o(0).to_string());
                i.terminator = mn != "call";
            }
            if mn == "call" { i.uses = LocSet::ALL; i.defs = LocSet::ALL; }
            Some(i)
        }
        ("ret", 0 | 1) | ("reti", 0) => {
            if ops.len() == 1 { if !CONDS.contains(&o(0)) { return None; } i.conditional = true; i.uses = cond_uses(o(0)); }
            else { i.terminator = true; }
            i.uses = i.uses.union(LocSet::ALL);
            Some(i)
        }
        ("rst", 1) => { i.target = Some(o(0).to_string()); i.uses = LocSet::ALL; i.defs = LocSet::ALL; Some(i) }
        ("scf" | "ccf", 0) => { if mn == "ccf" { i.uses = LocSet::of(Loc::FlagC); } i.defs = f; Some(i) }
        ("cpl", 0) => { i.uses = a; i.defs = a.union(f); Some(i) }
        ("daa", 0) => { i.uses = a.union(f); i.defs = a.union(f); Some(i) }
        ("nop" | "di" | "ei" | "halt" | "stop", 0) => Some(i),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write;

    #[test]
    fn text_round_trips_exactly() {
        let mut m = MirBuf::new();
        write!(m, "nes_8000:\n    ldh a, [nes_x] ; seed\n    ld b").unwrap();
        writeln!(m, ", a").unwrap();
        m.push_str("IF DEF(X)\n:\n    jr nz, :+\n    jp nes_8010\n.l:\n");
        assert_eq!(m.as_str(), "nes_8000:\n    ldh a, [nes_x] ; seed\n    ld b, a\nIF DEF(X)\n:\n    jr nz, :+\n    jp nes_8010\n.l:\n");
        assert_eq!(m.lines().len(), 8);
        assert_eq!(m.len(), m.as_str().len());
        let cut = m.as_str().find("    jr").unwrap() + 6;
        m.truncate(cut);
        assert_eq!(m.lines().len(), 5);
        m.push_str(" z, :-\n");
        assert_eq!(m.lines().len(), 6);
        assert_eq!(m.lines()[5].raw, "    jr z, :-");
    }

    #[test]
    fn builder_matches_text_parse() {
        let cases: &[(&str, &[&str], Option<&str>)] = &[
            ("ldh", &["a", "[nes_x]"], Some("superblock X cache seed")),
            ("ld", &["b", "a"], None),
            ("jr", &["nz", ":+"], None),
            ("rr", &["[hl]"], Some("native RMW shift/rotate")),
            ("call", &["nes_poll_nmi_hl"], None),
            ("ret", &[], None),
        ];
        for (mn, ops, c) in cases {
            let mut a = MirBuf::new();
            a.ins(mn, ops, *c);
            let mut b = MirBuf::new();
            b.push_str(a.as_str());
            assert_eq!(a.as_str(), b.as_str());
            assert_eq!(a.lines(), b.lines(), "{mn}");
            assert_eq!(a.built_count(), 1);
        }
    }

    #[test]
    fn def_use_and_blocks() {
        let mut m = MirBuf::new();
        m.push_str("nes_8000:\n    ldh a, [nes_x]\n    ld b, a\n    add $10\n    ldh [nes_z_shadow], a\n    jr nz, nes_8000\n    jp nes_9000\nnes_8010:\n    ret\n");
        let insts: Vec<&Inst> = m.insts().collect();
        assert!(insts[0].uses.contains(Loc::NesX) && insts[0].defs.contains(Loc::A));
        assert!(insts[1].uses.contains(Loc::A) && insts[1].defs.contains(Loc::B));
        assert!(insts[2].defs.contains(Loc::FlagC) && insts[2].defs.contains(Loc::A));
        assert!(insts[3].defs.contains(Loc::ZShadow));
        assert!(insts[4].conditional && insts[4].uses.contains(Loc::FlagZ));
        let b = m.blocks();
        assert_eq!(b.len(), 2);
        let mut n = MirBuf::new();
        n.nes_label("nes_8000", BlockMeta { pc: 0x8000, bank: None, kind: EntryKind::Canonical, poll: true });
        n.ins("ret", &[], None);
        assert_eq!(n.as_str(), "nes_8000:\n    ret\n");
        assert_eq!(n.blocks()[0].meta.unwrap().pc, 0x8000);
        assert_eq!(b[0].label.as_deref(), Some("nes_8000"));
        assert_eq!(b[0].succs, vec!["nes_8000".to_string(), "nes_9000".to_string()]);
        assert!(!b[0].falls_through);
    }
}
