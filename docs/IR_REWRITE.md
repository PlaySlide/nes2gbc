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
* `MirBuf`: the single emission buffer every path now writes into (it
  implements `fmt::Write`, `push_str`, `len`, `truncate`, and derefs to `str`,
  so emitter code is unchanged). It keeps the exact text and incrementally
  parses each completed line into an `Item`; printing is the stored text, so
  output is identical by construction. `MirBuf::blocks()` gives
  global-label blocks with successor labels and fall-through.
* `report_entry_reloads` (`NES2GBC_MIR_STATS=1`) prints, per path, how many
  block-entry HRAM reloads of A/X/Y/SP exist: the stage-2 opportunity.

### Shared logic

* `src/emit_common.rs`: `estimated_block_size`, `emit_pc_dispatch`,
  `flag_mask` (were copied in each path).
* `src/polls.rs`: NMI poll placement: `nmi_poll_points` (was copied 3x),
  `cyclic_back_targets`, counted-loop budget rules; used by NROM and banked.

## Stage plan

1. **Stage 1 (this branch)**: MIR types; all paths emit into `MirBuf`;
   dedupe shared helpers; byte-identical proof script. *Status: done for the
   buffer/IR layer and dedup; emitters still build instructions as text
   (writeln!) which MirBuf parses.*
   Remaining stage-1 items:
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
