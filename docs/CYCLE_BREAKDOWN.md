# GB cycle breakdown of a gameplay window (8 games, tip e9ed125)

Measurement only: no translator change. Built from `perf/banked-superblocks`
e9ed125 in a fresh worktree, NES2GBC_* unset, APU=1 (default), PyBoy with
sound emulated. Every executed GB T-cycle in the window is attributed. The
executed total matches the wall budget (GB frames x 140448, double speed)
within 0.2%, so HALT/idle time is included (it shows up in idle/wait and spin).

## Method (tools/cycle_breakdown/)

* `build.sh ROM OUT/name`: `make generate` + build, then
  `annotate_asm.py` puts a local label `.gqN` before every SM83 instruction of
  translated code (NES block, mapper bank block, overlay block, mapper2
  dispatch stub) in the final post-pass `runtime/generated.asm` and rebuilds.
  It checks that the annotated ROM is **byte-identical** to the plain build
  (also identical to the regular e9ed125 builds). `name.lmap.tsv` maps each
  label to its block, the 6502 instruction (from the
  `; $PC: $OP Mnem Mode` comments), and an overhead tag:
  HRAM save/reload per register, flag materialize/reload (Z/N/C/V/P),
  NMI poll, helper call, dispatch/bank setup, push/pop af, overlay guard,
  sprite-0 poll. The rest is `core`, meaning the 6502 semantics proper.
* `profile_run.py`: PyBoy `prof_attach` (T-cycles per executed PC, per ROM
  bank) over an NES-frame window. Inputs are keyed to NES frames (game frame
  counter for banked games; NMI-vector hook for NROM).
* `breakdown.py`: labels give exact per-GB-instruction cycles. Executions come
  from an SM83 timing table (conditional instructions inherit the count of the
  previous instruction of the same 6502 instruction). The NES instruction count
  is taken at the first GB instruction of each 6502 instruction. Runtime PCs
  map to the enclosing global symbol and are grouped into buckets. 6502
  busy-wait loops (pure load/compare/branch cycles, as in m12/work.py) are
  split out as spin.
* `report.py`: the tables below. "Combined" is the per-game share averaged
  with each game weighted equally.

Windows (NES frames): Zelda 60-360 (scripted file select, then walking),
Contra 700-1600, Castlevania 700-1700, Mega Man 1600-2600 (scripts as in
work4), SMB/DK/IC/BF 200-800 (start, right, A).

Limits:
- Interrupt entry cycles (20 T each) are not in PyBoy's per-PC counts.
- RAM-executed code is attributed to the nearest RAM symbol ("RAM code near ...").
- Helper cycles are reported under the runtime routine, not folded into the
  calling 6502 class. "calls/instr" shows how often a class calls out.
- Native whole-routine replacements and trace-entry code have no per-instruction
  comments and appear as "(block prologue / native routine bodies)".
- Castlevania's `Lda Absolute` is dominated by the inline sprite-0/$2002 poll
  loop at $F8CC. DK's `Ror ZeroPage` is the RNG at $F4FD that its main loop
  calls until NMI, i.e. NES idle filler, not real work.

## Headline numbers

| Game | GB T-cyc / NES frame | GB frames / NES frame | translated (incl. spin) | spin | runtime | NES instr / frame | T-cyc / 6502 instr inline | + mem/flag/dispatch helpers | ideal | ratio |
|---|---|---|---|---|---|---|---|---|---|---|
| Zelda | 282,831 | 2.02 | 62.9% | 6.7% | 37.1% | 3,089 | 49.0 | 62.4 | 16.8 | 3.7x |
| Contra | 325,600 | 2.32 | 64.1% | 0.0% | 35.9% | 4,049 | 50.2 | 61.4 | 19.4 | 3.2x |
| Castlevania | 394,877 | 2.82 | 72.5% | 0.0% | 27.5% | 5,390 | 52.4 | 62.0 | 16.8 | 3.7x |
| Mega Man | 495,992 | 3.53 | 79.0% | 8.7% | 21.0% | 8,204 | 40.2 | 48.1 | 18.8 | 2.6x |
| SMB | 156,224 | 1.11 | 71.2% | 23.1% | 28.8% | 2,467 | 30.5 | 32.5 | 13.5 | 2.4x |
| DK | 151,982 | 1.08 | 70.3% | 0.0% | 29.7% | 2,247 | 47.5 | 49.4 | 18.9 | 2.6x |
| IC | 156,237 | 1.11 | 71.7% | 0.0% | 28.3% | 2,407 | 46.6 | 48.9 | 16.8 | 2.9x |
| BF | 140,428 | 1.00 | 75.9% | 0.1% | 24.1% | 2,295 | 46.4 | 47.5 | 20.0 | 2.4x |

The NROM games run at about 1 GB frame per NES frame (frame-locked, with
spare time in spin and idle filler). The banked games need 2-3.5 GB frames per
NES frame, so they are where cycle cuts pay.

### Ideal vs current cycles per 6502 instruction

The "ideal" column assumes a hand-written SM83 sequence with A/X/Y and flags
living in GB registers and NES RAM at a fixed WRAM base, for example:
- 8 T for an immediate load;
- 16 for zp/abs;
- 32 for zp,X; 40 for abs,X/Y; 64 for (zp),Y;
- 4 for INX/TAX;
- 10 for a Z/C branch, 18 for N/V;
- 40 for JSR, 32 for RTS;
- +8 for RMW, +4 for inverted-carry compares/SBC.

Today the cost is 30-52 T per 6502 instruction inline and 32-62 T including
memory, flag and dispatch helpers: **2.4-3.7x the ideal**. In absolute terms
an ideal translation of the same instruction stream would need roughly
16-20 T per instruction. For Mega Man that is about 154k T per NES frame for
translated work instead of about 390k.

The excess concentrates in:
- **RTS and JSR** (153 and 86 T vs 32/40);
- **(zp),Y** loads/stores (138/153 T vs 64);
- **ADC/CMP** (with immediate: 101 and 47 T vs 8/12, because of carry/V/flag materialization);
- **DEY/DEX** (44/33 T vs 4: HRAM reload, store, and Z+N store);
- **branches** (22-32 T vs 10).

`Lda ZeroPage` and `Sta ZeroPage` are already at the ideal (12-18 T).

## Combined top 20 cost centers

Average % of executed GB cycles over the 8 games (equal weight). Savings are
estimated as average % of frame, assuming the idea removes the stated
fraction.

| # | Cost center | avg % | where | idea | est. saving |
|---|---|---|---|---|---|
| 1 | 6502 busy-wait loops (spin) | 4.82 | SMB 23.1, MM 8.7, Zelda 6.7 | Not work: waiting for NMI/vblank. Lower it with earlier NMI delivery under pacing (start the next NMI as soon as the frame is published) or `halt` in recognized wait loops. That saves power and gives cleaner profiles, not fps, unless the wait is on a GB-side event. | 0 fps; up to 4.8% headroom |
| 2 | RTS | 4.63 | all; 153 T, 19.5 GB instr | Predicted return: JSR stores a 1-byte return token and RTS compares the NES stack bytes against the static return PC, then `jp` direct. That skips dispatch_hl and HRAM SP reload/save (SP kept in a GB reg within a superblock). | ~2.5% |
| 3 | ROR zp | 3.04 | DK 22.6 (idle RNG), IC 1.7 | Keep the 6502 carry in the GB carry across ROR/ROL chains (no c_shadow reload/store per instruction): 59 T to ~24 T. | 0.3% real (DK part is filler) |
| 4 | LDA (zp),Y | 2.89 | MM 6.6, BF 4.9, Contra 3.9 | Pointer-region specialization: if the high byte is known RAM (<$08) emit a direct `ld h,$C0+hi&7` path. Guard and hoist the pointer translation out of loops (Y-indexed loops reuse HL). Avoid the cpu_read_hi call (0.53 calls/instr). | ~1.3% |
| 5 | JSR | 2.75 | all; 86 T | Write the return bytes with one `ld [hl+]` pair to a cached SP pointer. Elide SP save/reload between JSR and callee entry when the callee is static (carry SP in a register). | ~1.2% |
| 6 | NMI poll per block | 2.74 | Zelda 4.6, CV 4.0 | Poll only at loop back-edges, calls into unknown code and blocks longer than N cycles. Straight-line block chains need no poll each (max latency is bounded by the next back-edge). | ~1.9% |
| 7 | nes_oam_dma_fast_unrolled | 2.48 | NROM 3.4-3.8 | Skip when the source OAM page hash/dirty flag is unchanged, or DMA only the used sprite range (high-water already tracked). | ~1.0% |
| 8 | nes_video_build_oam_shadow_fast | 2.07 | BF 4.2, NROM 2.7 | Incremental conversion: only re-convert sprites whose 4 bytes changed. Early-out on Y >= $EF (hidden sprites) before attribute/tile translation. | ~1.0% |
| 9 | HRAM save A | 1.78 | all | Cross-block A carry for static edges (A live in `a` at block entry). Sink saves to exits that really need canonical A. | ~0.9% |
| 10 | STA (zp),Y | 1.76 | BF 6.9, IC 3.3, MM 3.3 | Same pointer specialization as #4, plus write-region proof to skip the PPU/APU check. | ~0.9% |
| 11 | LDA abs,X | 1.60 | Contra 2.7, CV 2.6, BF 2.5 | Keep X in a GB register (`c`) across a block and use `ld hl,base / add l / ld l,a / adc h ...` with the page-cross test elided when base+X can't cross. | ~0.4% |
| 12 | flag materialize Z | 1.58 | all | Lazy Z/N, a shared result byte (prototype on wip/lazy-flags: about 2.5% ceiling paired with N). Drop Z stores whose next consumer is a same-block branch already using GB Z. | ~0.8% |
| 13 | BNE | 1.51 | all; 31 T | Branch directly on GB flags when the producer is in the block (most cases). Otherwise one `ldh a,[z]; and a` instead of a reload+compare. Chain hot fall-through targets. | ~0.7% |
| 14 | block prologue / native routine bodies | 1.49 | SMB 7.4, DK 2.8 | Mostly useful native routines (SMB DrawSpriteObject, DK/SMB traces). Review the trace-entry glue only. | ~0.2% |
| 15 | helper call/jp instructions | 1.46 | Zelda 2.5, Contra 2.0 | Inline the common fast path of cpu_read_hi/prg_read_hi (ROM read with a known bank) at the call site. Use `rst` for the most common helpers (12 T saved per call). | ~0.5% |
| 16 | HRAM reload X | 1.45 | all | Keep X/Y in GB registers (b/c or d/e) inside superblocks; reload once at entry. Cross-block X/Y carry on static edges. | ~0.8% |
| 17 | LDA abs | 1.44 | CV 8.4 (sprite-0 $2002 poll at $F8CC) | Sprite-0 poll: compute the hit scanline and `halt` on a STAT LYC interrupt instead of spinning on emulated $2002. | ~1% (CV ~7%) |
| 18 | flag materialize N | 1.40 | all | As #12: N from the shared result byte; drop stores dead before the next N write. | ~0.7% |
| 19 | flag materialize C | 1.39 | all | Keep C in GB carry across ADC/SBC/ROx chains; store c_shadow only at block exits where it is live. | ~0.6% |
| 20 | HRAM save SP | 1.34 | all | With #2/#5, track the SP delta statically inside superblocks and commit SP once per exit. | ~0.8% |

Aggregates behind these items:
- HRAM A/X/Y/SP traffic is 10.4% and flag materialize/reload is 6.9% of all
  GB cycles: together 17.3%, the biggest single lever.
- Runtime memory helpers (cpu_read/write, prg_read) are 5.3% (Contra and CV
  about 10%).
- OAM is 5.4%, APU 4.0% (sound emulated, NES-frame-tied), nametable/stitch
  3.4%, follow camera 1.8% (Contra/CV 3+%, DK 4.9%), dispatch + bank switch
  1.5% plus mapper2 stubs 1.1% (Zelda 4.4 + 2.8).

Per-game items outside the top 20 worth a look:
- Contra/CV `nes_view_follow_hinted` (3.4/3.1%) and `nes_cpu_read_hi_view` (3.1/3.0%);
- DK `nes_view_follow_update_romx` 4.7%;
- MM `nes_cpu_write` 2.1% and mapper2 stubs 3.7%;
- Zelda `nes_dispatch_hl` 2.2%, mapper2 stubs 2.8%, nametable/stitch 6.9%.

## Reproduce

```
cd <worktree>; export PATH=/workspace/tools-bin:$PATH
tools/cycle_breakdown/build.sh /path/game.nes /tmp/cb/g
python3 tools/cycle_breakdown/profile_run.py /tmp/cb/g.gbc game.nes 1a "start@300:5,..." 700 1600 /tmp/cb/g.npy
python3 tools/cycle_breakdown/breakdown.py /tmp/cb/g Game
python3 tools/cycle_breakdown/report.py /tmp/cb g:Game ...
```
`tools/cycle_breakdown/run_all.sh` does all 8 games with the windows above.

# Generated tables
## Headline numbers per game

| Game | NES frames | GB T-cycles / NES frame | translated (incl. spin) | spin | runtime | idle/wait | NES instr / frame | T-cycles / NES instr (inline) | (inline + mem helpers) | ideal | ratio |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Zelda | 300 | 282831 | 62.9% | 6.7% | 37.1% | 1.2% | 3089 | 49.0 | 62.4 | 16.8 | 3.7x |
| Contra | 900 | 325600 | 64.1% | 0.0% | 35.9% | 2.1% | 4049 | 50.2 | 61.4 | 19.4 | 3.2x |
| Castlevania | 1000 | 394877 | 72.5% | 0.0% | 27.5% | 0.4% | 5390 | 52.4 | 62.0 | 16.8 | 3.7x |
| MegaMan | 1000 | 495992 | 79.0% | 8.7% | 21.0% | 0.0% | 8204 | 40.2 | 48.1 | 18.8 | 2.6x |
| SMB | 600 | 156224 | 71.2% | 23.1% | 28.8% | 1.2% | 2467 | 30.5 | 32.5 | 13.5 | 2.4x |
| DK | 600 | 151982 | 70.3% | 0.0% | 29.7% | 1.6% | 2247 | 47.5 | 49.4 | 18.9 | 2.6x |
| IC | 600 | 156237 | 71.7% | 0.0% | 28.3% | 2.2% | 2407 | 46.6 | 48.9 | 16.8 | 2.9x |
| BF | 600 | 140428 | 75.9% | 0.1% | 24.1% | 0.2% | 2295 | 46.4 | 47.5 | 20.0 | 2.4x |

NES instructions are counted at the first GB instruction of each translated 6502 instruction (spin loops included); "inline" is all GB cycles in translated blocks (core + overhead tags, excluding mapper2 stubs and spin loops) divided by that count; "+ mem helpers" adds cpu read/write, flag, dispatch/bank helpers and stubs.

## Combined top-level split (each game weighted equally, % of executed GB cycles)

| Bucket | avg | Zelda | Contra | Castlevania | MegaMan | SMB | DK | IC | BF |
|---|---|---|---|---|---|---|---|---|---|
| translated: core 6502 semantics | 41.87 | 28.22 | 38.25 | 41.51 | 46.46 | 32.60 | 52.35 | 42.47 | 53.08 |
| translated: HRAM A/X/Y/SP save+reload | 10.30 | 8.85 | 10.90 | 9.95 | 9.04 | 8.69 | 9.04 | 14.35 | 11.57 |
| translated: flag materialize/reload | 6.77 | 6.50 | 8.11 | 7.63 | 7.85 | 2.87 | 4.88 | 8.69 | 7.65 |
| runtime: OAM | 5.37 | 3.34 | 4.95 | 4.06 | 2.29 | 6.62 | 6.70 | 6.57 | 8.46 |
| runtime: cpu read/write helpers | 5.27 | 6.59 | 10.07 | 10.17 | 7.36 | 2.49 | 1.79 | 2.53 | 1.13 |
| 6502 busy-wait loops (spin) | 4.82 | 6.67 | 0.00 | 0.00 | 8.74 | 23.11 | 0.00 | 0.00 | 0.06 |
| runtime: APU | 4.04 | 3.81 | 2.18 | 1.42 | 5.75 | 5.36 | 4.31 | 3.91 | 5.55 |
| runtime: nametable/stitch | 3.35 | 6.90 | 2.81 | 1.75 | 0.21 | 3.58 | 3.31 | 5.06 | 3.15 |
| translated: NMI polls | 2.74 | 4.56 | 2.88 | 3.99 | 1.93 | 1.00 | 1.62 | 3.16 | 2.82 |
| runtime: video other | 2.04 | 1.64 | 1.67 | 1.56 | 1.11 | 3.06 | 2.99 | 2.69 | 1.60 |
| runtime: PPU reg emulation | 1.84 | 1.77 | 3.88 | 1.48 | 0.02 | 2.61 | 1.41 | 2.29 | 1.24 |
| runtime: follow camera | 1.76 | 0.24 | 3.52 | 3.16 | 1.25 | 0.36 | 4.92 | 0.23 | 0.38 |
| translated: helper/dispatch/bank call setup | 1.70 | 3.18 | 2.31 | 1.95 | 1.29 | 1.71 | 1.18 | 1.37 | 0.59 |
| runtime: dispatch + bank switch | 1.48 | 4.41 | 2.06 | 1.73 | 1.09 | 0.61 | 0.70 | 0.77 | 0.49 |
| translated: mapper2 dispatch stubs | 1.14 | 2.79 | 1.62 | 1.02 | 3.65 | 0.00 | 0.00 | 0.00 | 0.00 |
| runtime: idle/wait | 1.10 | 1.18 | 2.11 | 0.39 | 0.01 | 1.23 | 1.55 | 2.16 | 0.15 |
| runtime: controller | 1.00 | 2.10 | 1.01 | 0.42 | 0.33 | 1.02 | 1.08 | 1.05 | 1.02 |
| translated: sprite-0 poll wait | 0.95 | 1.13 | 0.00 | 6.49 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| translated: unlabeled | 0.53 | 0.00 | 0.00 | 0.00 | 0.00 | 1.24 | 1.20 | 1.71 | 0.13 |
| runtime: NMI/vblank/ISR | 0.52 | 0.38 | 0.33 | 0.27 | 0.65 | 0.63 | 0.63 | 0.60 | 0.66 |
| runtime: frame pacing | 0.50 | 1.37 | 1.15 | 0.88 | 0.00 | 0.47 | 0.09 | 0.06 | 0.00 |
| runtime: runtime other | 0.40 | 2.46 | 0.02 | 0.03 | 0.01 | 0.58 | 0.01 | 0.02 | 0.08 |
| runtime: flag helpers | 0.38 | 0.87 | 0.18 | 0.15 | 0.96 | 0.19 | 0.21 | 0.31 | 0.20 |
| translated: overlay guards | 0.13 | 1.03 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |

## Combined cost centers (each game weighted equally)

| # | Cost center | avg %% | Zelda | Contra | Castlevania | MegaMan | SMB | DK | IC | BF |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | T ovh: 6502 busy-wait loops (spin) | 4.82 | 6.67 | 0.00 | 0.00 | 8.74 | 23.11 | 0.00 | 0.00 | 0.06 |
| 2 | T core: Rts Implied | 4.63 | 3.38 | 5.91 | 4.03 | 3.64 | 4.30 | 6.58 | 4.05 | 5.11 |
| 3 | T core: Ror ZeroPage | 3.04 | 0.00 | 0.01 | 0.01 | 0.00 | 0.00 | 22.60 | 1.73 | 0.00 |
| 4 | T core: Lda IndirectIndexed | 2.89 | 1.39 | 3.93 | 3.42 | 6.55 | 0.66 | 0.71 | 1.63 | 4.87 |
| 5 | T core: Jsr Absolute | 2.75 | 1.96 | 2.81 | 1.89 | 1.76 | 3.21 | 5.54 | 2.17 | 2.65 |
| 6 | T ovh: NMI poll (block prologue) | 2.74 | 4.56 | 2.88 | 3.99 | 1.93 | 1.00 | 1.62 | 3.16 | 2.82 |
| 7 | R: nes_oam_dma_fast_unrolled | 2.48 | 1.86 | 1.62 | 1.33 | 1.06 | 3.37 | 3.47 | 3.37 | 3.75 |
| 8 | R: nes_video_build_oam_shadow_fast | 2.07 | 0.17 | 1.61 | 1.32 | 1.08 | 2.77 | 2.74 | 2.72 | 4.16 |
| 9 | T ovh: HRAM save A | 1.78 | 1.34 | 2.12 | 1.75 | 1.94 | 1.12 | 2.03 | 1.74 | 2.20 |
| 10 | T core: Sta IndirectIndexed | 1.76 | 0.08 | 0.00 | 0.06 | 3.27 | 0.27 | 0.22 | 3.32 | 6.87 |
| 11 | T core: Lda AbsoluteX | 1.60 | 1.22 | 2.71 | 2.56 | 1.18 | 0.75 | 0.89 | 1.05 | 2.46 |
| 12 | T ovh: flag materialize Z (store) | 1.58 | 1.90 | 1.98 | 2.08 | 1.20 | 0.84 | 0.96 | 1.95 | 1.72 |
| 13 | T core: Bne Relative | 1.51 | 1.57 | 0.88 | 1.36 | 1.60 | 1.66 | 0.89 | 2.85 | 1.27 |
| 14 | T core: (block prologue / native routine bodies) | 1.49 | 1.67 | 0.00 | 0.00 | 0.00 | 7.41 | 2.75 | 0.11 | 0.01 |
| 15 | T ovh: helper call/jp instructions | 1.46 | 2.54 | 2.03 | 1.79 | 1.23 | 1.42 | 0.90 | 1.25 | 0.51 |
| 16 | T ovh: HRAM reload X | 1.45 | 1.61 | 1.94 | 1.64 | 0.89 | 0.91 | 0.35 | 1.84 | 2.40 |
| 17 | T core: Lda Absolute | 1.44 | 1.32 | 0.30 | 8.38 | 0.15 | 0.95 | 0.09 | 0.26 | 0.04 |
| 18 | T ovh: flag materialize N (store) | 1.40 | 1.67 | 1.99 | 1.93 | 1.03 | 0.66 | 0.80 | 1.53 | 1.57 |
| 19 | T ovh: flag materialize C (store) | 1.39 | 1.46 | 1.16 | 1.13 | 1.63 | 0.50 | 1.89 | 2.05 | 1.27 |
| 20 | T ovh: HRAM save SP | 1.34 | 1.00 | 1.30 | 0.97 | 0.94 | 1.30 | 2.04 | 1.65 | 1.49 |
| 21 | T ovh: HRAM reload SP | 1.29 | 0.98 | 1.23 | 0.89 | 0.84 | 1.42 | 2.12 | 1.51 | 1.32 |
| 22 | T core: Beq Relative | 1.26 | 1.02 | 1.27 | 1.43 | 2.24 | 0.98 | 1.21 | 0.92 | 1.05 |
| 23 | R: nes_ppu_write_data | 1.26 | 1.31 | 2.13 | 1.26 | 0.02 | 1.77 | 1.11 | 1.81 | 0.65 |
| 24 | T ovh: HRAM reload Y | 1.19 | 1.16 | 1.14 | 1.17 | 1.20 | 0.96 | 0.36 | 2.13 | 1.40 |
| 25 | T ovh: HRAM reload A | 1.16 | 0.94 | 1.09 | 1.23 | 1.40 | 1.11 | 1.22 | 1.19 | 1.12 |
| 26 | T core: Lda ZeroPage | 1.15 | 0.58 | 0.85 | 0.77 | 1.53 | 0.45 | 2.29 | 1.56 | 1.18 |
| 27 | T core: Sta ZeroPage | 1.14 | 0.46 | 1.24 | 1.57 | 1.22 | 0.52 | 1.59 | 1.26 | 1.26 |
| 28 | T ovh: mapper2 dispatch stubs | 1.14 | 2.79 | 1.62 | 1.02 | 3.65 | 0.00 | 0.00 | 0.00 | 0.00 |
| 29 | T ovh: HRAM save Y | 1.09 | 0.94 | 1.17 | 0.98 | 1.02 | 1.17 | 0.37 | 2.12 | 0.92 |
| 30 | R: nes_view_follow_hinted | 1.03 | 0.11 | 3.40 | 3.06 | 1.21 | 0.17 | 0.00 | 0.09 | 0.18 |
| 31 | T ovh: HRAM save X | 1.01 | 0.89 | 0.91 | 1.32 | 0.81 | 0.70 | 0.54 | 2.18 | 0.72 |
| 32 | R: nes_cpu_read_hi | 1.01 | 2.44 | 2.41 | 1.31 | 1.88 | 0.00 | 0.00 | 0.00 | 0.00 |
| 33 | T ovh: push/pop af (flag/A preservation) | 1.00 | 0.44 | 1.18 | 0.72 | 1.96 | 0.35 | 0.16 | 0.99 | 2.19 |
| 34 | R: nes_video_wait_vram | 0.99 | 1.18 | 2.06 | 0.35 | 0.01 | 1.03 | 1.26 | 1.90 | 0.15 |
| 35 | T ovh: sprite-0 / $2002 poll wait (inline expansion) | 0.95 | 1.13 | 0.00 | 6.49 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| 36 | R: nes_video_sync_nametable_write | 0.91 | 1.83 | 0.84 | 0.29 | 0.01 | 0.86 | 1.02 | 1.64 | 0.77 |
| 37 | R: nes_gbc_vblank_isr | 0.90 | 0.91 | 0.82 | 0.89 | 0.79 | 1.17 | 0.90 | 0.89 | 0.82 |
| 38 | T core: Sta AbsoluteX | 0.90 | 0.69 | 2.20 | 1.68 | 0.95 | 0.50 | 0.05 | 0.45 | 0.66 |
| 39 | R: nes_ppu_cpu_write | 0.90 | 0.91 | 0.71 | 0.89 | 0.34 | 1.69 | 0.67 | 1.33 | 0.63 |
| 40 | T core: Ror ZeroPageX | 0.87 | 0.33 | 0.00 | 0.00 | 0.22 | 0.00 | 0.00 | 6.39 | 0.00 |

## Combined 6502 opcode x addressing mode (translated inline cycles incl. overhead tags)

Per class: share of executed cycles (avg over games), dynamic NES instr per frame (avg), GB instructions per 6502 instruction, T-cycles per 6502 instruction (inline), helper/dispatch calls per instruction, ideal T-cycles, excess.

| Class | avg % | NES instr/frame | GB instr / 6502 instr | T-cyc / instr | calls / instr | ideal | excess T-cyc/frame (avg) |
|---|---|---|---|---|---|---|---|
| Rts Implied | 6.19 | 105 | 19.5 | 153.4 | 0.31 | 32 | 12799 |
| Jsr Absolute | 4.07 | 111 | 10.1 | 86.2 | 0.13 | 40 | 5118 |
| Ror ZeroPage | 3.36 | 87 | 6.9 | 59.2 | 0.00 | 24 | 3046 |
| Lda IndirectIndexed | 3.35 | 78 | 17.1 | 137.8 | 0.53 | 64 | 5764 |
| Bne Relative | 2.37 | 234 | 3.2 | 30.7 | 0.03 | 10 | 4843 |
| Sta IndirectIndexed | 2.31 | 34 | 19.3 | 153.4 | 0.00 | 64 | 3059 |
| Lda AbsoluteX | 2.28 | 108 | 6.4 | 54.2 | 0.11 | 40 | 1541 |
| Lda Absolute | 2.01 | 78 | 11.3 | 108.5 | 0.08 | 16 | 7222 |
| Dey Implied | 1.99 | 91 | 5.8 | 43.8 | 0.00 | 4 | 3613 |
| Beq Relative | 1.96 | 271 | 2.4 | 21.8 | 0.02 | 10 | 3204 |
| Sta AbsoluteX | 1.39 | 63 | 8.9 | 73.9 | 0.07 | 40 | 2122 |
| Lda ZeroPage | 1.38 | 256 | 0.8 | 12.2 | 0.00 | 16 | 0 |
| Cmp Immediate | 1.37 | 90 | 5.6 | 47.3 | 0.01 | 12 | 3189 |
| Ror ZeroPageX | 1.31 | 21 | 16.0 | 109.2 | 0.00 | 40 | 1451 |
| Sta ZeroPage | 1.24 | 186 | 1.3 | 18.1 | 0.00 | 16 | 394 |
| Lda AbsoluteY | 1.04 | 45 | 8.3 | 73.9 | 0.43 | 40 | 1539 |
| Adc IndirectIndexed | 1.00 | 14 | 33.3 | 243.3 | 0.06 | 64 | 2544 |
| Bpl Relative | 0.95 | 78 | 2.9 | 31.8 | 0.02 | 18 | 1078 |
| Adc Immediate | 0.94 | 35 | 15.6 | 100.6 | 0.00 | 8 | 3254 |
| Lda ZeroPageX | 0.94 | 39 | 5.9 | 45.1 | 0.00 | 32 | 508 |
| Dex Implied | 0.91 | 68 | 4.5 | 33.3 | 0.00 | 4 | 1987 |
| Adc ZeroPage | 0.73 | 29 | 14.3 | 96.1 | 0.01 | 20 | 2239 |
| Lsr Accumulator | 0.67 | 83 | 2.7 | 19.5 | 0.00 | 8 | 957 |
| Iny Implied | 0.66 | 142 | 1.9 | 11.1 | 0.00 | 4 | 1009 |
| And Immediate | 0.66 | 138 | 1.6 | 15.4 | 0.00 | 8 | 1015 |
| Bcc Relative | 0.65 | 67 | 3.2 | 30.9 | 0.02 | 10 | 1411 |
| Sta AbsoluteY | 0.60 | 33 | 5.7 | 48.2 | 0.00 | 40 | 273 |
| Bcs Relative | 0.59 | 58 | 3.0 | 30.3 | 0.06 | 10 | 1173 |
| Eor ZeroPage | 0.59 | 19 | 7.4 | 58.9 | 0.01 | 20 | 723 |
| Pla Implied | 0.57 | 21 | 6.6 | 50.6 | 0.02 | 28 | 472 |
| Sta Absolute | 0.57 | 33 | 3.6 | 41.0 | 0.71 | 16 | 832 |
| Cpx Immediate | 0.52 | 33 | 5.7 | 44.1 | 0.00 | 12 | 1057 |
| Cmp ZeroPage | 0.47 | 30 | 8.0 | 66.3 | 0.00 | 24 | 1288 |
| Adc AbsoluteX | 0.46 | 8 | 22.0 | 147.3 | 0.24 | 40 | 856 |
| Bmi Relative | 0.45 | 33 | 3.3 | 34.9 | 0.06 | 18 | 550 |
| Inx Implied | 0.38 | 86 | 2.7 | 16.0 | 0.00 | 4 | 1027 |
| Pha Implied | 0.38 | 14 | 8.9 | 68.1 | 0.27 | 24 | 631 |
| Inc ZeroPage | 0.36 | 24 | 3.6 | 41.6 | 0.00 | 24 | 421 |
| Lda Immediate | 0.32 | 90 | 1.2 | 9.4 | 0.00 | 8 | 126 |
| Lsr ZeroPage | 0.32 | 11 | 5.9 | 52.8 | 0.00 | 24 | 326 |
| Rol ZeroPage | 0.32 | 17 | 5.6 | 53.7 | 0.00 | 24 | 519 |
| Ldy Immediate | 0.31 | 68 | 2.3 | 14.7 | 0.00 | 8 | 452 |
| Cpy Immediate | 0.31 | 18 | 5.2 | 42.4 | 0.02 | 12 | 543 |
| Ldy ZeroPage | 0.31 | 32 | 2.3 | 24.4 | 0.00 | 20 | 141 |
| Ldx ZeroPage | 0.29 | 40 | 2.3 | 23.8 | 0.00 | 20 | 152 |

## Zelda

300 NES frames, 282831 GB T-cycles per NES frame (2.02 GB frames per NES frame, double speed).

| Bucket | T-cyc/frame | % |
|---|---|---|
| translated: core 6502 semantics | 79820 | 28.22 |
| translated: HRAM A/X/Y/SP save+reload | 25037 | 8.85 |
| runtime: nametable/stitch | 19520 | 6.90 |
| 6502 busy-wait loops (spin) | 18873 | 6.67 |
| runtime: cpu read/write helpers | 18627 | 6.59 |
| translated: flag materialize/reload | 18385 | 6.50 |
| translated: NMI polls | 12892 | 4.56 |
| runtime: dispatch + bank switch | 12475 | 4.41 |
| runtime: APU | 10780 | 3.81 |
| runtime: OAM | 9436 | 3.34 |
| translated: helper/dispatch/bank call setup | 9000 | 3.18 |
| translated: mapper2 dispatch stubs | 7894 | 2.79 |
| runtime: runtime other | 6963 | 2.46 |
| runtime: controller | 5947 | 2.10 |
| runtime: PPU reg emulation | 4999 | 1.77 |
| runtime: video other | 4634 | 1.64 |
| runtime: frame pacing | 3880 | 1.37 |
| runtime: idle/wait | 3346 | 1.18 |
| translated: sprite-0 poll wait | 3205 | 1.13 |
| translated: overlay guards | 2909 | 1.03 |
| runtime: flag helpers | 2463 | 0.87 |
| runtime: NMI/vblank/ISR | 1064 | 0.38 |
| runtime: follow camera | 682 | 0.24 |

| Opcode x mode | % | NES instr/frame | GB instr/instr | T-cyc/instr | calls/instr |
|---|---|---|---|---|---|
| (block prologue)  | 7.26 | | | | |
| Rts Implied | 4.71 | 98 | 17.8 | 136.5 | 0.69 |
| Jsr Absolute | 3.31 | 100 | 10.7 | 94.1 | 0.62 |
| Bne Relative | 3.25 | 304 | 3.2 | 30.3 | 0.21 |
| Lda Absolute | 2.16 | 148 | 4.2 | 41.2 | 0.00 |
| Lda AbsoluteX | 2.10 | 91 | 6.7 | 65.5 | 0.44 |
| Beq Relative | 1.97 | 204 | 2.9 | 27.3 | 0.21 |
| Lda IndirectIndexed | 1.65 | 35 | 16.9 | 132.9 | 0.38 |
| Bpl Relative | 1.56 | 123 | 3.3 | 36.0 | 0.23 |
| Lda AbsoluteY | 1.41 | 46 | 8.9 | 86.7 | 0.54 |
| Cmp Immediate | 1.28 | 84 | 5.1 | 43.1 | 0.04 |
| Dey Implied | 1.22 | 128 | 4.1 | 27.0 | 0.00 |
| Lsr Accumulator | 1.18 | 120 | 4.0 | 27.8 | 0.00 |
| Sta AbsoluteX | 1.15 | 55 | 6.8 | 59.3 | 0.00 |
| Sta Absolute | 1.14 | 72 | 3.7 | 44.5 | 0.81 |
| Rol ZeroPageX | 1.05 | 37 | 11.0 | 80.0 | 0.00 |
| Cpx Immediate | 1.04 | 53 | 6.8 | 56.1 | 0.04 |
| Cpy Immediate | 1.00 | 49 | 7.3 | 57.8 | 0.09 |
| Dex Implied | 0.93 | 112 | 3.8 | 23.4 | 0.00 |
| Lda ZeroPage | 0.84 | 102 | 1.6 | 23.3 | 0.01 |
| Rol ZeroPage | 0.84 | 37 | 7.0 | 64.0 | 0.00 |
| Lda ZeroPageX | 0.74 | 49 | 5.4 | 42.0 | 0.00 |
| Bcs Relative | 0.72 | 61 | 3.0 | 33.4 | 0.56 |
| Cpx ZeroPage | 0.69 | 30 | 8.5 | 65.5 | 0.01 |
| Adc Immediate | 0.60 | 13 | 19.6 | 132.8 | 0.08 |

| Translated overhead tag | T-cyc/frame | % |
|---|---|---|
| 6502 busy-wait loops (spin) | 18873 | 6.67 |
| NMI poll (block prologue) | 12892 | 4.56 |
| mapper2 dispatch stubs | 7894 | 2.79 |
| helper call/jp instructions | 7194 | 2.54 |
| flag materialize Z (store) | 5385 | 1.90 |
| flag materialize N (store) | 4733 | 1.67 |
| HRAM reload X | 4546 | 1.61 |
| flag materialize C (store) | 4137 | 1.46 |
| HRAM save A | 3793 | 1.34 |
| HRAM reload Y | 3272 | 1.16 |
| sprite-0 / $2002 poll wait (inline expansion) | 3205 | 1.13 |
| overlay staleness guard | 2909 | 1.03 |
| HRAM save SP | 2837 | 1.00 |
| HRAM reload SP | 2764 | 0.98 |
| HRAM save Y | 2659 | 0.94 |
| HRAM reload A | 2657 | 0.94 |
| HRAM save X | 2509 | 0.89 |
| push/pop af (flag/A preservation) | 1234 | 0.44 |
| cross-bank trampoline setup | 1189 | 0.42 |
| flag reload Z | 1052 | 0.37 |
| flag reload C | 1045 | 0.37 |
| dispatch call setup | 617 | 0.22 |
| flag reload P | 338 | 0.12 |
| flag materialize P (store) | 338 | 0.12 |

| Runtime routine | bucket | T-cyc/frame | % |
|---|---|---|---|
| nes_cpu_read_hi | cpu read/write helpers | 6894 | 2.44 |
| nes_dispatch_hl | dispatch + bank switch | 6141 | 2.17 |
| nes_oam_dma_fast_unrolled | OAM | 5268 | 1.86 |
| nes_video_sync_nametable_write | nametable/stitch | 5182 | 1.83 |
| nes_ppu_stage_nametable_hl | nametable/stitch | 4199 | 1.48 |
| nes_ppu_write_data | PPU reg emulation | 3693 | 1.31 |
| nes_video_wait_vram | idle/wait | 3346 | 1.18 |
| nes_video_flush_nametable_queue_atomic | nametable/stitch | 3116 | 1.10 |
| nes_oam_scan16_romx | OAM | 2903 | 1.03 |
| nes_controller_latch | controller | 2846 | 1.01 |
| nes_cpu_read_joy_hl | cpu read/write helpers | 2587 | 0.91 |
| nes_ppu_cpu_write | cpu read/write helpers | 2587 | 0.91 |
| nes_overlay_dispatch_hl | dispatch + bank switch | 2570 | 0.91 |
| nes_gbc_vblank_isr | video other | 2569 | 0.91 |
| nes_mmc1_write_romx | runtime other | 2517 | 0.89 |
| nes_ppu_map_nametable_hl | nametable/stitch | 2404 | 0.85 |
| nes_controller_read | controller | 2218 | 0.78 |
| nes_mmc1_apply | runtime other | 2215 | 0.78 |
| nes_mmc1_serial_write5 | runtime other | 2194 | 0.78 |
| nes_cpu_write | cpu read/write helpers | 1961 | 0.69 |
| nes_jump_known_hl_a | dispatch + bank switch | 1822 | 0.64 |
| nes_apu_sweep_target_p1 | APU | 1740 | 0.62 |
| nes_apu_sweep_target_p2 | APU | 1654 | 0.58 |
| nes_pace_swap | frame pacing | 1403 | 0.50 |
| nes_prg_read_hi_a | cpu read/write helpers | 1381 | 0.49 |

Hottest translated blocks: nes_E64E 4.4%, nes_m2_b05_8521 2.4%, nes_r_6E21 2.1%, nes_r_6D02 1.4%, nes_m2_b05_852F 1.3%, nes_FFAC 1.1%, nes_E536 1.0%, nes_E556 1.0%

## Contra

900 NES frames, 325600 GB T-cycles per NES frame (2.32 GB frames per NES frame, double speed).

| Bucket | T-cyc/frame | % |
|---|---|---|
| translated: core 6502 semantics | 124542 | 38.25 |
| translated: HRAM A/X/Y/SP save+reload | 35490 | 10.90 |
| runtime: cpu read/write helpers | 32777 | 10.07 |
| translated: flag materialize/reload | 26412 | 8.11 |
| runtime: OAM | 16112 | 4.95 |
| runtime: PPU reg emulation | 12628 | 3.88 |
| runtime: follow camera | 11449 | 3.52 |
| translated: NMI polls | 9379 | 2.88 |
| runtime: nametable/stitch | 9163 | 2.81 |
| translated: helper/dispatch/bank call setup | 7528 | 2.31 |
| runtime: APU | 7095 | 2.18 |
| runtime: idle/wait | 6879 | 2.11 |
| runtime: dispatch + bank switch | 6698 | 2.06 |
| runtime: video other | 5453 | 1.67 |
| translated: mapper2 dispatch stubs | 5262 | 1.62 |
| runtime: frame pacing | 3731 | 1.15 |
| runtime: controller | 3293 | 1.01 |
| runtime: NMI/vblank/ISR | 1065 | 0.33 |
| runtime: flag helpers | 571 | 0.18 |
| runtime: runtime other | 71 | 0.02 |
| 6502 busy-wait loops (spin) | 0 | 0.00 |

| Opcode x mode | % | NES instr/frame | GB instr/instr | T-cyc/instr | calls/instr |
|---|---|---|---|---|---|
| Rts Implied | 8.39 | 160 | 21.7 | 170.5 | 0.36 |
| Lda IndirectIndexed | 5.05 | 116 | 16.1 | 141.3 | 1.00 |
| Jsr Absolute | 4.20 | 164 | 9.8 | 83.3 | 0.03 |
| Lda AbsoluteX | 3.49 | 212 | 6.4 | 53.6 | 0.01 |
| Sta AbsoluteX | 3.38 | 152 | 8.8 | 72.6 | 0.01 |
| (block prologue)  | 2.88 | | | | |
| Adc Immediate | 2.85 | 77 | 18.6 | 120.1 | 0.00 |
| Beq Relative | 2.01 | 213 | 3.1 | 30.7 | 0.04 |
| Bpl Relative | 1.71 | 151 | 3.2 | 36.8 | 0.00 |
| Adc ZeroPage | 1.57 | 44 | 16.5 | 116.6 | 0.03 |
| Sta ZeroPage | 1.42 | 246 | 1.4 | 18.8 | 0.00 |
| Bne Relative | 1.35 | 140 | 3.2 | 31.4 | 0.04 |
| Dey Implied | 1.33 | 96 | 5.8 | 45.0 | 0.00 |
| Lda AbsoluteY | 1.19 | 50 | 7.7 | 77.4 | 0.71 |
| Bcc Relative | 1.09 | 82 | 4.7 | 43.7 | 0.06 |
| Dex Implied | 1.08 | 86 | 5.2 | 40.7 | 0.00 |
| Lda ZeroPage | 1.02 | 177 | 1.2 | 18.7 | 0.00 |
| Cmp Immediate | 0.91 | 77 | 4.7 | 38.3 | 0.00 |
| Rol ZeroPage | 0.84 | 46 | 6.1 | 59.3 | 0.00 |
| Lsr Accumulator | 0.71 | 154 | 2.1 | 15.0 | 0.00 |
| Bcs Relative | 0.71 | 66 | 3.5 | 34.8 | 0.11 |
| Ldy ZeroPage | 0.65 | 62 | 3.1 | 34.1 | 0.00 |
| Lda ZeroPageX | 0.64 | 49 | 5.6 | 42.3 | 0.00 |
| Sta AbsoluteY | 0.63 | 28 | 8.0 | 72.7 | 0.00 |
| Adc AbsoluteX | 0.63 | 14 | 21.6 | 142.5 | 0.00 |

| Translated overhead tag | T-cyc/frame | % |
|---|---|---|
| NMI poll (block prologue) | 9379 | 2.88 |
| HRAM save A | 6909 | 2.12 |
| helper call/jp instructions | 6612 | 2.03 |
| flag materialize N (store) | 6468 | 1.99 |
| flag materialize Z (store) | 6459 | 1.98 |
| HRAM reload X | 6304 | 1.94 |
| mapper2 dispatch stubs | 5262 | 1.62 |
| HRAM save SP | 4231 | 1.30 |
| HRAM reload SP | 4015 | 1.23 |
| push/pop af (flag/A preservation) | 3836 | 1.18 |
| HRAM save Y | 3794 | 1.17 |
| flag materialize C (store) | 3784 | 1.16 |
| HRAM reload Y | 3726 | 1.14 |
| HRAM reload A | 3557 | 1.09 |
| HRAM save X | 2954 | 0.91 |
| flag reload C | 1751 | 0.54 |
| flag reload P | 1669 | 0.51 |
| flag materialize P (store) | 1669 | 0.51 |
| cross-bank trampoline setup | 605 | 0.19 |
| flag reload Z | 578 | 0.18 |
| dispatch call setup | 310 | 0.10 |
| flag reload N | 198 | 0.06 |

| Runtime routine | bucket | T-cyc/frame | % |
|---|---|---|---|
| nes_view_follow_hinted | follow camera | 11057 | 3.40 |
| nes_cpu_read_hi_view | cpu read/write helpers | 9973 | 3.06 |
| nes_cpu_read_hi | cpu read/write helpers | 7839 | 2.41 |
| nes_ppu_write_data | PPU reg emulation | 6940 | 2.13 |
| nes_video_wait_vram | idle/wait | 6721 | 2.06 |
| nes_oam_dma_fast_unrolled | OAM | 5268 | 1.62 |
| nes_video_build_oam_shadow_fast | OAM | 5254 | 1.61 |
| nes_oam_scan16_romx | OAM | 4672 | 1.43 |
| nes_prg_read_hi_a | cpu read/write helpers | 4187 | 1.29 |
| nes_dispatch_hl | dispatch + bank switch | 3961 | 1.22 |
| nes_cpu_read | cpu read/write helpers | 3739 | 1.15 |
| nes_chr_ram_write_hl | PPU reg emulation | 3623 | 1.11 |
| nes_video_sync_nametable_write | nametable/stitch | 2750 | 0.84 |
| nes_cpu_write | cpu read/write helpers | 2705 | 0.83 |
| nes_gbc_vblank_isr | video other | 2672 | 0.82 |
| nes_ppu_cpu_write | cpu read/write helpers | 2311 | 0.71 |
| nes_ppu_increment_addr | PPU reg emulation | 1931 | 0.59 |
| nes_controller_read | controller | 1920 | 0.59 |
| nes_mapper2_conflict_read_hl | cpu read/write helpers | 1634 | 0.50 |
| nes_pace_swap | frame pacing | 1421 | 0.44 |
| nes_ppu_stage_nametable_hl | nametable/stitch | 1412 | 0.43 |
| nes_ppu_map_nametable_hl | nametable/stitch | 1309 | 0.40 |
| nes_pace_take_snapshot | frame pacing | 1259 | 0.39 |
| nes_video_flush_nametable_queue_atomic | nametable/stitch | 1071 | 0.33 |
| nes_controller_latch | controller | 1045 | 0.32 |

Hottest translated blocks: nes_m2_b01_B02A 4.7%, nes_m2_b01_AF5D 3.6%, nes_m2_b01_AED7 3.1%, nes_m2_b01_B025 1.9%, nes_D38D 1.5%, nes_E355 1.4%, nes_C13F 1.4%, nes_E42B 1.3%

## Castlevania

1000 NES frames, 394877 GB T-cycles per NES frame (2.82 GB frames per NES frame, double speed).

| Bucket | T-cyc/frame | % |
|---|---|---|
| translated: core 6502 semantics | 163922 | 41.51 |
| runtime: cpu read/write helpers | 40172 | 10.17 |
| translated: HRAM A/X/Y/SP save+reload | 39278 | 9.95 |
| translated: flag materialize/reload | 30124 | 7.63 |
| translated: sprite-0 poll wait | 25616 | 6.49 |
| runtime: OAM | 16047 | 4.06 |
| translated: NMI polls | 15750 | 3.99 |
| runtime: follow camera | 12463 | 3.16 |
| translated: helper/dispatch/bank call setup | 7716 | 1.95 |
| runtime: nametable/stitch | 6901 | 1.75 |
| runtime: dispatch + bank switch | 6826 | 1.73 |
| runtime: video other | 6143 | 1.56 |
| runtime: PPU reg emulation | 5837 | 1.48 |
| runtime: APU | 5616 | 1.42 |
| translated: mapper2 dispatch stubs | 4038 | 1.02 |
| runtime: frame pacing | 3468 | 0.88 |
| runtime: controller | 1652 | 0.42 |
| runtime: idle/wait | 1528 | 0.39 |
| runtime: NMI/vblank/ISR | 1066 | 0.27 |
| runtime: flag helpers | 579 | 0.15 |
| runtime: runtime other | 136 | 0.03 |

| Opcode x mode | % | NES instr/frame | GB instr/instr | T-cyc/instr | calls/instr |
|---|---|---|---|---|---|
| Lda Absolute | 11.51 | 284 | 16.9 | 159.9 | 0.03 |
| Rts Implied | 5.64 | 124 | 22.9 | 179.3 | 0.45 |
| Lda IndirectIndexed | 4.88 | 126 | 17.2 | 152.4 | 0.95 |
| (block prologue)  | 3.99 | | | | |
| Bne Relative | 3.80 | 476 | 3.2 | 31.5 | 0.01 |
| Lda AbsoluteX | 3.26 | 235 | 6.7 | 54.7 | 0.10 |
| Beq Relative | 2.92 | 368 | 3.3 | 31.4 | 0.00 |
| Cmp Immediate | 2.89 | 207 | 6.5 | 55.2 | 0.00 |
| Jsr Absolute | 2.85 | 130 | 10.1 | 86.7 | 0.06 |
| Sta AbsoluteX | 2.59 | 144 | 8.6 | 71.2 | 0.02 |
| Adc Immediate | 2.40 | 82 | 17.9 | 115.6 | 0.00 |
| And Immediate | 2.01 | 343 | 2.3 | 23.1 | 0.00 |
| Sta ZeroPage | 1.68 | 376 | 1.2 | 17.6 | 0.00 |
| Adc ZeroPage | 1.49 | 76 | 11.7 | 77.5 | 0.01 |
| Lda AbsoluteY | 1.44 | 84 | 6.8 | 67.7 | 0.60 |
| Inx Implied | 1.32 | 220 | 3.9 | 23.7 | 0.00 |
| Dey Implied | 1.11 | 93 | 5.9 | 46.8 | 0.00 |
| Bcs Relative | 1.06 | 112 | 3.7 | 37.2 | 0.02 |
| Bcc Relative | 1.02 | 129 | 3.4 | 31.1 | 0.01 |
| Lda ZeroPage | 0.96 | 193 | 1.3 | 19.6 | 0.00 |
| Cpx Immediate | 0.78 | 72 | 5.6 | 43.0 | 0.00 |
| Bpl Relative | 0.74 | 65 | 4.6 | 44.9 | 0.02 |
| Ldx ZeroPage | 0.61 | 93 | 2.5 | 25.9 | 0.00 |
| Iny Implied | 0.58 | 114 | 3.2 | 20.0 | 0.00 |
| Dec ZeroPage | 0.56 | 46 | 4.0 | 48.1 | 0.00 |

| Translated overhead tag | T-cyc/frame | % |
|---|---|---|
| sprite-0 / $2002 poll wait (inline expansion) | 25616 | 6.49 |
| NMI poll (block prologue) | 15750 | 3.99 |
| flag materialize Z (store) | 8215 | 2.08 |
| flag materialize N (store) | 7632 | 1.93 |
| helper call/jp instructions | 7073 | 1.79 |
| HRAM save A | 6930 | 1.75 |
| HRAM reload X | 6471 | 1.64 |
| HRAM save X | 5200 | 1.32 |
| HRAM reload A | 4853 | 1.23 |
| HRAM reload Y | 4608 | 1.17 |
| flag materialize C (store) | 4455 | 1.13 |
| mapper2 dispatch stubs | 4038 | 1.02 |
| HRAM save Y | 3868 | 0.98 |
| HRAM save SP | 3849 | 0.97 |
| HRAM reload SP | 3499 | 0.89 |
| push/pop af (flag/A preservation) | 2838 | 0.72 |
| flag reload P | 1826 | 0.46 |
| flag materialize P (store) | 1826 | 0.46 |
| flag reload C | 1593 | 0.40 |
| flag reload Z | 1537 | 0.39 |
| cross-bank trampoline setup | 328 | 0.08 |
| dispatch call setup | 315 | 0.08 |
| flag reload N | 202 | 0.05 |

| Runtime routine | bucket | T-cyc/frame | % |
|---|---|---|---|
| nes_view_follow_hinted | follow camera | 12071 | 3.06 |
| nes_cpu_read_hi_view | cpu read/write helpers | 11837 | 3.00 |
| nes_prg_read_hi_a | cpu read/write helpers | 6341 | 1.61 |
| nes_oam_dma_fast_unrolled | OAM | 5268 | 1.33 |
| nes_video_build_oam_shadow_fast | OAM | 5218 | 1.32 |
| nes_cpu_read_hi | cpu read/write helpers | 5190 | 1.31 |
| nes_ppu_write_data | PPU reg emulation | 4987 | 1.26 |
| nes_cpu_write | cpu read/write helpers | 4799 | 1.22 |
| nes_cpu_read | cpu read/write helpers | 4678 | 1.18 |
| nes_oam_scan16_romx | OAM | 4588 | 1.16 |
| nes_dispatch_hl | dispatch + bank switch | 4006 | 1.01 |
| nes_ppu_cpu_write | cpu read/write helpers | 3502 | 0.89 |
| nes_gbc_vblank_isr | video other | 3496 | 0.89 |
| nes_mapper2_conflict_read_hl | cpu read/write helpers | 3191 | 0.81 |
| nes_video_flush_nametable_queue_atomic | nametable/stitch | 2343 | 0.59 |
| nes_pace_swap | frame pacing | 1416 | 0.36 |
| nes_video_wait_vram | idle/wait | 1392 | 0.35 |
| nes_jmp_indirect_hl | dispatch + bank switch | 1173 | 0.30 |
| nes_video_sync_nametable_write | nametable/stitch | 1158 | 0.29 |
| nes_pace_take_snapshot | frame pacing | 1110 | 0.28 |
| nes_restore_code_bank | dispatch + bank switch | 1035 | 0.26 |
| nes_video_refresh_stitch_column | nametable/stitch | 990 | 0.25 |
| nes_controller_read | controller | 960 | 0.24 |
| nes_ppu_increment_addr | PPU reg emulation | 847 | 0.21 |
| nes_poll_nmi_hl | NMI/vblank/ISR | 730 | 0.18 |

Hottest translated blocks: nes_F8CC 9.7%, nes_m2_b00_80B6 2.8%, nes_F8C9 2.7%, nes_F8DF 2.5%, nes_F8D8 2.2%, nes_m2_b00_8072 2.1%, nes_C1D8 1.6%, nes_CA6D 1.6%

## MegaMan

1000 NES frames, 495992 GB T-cycles per NES frame (3.53 GB frames per NES frame, double speed).

| Bucket | T-cyc/frame | % |
|---|---|---|
| translated: core 6502 semantics | 230417 | 46.46 |
| translated: HRAM A/X/Y/SP save+reload | 44832 | 9.04 |
| 6502 busy-wait loops (spin) | 43330 | 8.74 |
| translated: flag materialize/reload | 38949 | 7.85 |
| runtime: cpu read/write helpers | 36506 | 7.36 |
| runtime: APU | 28536 | 5.75 |
| translated: mapper2 dispatch stubs | 18123 | 3.65 |
| runtime: OAM | 11373 | 2.29 |
| translated: NMI polls | 9551 | 1.93 |
| translated: helper/dispatch/bank call setup | 6393 | 1.29 |
| runtime: follow camera | 6186 | 1.25 |
| runtime: video other | 5511 | 1.11 |
| runtime: dispatch + bank switch | 5384 | 1.09 |
| runtime: flag helpers | 4765 | 0.96 |
| runtime: NMI/vblank/ISR | 3223 | 0.65 |
| runtime: controller | 1652 | 0.33 |
| runtime: nametable/stitch | 1027 | 0.21 |
| runtime: PPU reg emulation | 124 | 0.02 |
| runtime: runtime other | 57 | 0.01 |
| runtime: idle/wait | 50 | 0.01 |
| runtime: frame pacing | 2 | 0.00 |

| Opcode x mode | % | NES instr/frame | GB instr/instr | T-cyc/instr | calls/instr |
|---|---|---|---|---|---|
| Lda IndirectIndexed | 7.02 | 280 | 16.6 | 124.3 | 0.24 |
| Rts Implied | 4.75 | 160 | 19.4 | 147.5 | 0.22 |
| Adc IndirectIndexed | 4.40 | 90 | 33.4 | 241.0 | 0.00 |
| Sta IndirectIndexed | 4.17 | 136 | 19.2 | 152.1 | 0.00 |
| Beq Relative | 3.29 | 995 | 1.9 | 16.4 | 0.01 |
| Bne Relative | 2.93 | 356 | 4.4 | 40.8 | 0.00 |
| Jsr Absolute | 2.70 | 160 | 9.9 | 83.7 | 0.04 |
| Cmp ZeroPage | 2.67 | 200 | 8.0 | 66.2 | 0.00 |
| Lda AbsoluteY | 2.00 | 123 | 9.9 | 80.2 | 0.33 |
| (block prologue)  | 1.93 | | | | |
| Ora IndirectIndexed | 1.92 | 55 | 23.0 | 171.8 | 0.00 |
| Adc ZeroPage | 1.82 | 80 | 16.9 | 112.3 | 0.00 |
| Sta AbsoluteX | 1.70 | 103 | 9.3 | 82.0 | 0.26 |
| Cmp Immediate | 1.69 | 155 | 6.2 | 54.0 | 0.00 |
| Lda ZeroPage | 1.63 | 1075 | 0.5 | 7.5 | 0.00 |
| Lda AbsoluteX | 1.44 | 180 | 4.7 | 39.7 | 0.10 |
| Sta ZeroPage | 1.38 | 383 | 1.2 | 17.9 | 0.00 |
| Sta AbsoluteY | 1.21 | 148 | 5.0 | 40.6 | 0.00 |
| Bcc Relative | 1.07 | 180 | 2.9 | 29.6 | 0.00 |
| Adc Immediate | 1.06 | 49 | 16.9 | 108.2 | 0.00 |
| Ldy Immediate | 0.88 | 282 | 2.3 | 15.4 | 0.00 |
| Dex Implied | 0.86 | 141 | 4.0 | 30.3 | 0.00 |
| Cmp IndirectIndexed | 0.85 | 25 | 24.2 | 171.2 | 0.00 |
| Sbc Immediate | 0.73 | 34 | 16.9 | 105.8 | 0.00 |
| Iny Implied | 0.67 | 478 | 1.4 | 6.9 | 0.00 |

| Translated overhead tag | T-cyc/frame | % |
|---|---|---|
| 6502 busy-wait loops (spin) | 43330 | 8.74 |
| mapper2 dispatch stubs | 18123 | 3.65 |
| push/pop af (flag/A preservation) | 9739 | 1.96 |
| HRAM save A | 9640 | 1.94 |
| NMI poll (block prologue) | 9551 | 1.93 |
| flag materialize C (store) | 8085 | 1.63 |
| HRAM reload A | 6957 | 1.40 |
| helper call/jp instructions | 6089 | 1.23 |
| flag materialize Z (store) | 5943 | 1.20 |
| HRAM reload Y | 5934 | 1.20 |
| flag materialize N (store) | 5115 | 1.03 |
| HRAM save Y | 5066 | 1.02 |
| HRAM save SP | 4663 | 0.94 |
| HRAM reload X | 4412 | 0.89 |
| HRAM reload SP | 4165 | 0.84 |
| HRAM save X | 3995 | 0.81 |
| flag reload C | 3134 | 0.63 |
| flag reload P | 2321 | 0.47 |
| flag materialize P (store) | 2321 | 0.47 |
| flag reload Z | 2087 | 0.42 |

| Runtime routine | bucket | T-cyc/frame | % |
|---|---|---|---|
| nes_cpu_write | cpu read/write helpers | 10548 | 2.13 |
| nes_cpu_read_hi | cpu read/write helpers | 9331 | 1.88 |
| nes_prg_read_hi_a | cpu read/write helpers | 6723 | 1.36 |
| nes_view_follow_hinted | follow camera | 5990 | 1.21 |
| nes_video_build_oam_shadow_fast | OAM | 5345 | 1.08 |
| nes_oam_dma_fast_unrolled | OAM | 5268 | 1.06 |
| nes_apu_write_banked | APU | 4119 | 0.83 |
| nes_mapper2_conflict_read_hl | cpu read/write helpers | 3973 | 0.80 |
| nes_gbc_vblank_isr | video other | 3927 | 0.79 |
| nes_apu_clock_triangle_linear | APU | 2928 | 0.59 |
| nes_materialize_p | flag helpers | 2642 | 0.53 |
| nes_dispatch_hl | dispatch + bank switch | 2600 | 0.52 |
| nes_poll_nmi_hl | NMI/vblank/ISR | 2306 | 0.46 |
| nes_set_p_from_a | flag helpers | 2122 | 0.43 |
| nes_apu_frame_step | APU | 1947 | 0.39 |
| nes_apu_timer_to_period | APU | 1816 | 0.37 |
| nes_apu_sweep_target_p2 | APU | 1747 | 0.35 |
| nes_ppu_cpu_write | cpu read/write helpers | 1669 | 0.34 |
| nes_apu_sweep_target_p1 | APU | 1652 | 0.33 |
| nes_apu_clock_lengths | APU | 1626 | 0.33 |
| nes_apu_refresh_p2 | APU | 1513 | 0.31 |
| nes_apu_refresh_p1 | APU | 1491 | 0.30 |
| nes_restore_code_bank | dispatch + bank switch | 1326 | 0.27 |
| nes_apu_update_triangle | APU | 1319 | 0.27 |
| nes_cpu_read_joy_hl | cpu read/write helpers | 1120 | 0.23 |

Hottest translated blocks: nes_m2_b04_96E8 2.9%, nes_D47A 2.5%, nes_m2_b04_94B3 2.2%, nes_D242 2.0%, nes_m2_b04_9256 2.0%, nes_m2_b04_9281 1.6%, nes_m2_b04_9460 1.3%, nes_m2_b05_98F2 1.2%

## SMB

600 NES frames, 156224 GB T-cycles per NES frame (1.11 GB frames per NES frame, double speed).

| Bucket | T-cyc/frame | % |
|---|---|---|
| translated: core 6502 semantics | 50923 | 32.60 |
| 6502 busy-wait loops (spin) | 36105 | 23.11 |
| translated: HRAM A/X/Y/SP save+reload | 13571 | 8.69 |
| runtime: OAM | 10345 | 6.62 |
| runtime: APU | 8369 | 5.36 |
| runtime: nametable/stitch | 5588 | 3.58 |
| runtime: video other | 4774 | 3.06 |
| translated: flag materialize/reload | 4478 | 2.87 |
| runtime: PPU reg emulation | 4084 | 2.61 |
| runtime: cpu read/write helpers | 3882 | 2.49 |
| translated: helper/dispatch/bank call setup | 2675 | 1.71 |
| translated: unlabeled | 1931 | 1.24 |
| runtime: idle/wait | 1918 | 1.23 |
| runtime: controller | 1592 | 1.02 |
| translated: NMI polls | 1568 | 1.00 |
| runtime: NMI/vblank/ISR | 978 | 0.63 |
| runtime: dispatch + bank switch | 948 | 0.61 |
| runtime: runtime other | 899 | 0.58 |
| runtime: frame pacing | 742 | 0.47 |
| runtime: follow camera | 559 | 0.36 |
| runtime: flag helpers | 296 | 0.19 |

| Opcode x mode | % | NES instr/frame | GB instr/instr | T-cyc/instr | calls/instr |
|---|---|---|---|---|---|
| (block prologue)  | 10.27 | | | | |
| Rts Implied | 5.79 | 63 | 17.4 | 143.3 | 0.37 |
| Jsr Absolute | 4.75 | 76 | 11.6 | 97.6 | 0.59 |
| Iny Implied | 1.88 | 213 | 2.0 | 13.8 | 0.00 |
| Bne Relative | 1.84 | 149 | 2.1 | 19.3 | 0.02 |
| Sta AbsoluteY | 1.84 | 56 | 5.9 | 51.3 | 0.00 |
| Beq Relative | 1.36 | 92 | 2.4 | 23.3 | 0.15 |
| (unlabeled translated)  | 1.24 | | | | |
| Cmp Immediate | 1.23 | 71 | 3.5 | 27.2 | 0.01 |
| Lda Absolute | 1.18 | 93 | 1.4 | 19.8 | 0.00 |
| Lda AbsoluteX | 1.10 | 29 | 7.2 | 58.4 | 0.07 |
| Sta Absolute | 0.88 | 43 | 2.7 | 31.9 | 0.51 |
| Lda ZeroPageX | 0.83 | 32 | 5.4 | 41.2 | 0.00 |
| Sta AbsoluteX | 0.71 | 17 | 8.3 | 66.0 | 0.01 |
| Lda IndirectIndexed | 0.70 | 7 | 21.4 | 158.1 | 0.03 |
| NativeLoop cmp_add_wrap | 0.67 | 1 | 178.0 | 1332.0 | 0.00 |
| Sta ZeroPage | 0.58 | 49 | 1.3 | 18.4 | 0.00 |
| Pla Implied | 0.55 | 13 | 8.2 | 66.8 | 0.00 |
| Lda AbsoluteY | 0.52 | 15 | 7.0 | 54.2 | 0.01 |
| NativeLoop dec_nonzero | 0.49 | 1 | 147.2 | 1169.8 | 1.00 |
| Lda ZeroPage | 0.49 | 44 | 1.1 | 17.2 | 0.00 |
| Pha Implied | 0.45 | 12 | 8.2 | 58.1 | 0.00 |
| Dex Implied | 0.44 | 27 | 3.9 | 25.1 | 0.00 |
| Bpl Relative | 0.39 | 25 | 2.2 | 24.7 | 0.00 |
| Dey Implied | 0.39 | 15 | 5.5 | 41.0 | 0.00 |

| Translated overhead tag | T-cyc/frame | % |
|---|---|---|
| 6502 busy-wait loops (spin) | 36105 | 23.11 |
| HRAM reload SP | 2220 | 1.42 |
| helper call/jp instructions | 2216 | 1.42 |
| HRAM save SP | 2025 | 1.30 |
| HRAM save Y | 1826 | 1.17 |
| HRAM save A | 1748 | 1.12 |
| HRAM reload A | 1730 | 1.11 |
| NMI poll (block prologue) | 1568 | 1.00 |
| HRAM reload Y | 1497 | 0.96 |
| HRAM reload X | 1427 | 0.91 |
| flag materialize Z (store) | 1308 | 0.84 |
| HRAM save X | 1099 | 0.70 |
| flag materialize N (store) | 1036 | 0.66 |
| flag materialize C (store) | 776 | 0.50 |
| push/pop af (flag/A preservation) | 551 | 0.35 |
| cross-bank trampoline setup | 459 | 0.29 |
| flag reload Z | 394 | 0.25 |
| flag reload C | 303 | 0.19 |

| Runtime routine | bucket | T-cyc/frame | % |
|---|---|---|---|
| nes_oam_dma_fast_unrolled | OAM | 5268 | 3.37 |
| nes_video_build_oam_shadow_fast | OAM | 4332 | 2.77 |
| nes_ppu_write_data | PPU reg emulation | 2761 | 1.77 |
| nes_ppu_cpu_write | cpu read/write helpers | 2638 | 1.69 |
| nes_gbc_vblank_isr | video other | 1831 | 1.17 |
| nes_video_wait_vram | idle/wait | 1615 | 1.03 |
| nes_video_sync_nametable_write | nametable/stitch | 1343 | 0.86 |
| nes_apu_sweep_target_p1 | APU | 1245 | 0.80 |
| nes_video_rebuild_generic_maps_atomic | video other | 1115 | 0.71 |
| nes_video_flush_nametable_queue_atomic | nametable/stitch | 1013 | 0.65 |
| nes_apu_sweep_target_p2 | APU | 935 | 0.60 |
| nes_apu_write_banked | APU | 871 | 0.56 |
| nes_native_88D0 | runtime other | 867 | 0.55 |
| nes_apu_clock_triangle_linear | APU | 797 | 0.51 |
| nes_joy_serial_loop | controller | 788 | 0.50 |
| nes_apu_clock_lengths | APU | 713 | 0.46 |
| nes_poll_nmi_hl | NMI/vblank/ISR | 703 | 0.45 |
| nes_video_refresh_stitch_column | nametable/stitch | 700 | 0.45 |
| nes_video_sync_oam_hw | OAM | 698 | 0.45 |
| nes_ppu_increment_addr | PPU reg emulation | 681 | 0.44 |
| nes_jump_known_hl_a_8bit | dispatch + bank switch | 680 | 0.44 |
| RAM code near nes_gbc_palette_shadow | video other | 652 | 0.42 |
| nes_ppu_write_run | PPU reg emulation | 600 | 0.38 |
| nes_apu_frame_step | APU | 564 | 0.36 |
| nes_controller_latch | controller | 520 | 0.33 |

Hottest translated blocks: nes_8227 4.8%, nes_E3F0_trace 2.1%, nes_C144 1.8%, nes_F282 1.6%, nes_F1C0_trace 1.2%, nes_8F9E_trace 0.7%, nes_81CF 0.7%, nes_E29C 0.6%

## DK

600 NES frames, 151982 GB T-cycles per NES frame (1.08 GB frames per NES frame, double speed).

| Bucket | T-cyc/frame | % |
|---|---|---|
| translated: core 6502 semantics | 79570 | 52.35 |
| translated: HRAM A/X/Y/SP save+reload | 13740 | 9.04 |
| runtime: OAM | 10178 | 6.70 |
| runtime: follow camera | 7477 | 4.92 |
| translated: flag materialize/reload | 7414 | 4.88 |
| runtime: APU | 6557 | 4.31 |
| runtime: nametable/stitch | 5029 | 3.31 |
| runtime: video other | 4551 | 2.99 |
| runtime: cpu read/write helpers | 2725 | 1.79 |
| translated: NMI polls | 2468 | 1.62 |
| runtime: idle/wait | 2356 | 1.55 |
| runtime: PPU reg emulation | 2149 | 1.41 |
| translated: unlabeled | 1826 | 1.20 |
| translated: helper/dispatch/bank call setup | 1799 | 1.18 |
| runtime: controller | 1644 | 1.08 |
| runtime: dispatch + bank switch | 1058 | 0.70 |
| runtime: NMI/vblank/ISR | 961 | 0.63 |
| runtime: flag helpers | 321 | 0.21 |
| runtime: frame pacing | 142 | 0.09 |
| runtime: runtime other | 19 | 0.01 |

| Opcode x mode | % | NES instr/frame | GB instr/instr | T-cyc/instr | calls/instr |
|---|---|---|---|---|---|
| Ror ZeroPage | 25.11 | 636 | 7.0 | 60.0 | 0.00 |
| Rts Implied | 8.38 | 106 | 14.7 | 120.4 | 0.16 |
| Jsr Absolute | 7.65 | 122 | 10.7 | 95.2 | 0.20 |
| (block prologue)  | 5.08 | | | | |
| Eor ZeroPage | 2.51 | 80 | 6.0 | 48.0 | 0.00 |
| Lda ZeroPage | 2.32 | 217 | 1.0 | 16.2 | 0.00 |
| Lda AbsoluteX | 1.73 | 26 | 11.5 | 102.9 | 0.63 |
| Sta ZeroPage | 1.64 | 127 | 1.8 | 19.7 | 0.00 |
| Beq Relative | 1.42 | 123 | 1.6 | 17.6 | 0.01 |
| (unlabeled translated)  | 1.20 | | | | |
| Bne Relative | 1.11 | 76 | 2.3 | 22.1 | 0.09 |
| Clc Implied | 1.05 | 81 | 2.0 | 19.8 | 0.00 |
| And Immediate | 0.94 | 167 | 1.1 | 8.6 | 0.00 |
| Dey Implied | 0.77 | 22 | 7.2 | 52.3 | 0.00 |
| Lda IndirectIndexed | 0.77 | 6 | 23.9 | 182.8 | 0.00 |
| Cmp Immediate | 0.73 | 43 | 3.3 | 26.1 | 0.00 |
| Pla Implied | 0.68 | 22 | 6.5 | 47.8 | 0.00 |
| Lsr Accumulator | 0.66 | 48 | 2.6 | 21.0 | 0.00 |
| Sec Implied | 0.52 | 40 | 2.0 | 19.9 | 0.00 |
| Lda ZeroPageX | 0.50 | 14 | 7.4 | 55.5 | 0.00 |
| Sta Absolute | 0.43 | 19 | 2.8 | 34.6 | 0.79 |
| Dex Implied | 0.33 | 17 | 4.1 | 29.1 | 0.00 |
| Rol Accumulator | 0.30 | 16 | 4.0 | 27.9 | 0.00 |
| Sta IndirectIndexed | 0.29 | 3 | 19.3 | 154.3 | 0.00 |
| Bpl Relative | 0.29 | 17 | 2.2 | 26.1 | 0.00 |

| Translated overhead tag | T-cyc/frame | % |
|---|---|---|
| HRAM reload SP | 3226 | 2.12 |
| HRAM save SP | 3104 | 2.04 |
| HRAM save A | 3080 | 2.03 |
| flag materialize C (store) | 2872 | 1.89 |
| NMI poll (block prologue) | 2468 | 1.62 |
| HRAM reload A | 1850 | 1.22 |
| flag materialize Z (store) | 1460 | 0.96 |
| helper call/jp instructions | 1374 | 0.90 |
| flag reload C | 1223 | 0.81 |
| flag materialize N (store) | 1210 | 0.80 |
| HRAM save X | 825 | 0.54 |
| HRAM save Y | 567 | 0.37 |
| HRAM reload Y | 550 | 0.36 |
| HRAM reload X | 538 | 0.35 |
| cross-bank trampoline setup | 425 | 0.28 |
| flag reload Z | 350 | 0.23 |
| push/pop af (flag/A preservation) | 245 | 0.16 |

| Runtime routine | bucket | T-cyc/frame | % |
|---|---|---|---|
| nes_view_follow_update_romx | follow camera | 7129 | 4.69 |
| nes_oam_dma_fast_unrolled | OAM | 5268 | 3.47 |
| nes_video_build_oam_shadow_fast | OAM | 4161 | 2.74 |
| nes_video_wait_vram | idle/wait | 1913 | 1.26 |
| nes_ppu_write_data | PPU reg emulation | 1682 | 1.11 |
| nes_video_rebuild_generic_maps_atomic | video other | 1675 | 1.10 |
| nes_video_sync_nametable_write | nametable/stitch | 1558 | 1.02 |
| nes_gbc_vblank_isr | video other | 1366 | 0.90 |
| nes_cpu_read_joy_hl | cpu read/write helpers | 1120 | 0.74 |
| nes_ppu_cpu_write | cpu read/write helpers | 1024 | 0.67 |
| nes_apu_sweep_target_p1 | APU | 979 | 0.64 |
| nes_controller_read | controller | 960 | 0.63 |
| nes_ppu_map_nametable_hl | nametable/stitch | 904 | 0.59 |
| nes_apu_clock_triangle_linear | APU | 783 | 0.52 |
| nes_video_sync_oam_hw | OAM | 708 | 0.47 |
| nes_poll_nmi_hl | NMI/vblank/ISR | 698 | 0.46 |
| nes_apu_clock_lengths | APU | 663 | 0.44 |
| RAM code near nes_gbc_palette_shadow | video other | 661 | 0.43 |
| nes_jump_known_hl_a_8bit | dispatch + bank switch | 598 | 0.39 |
| nes_apu_sweep_target_p2 | APU | 594 | 0.39 |
| nes_apu_frame_step | APU | 564 | 0.37 |
| nes_controller_latch | controller | 520 | 0.34 |
| nes_video_sync_attribute_write_physical | nametable/stitch | 486 | 0.32 |
| nes_ppu_increment_addr | PPU reg emulation | 457 | 0.30 |
| nes_video_wait_oam | idle/wait | 443 | 0.29 |

Hottest translated blocks: nes_F4FD 31.2%, nes_F4ED 7.6%, nes_C7E1 6.1%, nes_F524 3.3%, nes_EFF5_trace 2.6%, nes_F4B8_trace 0.6%, nes_F4FC_trace 0.5%, nes_F522 0.5%

## IC

600 NES frames, 156237 GB T-cycles per NES frame (1.11 GB frames per NES frame, double speed).

| Bucket | T-cyc/frame | % |
|---|---|---|
| translated: core 6502 semantics | 66359 | 42.47 |
| translated: HRAM A/X/Y/SP save+reload | 22420 | 14.35 |
| translated: flag materialize/reload | 13572 | 8.69 |
| runtime: OAM | 10261 | 6.57 |
| runtime: nametable/stitch | 7910 | 5.06 |
| runtime: APU | 6113 | 3.91 |
| translated: NMI polls | 4931 | 3.16 |
| runtime: video other | 4203 | 2.69 |
| runtime: cpu read/write helpers | 3956 | 2.53 |
| runtime: PPU reg emulation | 3577 | 2.29 |
| runtime: idle/wait | 3377 | 2.16 |
| translated: unlabeled | 2674 | 1.71 |
| translated: helper/dispatch/bank call setup | 2135 | 1.37 |
| runtime: controller | 1644 | 1.05 |
| runtime: dispatch + bank switch | 1198 | 0.77 |
| runtime: NMI/vblank/ISR | 944 | 0.60 |
| runtime: flag helpers | 478 | 0.31 |
| runtime: follow camera | 354 | 0.23 |
| runtime: frame pacing | 100 | 0.06 |
| runtime: runtime other | 30 | 0.02 |

| Opcode x mode | % | NES instr/frame | GB instr/instr | T-cyc/instr | calls/instr |
|---|---|---|---|---|---|
| Ror ZeroPageX | 9.58 | 139 | 16.0 | 108.0 | 0.00 |
| Dey Implied | 8.28 | 188 | 8.8 | 69.0 | 0.00 |
| Rts Implied | 5.26 | 64 | 15.6 | 127.7 | 0.17 |
| Sta IndirectIndexed | 4.37 | 43 | 19.8 | 157.4 | 0.00 |
| (block prologue)  | 4.00 | | | | |
| Jsr Absolute | 3.25 | 66 | 9.4 | 77.1 | 0.13 |
| Bne Relative | 3.16 | 263 | 2.2 | 18.7 | 0.00 |
| Lda ZeroPage | 2.54 | 138 | 2.3 | 28.7 | 0.00 |
| Lda AbsoluteX | 1.95 | 30 | 11.8 | 102.5 | 0.54 |
| Ror ZeroPage | 1.76 | 55 | 5.3 | 49.7 | 0.00 |
| (unlabeled translated)  | 1.71 | | | | |
| Lda IndirectIndexed | 1.69 | 19 | 19.8 | 141.8 | 0.00 |
| Sta ZeroPage | 1.36 | 111 | 1.6 | 19.2 | 0.00 |
| Lsr ZeroPage | 1.34 | 36 | 6.8 | 58.7 | 0.00 |
| Dex Implied | 1.31 | 63 | 4.6 | 32.8 | 0.00 |
| Beq Relative | 1.28 | 89 | 2.2 | 22.4 | 0.09 |
| Eor ZeroPage | 1.28 | 28 | 8.0 | 72.0 | 0.00 |
| Pla Implied | 1.28 | 41 | 6.4 | 48.4 | 0.00 |
| Lda ZeroPageX | 1.00 | 32 | 6.7 | 49.1 | 0.00 |
| Pha Implied | 0.96 | 24 | 8.9 | 61.7 | 0.00 |
| Lsr Accumulator | 0.80 | 52 | 3.2 | 24.1 | 0.00 |
| Sta AbsoluteX | 0.70 | 9 | 15.6 | 120.1 | 0.01 |
| And Immediate | 0.68 | 80 | 1.5 | 13.4 | 0.00 |
| Bpl Relative | 0.65 | 43 | 2.1 | 24.1 | 0.00 |
| Iny Implied | 0.64 | 91 | 2.0 | 11.1 | 0.00 |

| Translated overhead tag | T-cyc/frame | % |
|---|---|---|
| NMI poll (block prologue) | 4931 | 3.16 |
| HRAM save X | 3404 | 2.18 |
| HRAM reload Y | 3331 | 2.13 |
| HRAM save Y | 3308 | 2.12 |
| flag materialize C (store) | 3208 | 2.05 |
| flag materialize Z (store) | 3044 | 1.95 |
| HRAM reload X | 2870 | 1.84 |
| HRAM save A | 2712 | 1.74 |
| HRAM save SP | 2574 | 1.65 |
| flag materialize N (store) | 2391 | 1.53 |
| HRAM reload SP | 2365 | 1.51 |
| flag reload C | 2256 | 1.44 |
| helper call/jp instructions | 1956 | 1.25 |
| HRAM reload A | 1855 | 1.19 |
| push/pop af (flag/A preservation) | 1540 | 0.99 |
| flag reload Z | 786 | 0.50 |
| cross-bank trampoline setup | 147 | 0.09 |
| flag reload N | 124 | 0.08 |
| flag reload P | 112 | 0.07 |
| flag materialize P (store) | 112 | 0.07 |

| Runtime routine | bucket | T-cyc/frame | % |
|---|---|---|---|
| nes_oam_dma_fast_unrolled | OAM | 5268 | 3.37 |
| nes_video_build_oam_shadow_fast | OAM | 4242 | 2.72 |
| nes_video_wait_vram | idle/wait | 2965 | 1.90 |
| nes_ppu_write_data | PPU reg emulation | 2833 | 1.81 |
| nes_video_sync_nametable_write | nametable/stitch | 2569 | 1.64 |
| nes_ppu_cpu_write | cpu read/write helpers | 2070 | 1.33 |
| nes_ppu_map_nametable_hl | nametable/stitch | 1451 | 0.93 |
| nes_gbc_vblank_isr | video other | 1392 | 0.89 |
| nes_cpu_read_joy_hl | cpu read/write helpers | 1120 | 0.72 |
| nes_video_rebuild_generic_maps_atomic | video other | 1117 | 0.71 |
| nes_controller_read | controller | 960 | 0.61 |
| nes_apu_clock_triangle_linear | APU | 803 | 0.51 |
| nes_video_mirror_vertical_seam_tile_safe | nametable/stitch | 767 | 0.49 |
| nes_ppu_increment_addr | PPU reg emulation | 740 | 0.47 |
| nes_video_sync_oam_hw | OAM | 709 | 0.45 |
| nes_ppu_stage_nametable_hl | nametable/stitch | 709 | 0.45 |
| nes_apu_clock_lengths | APU | 698 | 0.45 |
| nes_poll_nmi_hl | NMI/vblank/ISR | 681 | 0.44 |
| nes_apu_sweep_target_p1 | APU | 666 | 0.43 |
| RAM code near nes_gbc_palette_shadow | video other | 662 | 0.42 |
| nes_apu_sweep_target_p2 | APU | 609 | 0.39 |
| nes_video_flush_nametable_queue_atomic | nametable/stitch | 573 | 0.37 |
| nes_apu_frame_step | APU | 564 | 0.36 |
| nes_controller_latch | controller | 520 | 0.33 |
| nes_dispatch_hl | dispatch + bank switch | 506 | 0.32 |

Hottest translated blocks: nes_CAE6 20.9%, nes_C7A5_trace 3.6%, nes_CB02 3.3%, nes_C77B 3.0%, nes_CAD8 2.9%, nes_CACE 2.1%, nes_CAEC_trace 2.1%, nes_C7FD_trace 1.8%

## BF

600 NES frames, 140428 GB T-cycles per NES frame (1.00 GB frames per NES frame, double speed).

| Bucket | T-cyc/frame | % |
|---|---|---|
| translated: core 6502 semantics | 74543 | 53.08 |
| translated: HRAM A/X/Y/SP save+reload | 16242 | 11.57 |
| runtime: OAM | 11875 | 8.46 |
| translated: flag materialize/reload | 10741 | 7.65 |
| runtime: APU | 7788 | 5.55 |
| runtime: nametable/stitch | 4428 | 3.15 |
| translated: NMI polls | 3956 | 2.82 |
| runtime: video other | 2252 | 1.60 |
| runtime: PPU reg emulation | 1744 | 1.24 |
| runtime: cpu read/write helpers | 1581 | 1.13 |
| runtime: controller | 1430 | 1.02 |
| runtime: NMI/vblank/ISR | 928 | 0.66 |
| translated: helper/dispatch/bank call setup | 827 | 0.59 |
| runtime: dispatch + bank switch | 688 | 0.49 |
| runtime: follow camera | 537 | 0.38 |
| runtime: flag helpers | 279 | 0.20 |
| runtime: idle/wait | 211 | 0.15 |
| translated: unlabeled | 180 | 0.13 |
| runtime: runtime other | 119 | 0.08 |
| 6502 busy-wait loops (spin) | 78 | 0.06 |

| Opcode x mode | % | NES instr/frame | GB instr/instr | T-cyc/instr | calls/instr |
|---|---|---|---|---|---|
| Sta IndirectIndexed | 9.06 | 82 | 19.2 | 155.2 | 0.00 |
| Rts Implied | 6.55 | 68 | 16.7 | 134.5 | 0.10 |
| Lda IndirectIndexed | 5.01 | 35 | 25.8 | 200.8 | 0.00 |
| Jsr Absolute | 3.85 | 68 | 9.5 | 79.0 | 0.06 |
| Lda ZeroPageX | 3.42 | 94 | 6.4 | 51.3 | 0.00 |
| Lda AbsoluteX | 3.20 | 63 | 8.2 | 71.0 | 0.11 |
| Adc IndirectIndexed | 3.03 | 16 | 35.0 | 272.0 | 0.00 |
| (block prologue)  | 2.91 | | | | |
| Dey Implied | 2.61 | 124 | 4.1 | 29.5 | 0.00 |
| Adc AbsoluteX | 2.19 | 29 | 17.7 | 106.8 | 0.00 |
| Dex Implied | 2.07 | 48 | 7.2 | 60.1 | 0.00 |
| Cmp Immediate | 1.86 | 70 | 4.6 | 37.3 | 0.00 |
| Bpl Relative | 1.80 | 100 | 2.3 | 25.4 | 0.00 |
| Bmi Relative | 1.67 | 79 | 2.7 | 29.7 | 0.00 |
| Bne Relative | 1.50 | 111 | 1.9 | 19.0 | 0.01 |
| Beq Relative | 1.39 | 82 | 2.5 | 24.0 | 0.00 |
| Sta ZeroPage | 1.32 | 108 | 1.1 | 17.0 | 0.00 |
| Lda ZeroPage | 1.29 | 105 | 1.1 | 17.3 | 0.00 |
| Inc ZeroPage | 1.25 | 37 | 3.9 | 46.9 | 0.00 |
| Pla Implied | 1.12 | 18 | 11.5 | 88.8 | 0.00 |
| Lda AbsoluteY | 1.05 | 25 | 6.8 | 57.9 | 0.00 |
| Bcc Relative | 0.89 | 71 | 1.8 | 17.6 | 0.00 |
| Adc AbsoluteY | 0.87 | 5 | 31.7 | 230.2 | 0.00 |
| Cmp AbsoluteY | 0.86 | 12 | 13.8 | 102.0 | 0.00 |
| Sta AbsoluteX | 0.83 | 20 | 7.9 | 59.3 | 0.01 |

| Translated overhead tag | T-cyc/frame | % |
|---|---|---|
| NMI poll (block prologue) | 3956 | 2.82 |
| HRAM reload X | 3364 | 2.40 |
| HRAM save A | 3088 | 2.20 |
| push/pop af (flag/A preservation) | 3071 | 2.19 |
| flag materialize Z (store) | 2410 | 1.72 |
| flag materialize N (store) | 2205 | 1.57 |
| HRAM save SP | 2093 | 1.49 |
| HRAM reload Y | 1970 | 1.40 |
| HRAM reload SP | 1851 | 1.32 |
| flag materialize C (store) | 1786 | 1.27 |
| HRAM reload A | 1574 | 1.12 |
| HRAM save Y | 1289 | 0.92 |
| HRAM save X | 1014 | 0.72 |
| helper call/jp instructions | 710 | 0.51 |
| flag reload Z | 590 | 0.42 |
| flag reload C | 587 | 0.42 |
| cross-bank trampoline setup | 118 | 0.08 |
| flag reload N | 91 | 0.06 |
| 6502 busy-wait loops (spin) | 78 | 0.06 |

| Runtime routine | bucket | T-cyc/frame | % |
|---|---|---|---|
| nes_video_build_oam_shadow_fast | OAM | 5848 | 4.16 |
| nes_oam_dma_fast_unrolled | OAM | 5267 | 3.75 |
| nes_apu_sweep_target_p2 | APU | 1201 | 0.86 |
| nes_gbc_vblank_isr | video other | 1156 | 0.82 |
| nes_video_sync_nametable_write | nametable/stitch | 1077 | 0.77 |
| nes_ppu_stage_nametable_hl | nametable/stitch | 968 | 0.69 |
| nes_apu_sweep_target_p1 | APU | 956 | 0.68 |
| nes_ppu_write_data | PPU reg emulation | 918 | 0.65 |
| nes_video_flush_nametable_queue_atomic | nametable/stitch | 918 | 0.65 |
| nes_ppu_cpu_write | cpu read/write helpers | 882 | 0.63 |
| nes_ppu_map_nametable_hl | nametable/stitch | 848 | 0.60 |
| nes_controller_read | controller | 835 | 0.59 |
| nes_apu_clock_triangle_linear | APU | 792 | 0.56 |
| nes_apu_clock_lengths | APU | 727 | 0.52 |
| nes_video_sync_oam_hw | OAM | 720 | 0.51 |
| RAM code near nes_gbc_palette_shadow | video other | 672 | 0.48 |
| nes_poll_nmi_hl | NMI/vblank/ISR | 668 | 0.48 |
| nes_apu_frame_step | APU | 564 | 0.40 |
| nes_controller_latch | controller | 452 | 0.32 |
| nes_apu_clock_sweep_p2 | APU | 418 | 0.30 |
| nes_apu_refresh_p2 | APU | 405 | 0.29 |
| nes_apu_write_banked | APU | 398 | 0.28 |
| nes_ppu_increment_addr | PPU reg emulation | 397 | 0.28 |
| nes_apu_clock_sweep_p1 | APU | 383 | 0.27 |
| nes_apu_timer_to_period | APU | 366 | 0.26 |

Hottest translated blocks: nes_E4B1 8.1%, nes_E49A 6.3%, nes_E3C4 3.6%, nes_E4CA 3.2%, nes_F1B7 2.5%, nes_ED44 1.9%, nes_EE31 1.8%, nes_E776 1.7%

