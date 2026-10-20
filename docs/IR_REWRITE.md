# Translator IR rewrite (branch `rewrite/ir-stage1`)

Goal: replace "emit fixed asm text, then repair it with ~100 regex post-passes"
with a machine-level IR that the Rust translator can analyse and optimise
across blocks, so A/X/Y can stay in GB registers over block edges (the HRAM
save/reload bucket is ~9.8% of executed cycles, flags ~6.6%; see
/tmp/rewrite_audit.md and the cycle breakdown).

Every stage must keep `runtime/generated.asm` and the final ROM
**byte-identical** until a stage deliberately changes codegen, proven with
`tools/check_byte_identical.sh` against a reference build of the 12 test
games plus opt-out combinations and the CI smoke ROMs.

## Code paths today

| Path | File | Used for |
|---|---|---|
| plain | `src/recompile.rs` | small/CI builds, `MAX_BLOCKS` |
| superblock (state-carrying) | `src/state_superblock.rs` | NROM/CNROM (`superblock` in main.rs is an alias of this module) |
| banked views | `src/banked.rs` (calls into state_superblock per block) | MMC1/UxROM/... |
| instruction templates | `src/lr35902.rs` (`emit_ops`) | per-6502-op lowering used by all of the above |

`src/superblock.rs` was a stale, never-compiled copy (lib.rs aliases
`superblock` to `state_superblock`); stage 1 deletes it.

## The IR (`src/mir.rs`)

* `Loc` / `LocSet`: storage locations: GB registers A B C D E H L, flags
  Z N H C, SP, generic memory, and the HRAM NES-state slots (`nes_a`,
  `nes_x`, `nes_y`, `nes_sp`, `nes_p`, `nes_z/n/c_shadow`,
  `nes_host_vblank_pending`).
* `Inst`: one LR35902 instruction with mnemonic, operands, explicit
  `uses`/`defs` sets, control target, conditional / terminator bits.
  `call`/`rst`/`ret` are conservatively ALL until stage 2 adds helper
  clobber tables (`Inst::call_target` is the lookup key).
* `Item`: `Inst`, `Label { global }` (RGBDS scoping: `.x` and `a.b` are local,
  `:` anonymous), `Directive` (SECTION, IF/ENDC, db, macros: opaque),
  `Comment`, `Blank`.
* `MirBuf`: the single emission buffer every path writes into.
  * **Typed builders** (stage 1c): `ins(mnemonic, &[operands], comment)`
    constructs the `Inst` (def/use from the decoder table) and renders its
    line; `label(name)`; `nes_label(name, BlockMeta)`. Emitters construct
    instructions through these; text is rendered from them. About 99% of
    instructions are built (SMB 84,052 built / 1,209 parsed; Zelda
    242,086 / 423). What still arrives as text and is parsed: directives
    (SECTION, IF/ELSE/ENDC, db/dw/ds, INCBIN), comments, the follow-hint
    routine in main.rs, and banked chained-entry adapters (strings joined
    with `jp X_trace`).
  * `append(MirBuf)` / `without_line` move built MIR between buffers
    (lr35902 `emit_ops_buf` output is appended, not re-parsed).
  * It still implements `fmt::Write`/`push_str`/`truncate` and derefs to
    `str` for the remaining text paths, parsing completed lines.
* `BlockMeta` (stage 1c): emitter-supplied identity of NES block labels:
  NES `pc`, PRG `bank` (banked views), `EntryKind` (`Canonical` = HRAM
  contract; `Trace` = `_trace` continuation with resident A/X/Y;
  `Unchecked` = overlay `nes_r_XXXX_nc`), and the `poll` flag.
  `MirBuf::blocks()` attaches it to each `Block`.
* `report_entry_reloads` (`NES2GBC_MIR_STATS=1`) prints, per path, block
  count, built vs parsed instructions, and block-entry HRAM reloads of
  A/X/Y/SP: the stage-2 opportunity.

### Shared logic

* `src/emit_common.rs` (stage 1a + 1d): bank constants,
  `estimated_block_size`, `emit_pc_dispatch`, `flag_mask`, `is_branch`,
  `terminal_mnemonic`, `select_reachable`, `assign_code_banks(max_bank)`
  (255 plain / 254 superblock), `emit_dispatch_tables`, `approx_code_bytes`,
  `emit_static_target`, `emit_known_target`,
  `emit_static_control(fast_a_target)` (`emit_fast_a_target` is only used by
  the plain path), `reachable_from_roots`, `nmi_exclusive_blocks`. These were
  copied between recompile.rs and state_superblock.rs and differed only in
  formatting/comments, plus those two parameters.
* `src/polls.rs`: NMI poll placement: `nmi_poll_points`,
  `cyclic_back_targets`, counted-loop budget rules; used by NROM and banked.
* banked.rs keeps its own `is_branch`/`terminal_mnemonic`/
  `emit_dispatch_tables` (different semantics: BRK, mapper dispatch layout).

## Stage plan

1. **Stage 1 (this branch)**: MIR types; all paths emit into `MirBuf`;
   typed builders; NES block metadata; dedupe shared helpers;
   byte-identical proof script. *Status: done (1a-1d).* Still textual
   (by design for now): directives/comments; remaining duplication is the two
   main per-block emission loops (`recompile::emit_cfg_with_interrupts_impl`
   vs `state_superblock`'s superblock planner/emitter), which really differ
   (A-residency fast entries vs X/Y/A trace residency) and are unified as part
   of stage 2 when both lower from one MIR block model.
   Former stage-1 items (all done):
   * construct `Inst`s directly (typed builders such as
     `m.ldh_load(Loc::A, Loc::NesX)`) instead of formatting text, starting
     with `state_superblock.rs` helpers (`ensure_x/ensure_y/materialize_*`,
     `emit_update_nz`) and `lr35902::emit_ops`, keeping text identical;
   * merge the remaining near-duplicates between recompile.rs and
     state_superblock.rs (`select_reachable`, `assign_code_banks`,
     `emit_dispatch_tables`, `emit_static_target/control`,
     `nmi_exclusive_blocks`, `reachable_from_roots`), which differ in small,
     path-specific details and need parameterising;
   * make `MirBuf::blocks` carry the NES block id and entry kind (dispatch
     entry, poll point, private trace edge) from the emitter instead of
     re-deriving from labels.
2. **Stage 2: cross-block register carrying.**
   * Per NES block, record the entry contract (which of A/X/Y are expected
     in A/B/C vs HRAM) as an attribute of the block, not as text.
   * Global liveness over MIR blocks (successors from `Block::succs` +
     CFG edges); HRAM slots are ordinary locations.
   * Every dynamic entry (dispatch table target, poll resume, RTS return)
     gets a small *reload stub* that loads HRAM into registers and falls into
     the register-contract entry; direct jumps from predecessors that hold
     the values jump past the stub. (The text-level attempt
     `skip_entry_reloads.py` failed because it couldn't see these contracts
     and widened JR to JP; the IR fixes both.)
   * Helper clobber tables: per runtime helper (nes_ppu_cpu_write.*,
     nes_cpu_read*, APU writes, nes_poll_nmi_hl...) the registers it
     preserves; start with B/C preserved by bus helpers.
   * Stage-2 gate: wcost + work.py/longplay on 12 games, fullchk; opt-out
     `NES2GBC_MIR_CARRY=0` must reproduce stage-1 output byte-identically.
3. **Stage 3: lazy flags in MIR** (Z/N as "last result register", C in host
   carry across blocks), retiring dead_hram_state_global / flag peephole
   passes as their work moves into MIR analyses.
4. **Stage 4: port high-value Python post-passes to MIR transforms** and
   delete the text pipeline stages they replace (each port proven
   byte-identical first, then optimised).

## Proof procedure

```
tools/check_byte_identical.sh ref   MANIFEST HASHES   # on the reference commit
tools/check_byte_identical.sh check MANIFEST HASHES   # on the candidate
```
MANIFEST lines: `NAME ROM [ENV=VAL ...] [-- MAKEVAR=VAL ...]`. The manifest
used for stage 1 (box-local ROM paths) covers Zelda, Contra, Mega Man,
Castlevania, SMB, DK, Ice Climber, Balloon Fight, Kung Fu, Ice Hockey,
Bomberman, Excitebike, DK Classics, opt-out combos (RMW_HL=0,
COUNTED_LOOP_POLLS=0, NROM_CYCLIC_POLLS=0, POLL_ALL_BACK+BANKED_POLL_ALL,
STATE_PARK_A=0+STATE_INDY=0) and the CI smoke ROMs (MAX_BLOCKS=16 NROM,
UxROM, cross-bank, large UxROM, MMC1), all with the default APU=1.

## Stage 2 status (branch `rewrite/ir-stage2`, from stage-1 tip 9b5e5df)

Stage 2 lives on its own branch so stage 1 stays a pure refactor.

**What it does (state_superblock path, i.e. mapper-0/NROM-style builds):**
NES X (in B) and, optionally, Y (in C) stay in GB registers across static
block edges. This covers taken/not-taken branches, JMP abs, and fallthrough
into another section head within the same bank.

- Each candidate block gets a register-contract entry `nes_T_trace`. It sits
  right after the canonical `nes_T:` label and an HRAM-reload stub.
- Every dynamic or indirect entry still lands on `nes_T`. That covers dispatch
  tables, RTS/RTI targets, the NMI entry, pushed-return continuations and
  bank thunks. Those entries go through the stub, which reloads B/C from
  `nes_x`/`nes_y`.
- Carried static predecessors jump straight to `nes_T_trace`.
- Contracts are refined by re-emission until a fixed point (at most 5 passes,
  monotone Dirty -> Clean -> None).
- Contracts are *clean* by default: HRAM stays authoritative at every 6502
  control transfer, because many text passes assume it. That is why
  NMI/poll paths need no extra save/restore: poll and interrupt helpers are
  known to clobber everything, and the handler reloads from HRAM.
- `NES2GBC_XY_DIRTY=1` allows dirty contracts. It is experimental and unsafe
  with native loop passes.
- Clean residency is also *kept across barriers* whose lowered MIR provably
  preserves B/nes_x (C/nes_y).
  - `src/clobbers.rs` derives the per-helper clobber table from `runtime/*.asm`.
  - It adds `TEXT_INLINED` for helpers that `hot_alu_generated.py` inlines
    with extra scratch registers.
  - A cargo test keeps that list in sync with the pass.

**Env:**

| Variable | Effect |
| --- | --- |
| `NES2GBC_XY_CARRY=0` | Byte-identical stage-1 output. Verified with `tools/check_byte_identical.sh` on 24 builds. |
| `NES2GBC_XY_CARRY=x` | X only. This is the default. |
| `NES2GBC_XY_CARRY=xy` | X and Y. |
| `NES2GBC_XY_CARRY=y` | Y only. |
| `NES2GBC_XY_KEEP=0` | No keep across barriers. |
| `NES2GBC_XY_RANGE=lo-hi` | Debug bisection filter. |

Y is off by default because it measured as a net loss: SMB wcost was +4%.
Predecessors must materialise C for the contract more often than the
contract saves reloads.

**Not done:** banked.rs (MMC1/UxROM/... games) has no carry yet. Contracts are
same-bank only. `NES2GBC_DEBUG_TRACE` builds disable it.

**Stage 3 (A carry), proposed:**

- Reuse the contract machinery with A in host A. Every exit path then has to
  keep A out of scratch use: most lowering uses A as the accumulator for
  addresses and flags.
- So the contract should be "A in D" (or E), via the DE cache, or limited to
  edges whose successor's first MIR use is A.
- Text passes (cache_a_in_blocks, a-cache, de-cache) must learn the
  `_trace` stub convention first.
