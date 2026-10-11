# Shared Z/N shadow (ZN_SHARED) — design, constraints, inventory

Status: design + inventory only (branch `perf/zn-shared`). Nothing here changes
generated code yet; `ZN_SHARED` is not wired up.

## Today

* `nes_z_shadow` (HRAM): Z = (byte == 0).
* `nes_n_shadow` (HRAM): N = bit 7.
* Common writers (LDA/LDX/LDY/TAX/INX/ALU/CMP/shift results) store the *same*
  value to both: `ldh [nes_z_shadow], a / ldh [nes_n_shadow], a` (3+3 M).
  Liveness passes (`dead_terminal_*`, `dead_global_zn`, `dead_hram_state_global`)
  already drop whichever half is dead, so many sites store only one byte.
* Rare writers where Z and N come from different sources: BIT (`nes_bit_a_e`,
  Z = A&M, N = M bit 7), PLP/RTI (`nes_set_p_from_a`), the RAM interpreter
  (`interp.asm`, 120 stores), boot/ISR/PPU-poll initialisers.

## Ceiling (158a9f1, APU=0, work.py windows, % of all GB cycles)

| game | st nes_z_shadow | st nes_n_shadow | ld nes_n_shadow |
|---|---|---|---|
| Mega Man | 3.9% | 1.6% | 0.0% |
| Contra | 3.2% | 2.6% | 0.0% |
| Castlevania | 3.3% | 2.3% | 0.1% |
| Zelda | 1.7% | 0.9% | 0.0% |

Only the N stores at *paired* sites can disappear (lone N stores just move to
the shared byte), so the realistic win is roughly 0.7-2% depending on the game.
N reads are almost free today (<0.1%), so making N reads slower is affordable;
Z reads (`ldh a,[z] / and a`, ~0.4%) must stay one load + test.

## The hard constraint (why a pure one-byte scheme is impossible)

Common writers store an arbitrary A (all 256 values) and readers decode a fixed
function of the stored byte(s). Every byte value is therefore already "taken"
by a common decoding: 0 -> (Z=1,N=0), $01-$7F -> (0,0), $80-$FF -> (0,1).
The state Z=1,N=1 (BIT with A&M=0 and M bit 7 set; PLP/RTI of such a P) has no
free encoding. A second byte `s` ("N when Z=1") written only by rare paths
does not fix it on its own: after rare(Z=1,N=1) -> common write of 0 ->
N read, the bytes are (0,$80) in both histories, but the correct N differs.
So **some** common writers must still clear `s`. The design below makes that
a static, rare set of writers instead of every writer.

## Design: one value byte + region-scoped override byte

* `zn` = the existing `nes_z_shadow` address. Z = (zn == 0). If zn != 0,
  N = bit 7 of zn.
* `s` = the existing `nes_n_shadow` address, reinterpreted: N = bit 7 of `s`
  **only when zn == 0**. Invariant: outside a *rare region*, `s` bit 7 = 0.
* Common writers: one store, `ldh [zn], a` (lone N store -> store to zn; the
  liveness pass that dropped the Z store proved Z dead there, so clobbering it
  is fine; lone Z store unchanged: N dead).
* Rare writers (BIT, PLP, RTI/set_p, interpreter exits, boot/ISR):
  zn = Z ? 0 : (N ? $80 : $01); s = (Z && N) ? $80 : 0.
* Rare region of a rare site R = code reachable from R before every path has
  passed a common ZN writer. The first common ZN writers on those paths
  ("region exits") additionally clear `s` (keep the second store, but of 0:
  e.g. `push af / xor a / ldh [s], a / pop af`, or `ldh [s], a` when A is
  provably < $80). Everywhere else the second store disappears.
* N readers (`ldh a,[n] / bit 7,a` and friends, ~25-50 static sites per game):
  `ldh a,[zn] / and a / jr nz,:+ / ldh a,[s] / : / bit 7,a` (A may differ
  in bits 0-6: require A dead after, else keep an extra `ld`).
* Z readers unchanged.
* `nes_materialize_p` (NMI entry, PHP, BRK): N decode as above.
  `nes_set_p_from_a` (PLP, RTI): rare-writer encoding above.
* RTI: the restored state is (Z=1,N=1) only if the interrupted point was
  inside a static rare region; the region exits after that point clear `s`,
  so RTI is not an independent region source. NMI-handler code is ordinary
  translated code and gets its own regions.

### Region analysis (the risky part)

Regions must be computed over the translated-code CFG, not per block:
BIT is often the last instruction before a branch, and PLP is followed by RTS.
If a region escapes through an indirect transfer (RTS/JMP ()/dispatch) before
all paths hit a common writer, the region cannot be bounded statically. The
plan for those sites is to *close the region at the rare site itself*: the
rare writer emits the extra work only when both Z and N are live afterwards
(compiler flag liveness already knows this), and otherwise emits the cheap
encoding with `s` left 0. Sites where both are live and the region escapes
keep today's two-byte semantics for the whole build (fallback: ZN_SHARED off
for that ROM), which is decided at generate time and reported.

## Plan (incremental, behind `ZN_SHARED=1`, default off)

1. Runtime: `nes_materialize_p`, `nes_set_p_from_a`, `nes_bit_a_e`, boot/ISR,
   PPU poll initialisers, interpreter flag code (`gen_ram_interp.py`) use the
   new encoding when `NES2GBC_ZN_SHARED` is defined. A/B with all N stores
   still present must be bit-for-bit equivalent in RAM (eqchk) — this step
   only changes the rare writers' encoding.
2. Emitter + tools: N reads -> decode form (src/lr35902.rs, recompile.rs,
   superblock.rs, state_superblock.rs; tools listed below that synthesize
   `ldh a, [nes_n_shadow]`).
3. Final pass `zn_shared.py` (after every flag pass): rare-site/region
   analysis, drop paired N stores outside region exits, rewrite region-exit
   N stores to the `s` clear, retarget lone N stores to zn. All the
   pattern-matching passes keep seeing today's two-byte form because this runs
   last.
4. Verification: MM eqchk frame-by-frame vs ZN_SHARED=0, SMB gate, NES-frame
   RAM compares, longplay all four, 9-game soak, ci_smoke, cargo test.

## Inventory

Counts: reads = `ldh a, [nes_{z,n}_shadow]` literals, writes =
`ldh [nes_{z,n}_shadow], a` literals, refs = any mention.

### Core (emitter, runtime)

| file | reads | writes | refs |
|---|---|---|---|
| `src/lr35902.rs` | 4 | 10 | 14 |
| `src/recompile.rs` | 3 | 0 | 3 |
| `src/state_superblock.rs` | 2 | 6 | 8 |
| `src/superblock.rs` | 2 | 0 | 2 |
| `runtime/cpu.asm` | 2 | 12 | 17 |
| `runtime/interp.asm` | 4 | 120 | 124 |
| `runtime/main.asm` | 0 | 2 | 2 |
| `runtime/ppu.asm` | 0 | 2 | 2 |

Runtime roles: `nes_set_nz_from_a` (common writer), `nes_materialize_p`
(reader: NMI entry/PHP/BRK), `nes_set_p_from_a` (rare writer: PLP/RTI),
`nes_compare_a_e` (common), `nes_bit_a_e` (rare), boot init in `main.asm`
and the sprite-0/$2002 poll reset in `ppu.asm` (rare, constant 0 -> Z=1,N=0),
`interp.asm` (RAM interpreter, generated by `tools/gen_ram_interp.py`).

### Post-passes (`tools/*.py`)

All of these run before the proposed final pass and keep working on today's
two-byte form; only the ones that *emit* N reads need step 2 changes.

| tool | reads | writes | refs |
|---|---|---|---|
| `dead_af_compute.py` | 1 | 0 | 1 |
| `dead_global_zn.py` | 0 | 0 | 3 |
| `dead_hram_state_global.py` | 0 | 0 | 2 |
| `dead_terminal_carry_zn.py` | 0 | 2 | 2 |
| `dead_terminal_n.py` | 0 | 2 | 2 |
| `dead_terminal_overflow_zn.py` | 0 | 2 | 2 |
| `dead_terminal_zn.py` | 0 | 2 | 2 |
| `fast_fill_loops.py` | 0 | 1 | 1 |
| `final_peephole.py` | 0 | 0 | 1 |
| `fuse_compare_carry_branch_safe.py` | 0 | 0 | 2 |
| `fuse_index_branch_value.py` | 2 | 4 | 7 |
| `fuse_mmc1_serial_writes.py` | 0 | 2 | 2 |
| `fuse_sprite0_branch.py` | 1 | 2 | 3 |
| `gen_ram_interp.py` | 4 | 7 | 11 |
| `host_z_branch.py` | 5 | 4 | 10 |
| `hot_alu_generated.py` | 0 | 8 | 8 |
| `index_math_generated.py` | 0 | 4 | 4 |
| `native_blockbuf_collision.py` | 0 | 2 | 2 |
| `native_bounding_box.py` | 0 | 2 | 2 |
| `native_dk_box_collision.py` | 0 | 2 | 2 |
| `native_dk_sprite_loops.py` | 0 | 4 | 4 |
| `native_draw_sprite_object.py` | 0 | 2 | 2 |
| `native_enemy_parser.py` | 0 | 2 | 2 |
| `native_metatile_column.py` | 0 | 2 | 2 |
| `native_move_object_h.py` | 0 | 2 | 2 |
| `native_multibyte_compare_copy.py` | 0 | 2 | 2 |
| `native_offscreen_bits.py` | 0 | 4 | 4 |
| `native_relative_xy_leaf.py` | 0 | 2 | 2 |
| `native_small_loops.py` | 0 | 4 | 4 |
| `native_tiny_leaves.py` | 0 | 6 | 6 |
| `peephole_generated.py` | 1 | 4 | 8 |
| `preserve_a_across_branches.py` | 2 | 0 | 2 |
| `reg_copy_prop.py` | 0 | 0 | 1 |
| `shrink_compare_generated.py` | 0 | 4 | 4 |
| `specialize_inline_dispatchers.py` | 0 | 2 | 2 |
| `specialize_sprite0_poll.py` | 0 | 2 | 2 |
| `store_reload.py` | 1 | 1 | 3 |
| `virtualize_sprite0_waits.py` | 0 | 2 | 2 |

## Findings from the implementation attempt (session 2)

Static scan of the final asm at 158a9f1 (`/tmp/sweep/znscan.py`):

| game | paired Z+N stores | lone N stores | N reads | inline BIT split pairs | BIT/PLP helper calls |
|---|---|---|---|---|---|
| Mega Man | 739 | 104 | 24 | 1 | 18 |
| Zelda | 2540 | 206 | 44 | 7 | 17 |
| Contra | 2273 | 86 | 124 | 2 | 10 |
| Castlevania | 2316 | 25 | 98 | 4 | 2 |
| SMB | 60 | 155 | 50 | 0 | 0 |

Blocker: the region analysis does not close in practice.

* PLP sites are followed by RTS (`call nes_set_p_from_a` then the inline RTS
  pop and `jp nes_jump_known_hl_a*` / `nes_dispatch_hl`), so their region
  escapes through a dynamic return on the first instruction.
* All four banked games link the RAM interpreter (`RAM_INTERP`). Its exits
  back to translated code are dynamic too. Any BIT/PLP it executes is a rare
  writer whose region starts at an arbitrary dispatch target.
* Without interprocedural return sets (all RTS targets, including the
  PHA/PHA/RTS jump-table idiom used by Zelda and Mega Man), every banked game
  would have to abort to the two-byte form. SMB is the only build that
  qualifies, and it has just 60 paired stores, so there is nothing to gain.

The shared-byte scheme therefore needs the same interprocedural machinery as
the alternative below, plus a format change. The alternative gets most of
the win without changing the format.

## Recommended next approach: interprocedural N dead-store elimination

N is read rarely (24-124 static BMI/BPL sites per game; PHP/BRK; NMI entry
only through the handler's stacked P, which the poll-aware liveness already
models). Most paired N stores are dead globally. They survive only because
liveness stops at JSR/RTS/dispatch. `tools/dead_global_zn.py` exists but is
not in the pipeline. It treats JSR as a reader, treats RTS as a dynamic exit,
and only matches `nes_XXXX` (not banked `nes_mN_bXX_YYYY`) labels.

Plan:
1. Return-site sets per RTS: JSR return sites of every caller that reaches it,
   plus RTS-trick targets (PHA/PHA/RTS tables) from the existing
   `rts_return_sets` analysis. Unknown -> live.
2. JSR = edge into the callee entry (not a reader); RTS live-out = union of
   live-in at its return sites; indirect JMP / dispatch with unresolved
   targets -> live.
3. Extend to banked labels, run after `dead_hram_state_global.py`, and drop
   only `ldh [nes_n_shadow], a` (Z can follow later).
4. Same verification list as above. No runtime, reader or interpreter change,
   so it is safe to land incrementally.
