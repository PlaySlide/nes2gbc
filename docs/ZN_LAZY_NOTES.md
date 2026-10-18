# Lazy flags (Z/N/C) — measurements and findings (WIP, not merged)

Base: perf/banked-superblocks d54dcb8, APU=1, NES2GBC_* unset. Costs use the
normalized metric (`wcost.py`: translated-code cycles at the reference
build's block execution counts).

## Ceiling of a shared Z/N byte (dynamic, % of all profiled GB cycles)

| game | st Z | st N | paired N (removable by a shared byte) | st C | ld Z | ld N | ld C |
|---|---|---|---|---|---|---|---|
| Zelda | 1.96 | 1.73 | 1.66 | 1.52 | 0.41 | 0.08 | 0.43 |
| Contra | 1.98 | 1.98 | 1.91 | 1.17 | 0.19 | 0.07 | 0.54 |
| Castlevania | 2.63 | 2.48 | 2.47 | 1.13 | 0.39 | 0.06 | 0.41 |
| Mega Man | 1.24 | 1.08 | 1.01 | 1.66 | 0.45 | 0.08 | 0.66 |
| SMB | 0.86 | 0.69 | 0.64 | 0.52 | 0.26 | 0.05 | 0.20 |
| DK | 0.96 | 0.79 | 0.75 | 1.87 | 0.24 | 0.04 | 0.80 |
| IC | 1.92 | 1.51 | 1.41 | 2.03 | 0.51 | 0.09 | 1.43 |
| BF | 1.71 | 1.57 | 1.52 | 1.27 | 0.42 | 0.07 | 0.42 |

A perfect shared Z/N byte (zero reader/region overhead) is bounded by the
"paired N" column: about 1-2.5% of cycles. docs/ZN_SHADOW.md (perf/zn-shared)
explains why the one-byte format cannot be bounded statically (PLP->RTS,
RAM-interpreter exits).

## Where the remaining flag stores come from: conservative exits

Unsound experiment (`NES2GBC_HRAM_EXP_DEAD=<vars>`: every all-live exit
treated as dead for <vars>), normalized vs d54dcb8:

| exits treated dead for | Zelda | Contra | Castlevania | Mega Man |
|---|---|---|---|---|
| N | -0.49% | -2.95% | -3.34% | -1.25% |
| N, Z, C | -3.98% | -6.38% | -7.42% | -2.55% |

So better *liveness* at the conservative exits is worth more than changing
the flag format. Contra conservative exits (static): 3482
`IF DEF(NES2GBC_RAM_INTERP)` interpreter fallbacks in mapper-2 dispatch stubs,
678 `jp nes_dispatch_hl`, 572 NMI polls, 185 BRK/IRQ, 34 RTI, 26
materialize_p, 23 apu_write, 10 set_p, 8 jmp-indirect.
`NES2GBC_HRAM_EXP_CAT=<regex>` restricts the experiment to exits whose code
matches; on Contra, dispatch alone gives ~614k cycles/run and everything
except dispatch ~4.5M (11.9M with all exits). The exits interact.

## Concrete leads

1. **Banked RTS return sets are never applied on Contra** ("0 RTS fallbacks
   with proven return sets"). `return_sets_banked` is complete with 148 known
   sets (394 unknown), but every known set contains $A2BA, and $A2BA has no
   generic `nes_A2BA` label (only bank variants), so the pass drops the whole
   set. Fix: accept bank-variant-only continuations when the dispatch for
   that PC can only land on those variants. The interpreter fallback is a
   flag reader, so this must stay conservative. Castlevania also shows 0.
   Zelda and Mega Man use 266-268.
2. **NMI-poll edges are off for Contra and Castlevania** because the program
   contains TSX. Their TSX sites look like data decoded as code. I tried a
   gate based on reachability from the NMI handler
   (`NES2GBC_HRAM_NMI_TSX_REACH=1`, default off): the handler reaches an
   unresolved exit in every game, so it turns the edges off everywhere.
   Forcing the edges on (unsound) gains ~0 on Contra/Castlevania, so this is
   not the lever.
3. **RAM-interpreter fallbacks** in dispatch stubs (`jp nes_interp_enter_rom`
   when the bank is not one of the expected ones) read all state. They are
   the largest static exit class; modelling them needs a proof that the
   fallback is unreachable, or a "bank set is exact" marker from the emitter.
4. Lazy-flag format (shared Z/N byte, carry capture only when live): bounded
   by the table above; still blocked by the region problem in ZN_SHADOW.md.

Knobs added (all default off; the default build is byte-identical to d54dcb8):
NES2GBC_HRAM_EXP_DEAD, NES2GBC_HRAM_EXP_CAT, NES2GBC_HRAM_EXP_DUMP,
NES2GBC_HRAM_NMI_TSX_REACH, NES2GBC_HRAM_EXP_TSX_IGNORE.

## Session 2: leads 1 and 2 (no merge)

Lead 1 premise was wrong: no return continuation is "bank-variant only".
In every return set that the pass drops, the missing continuation has no
translation at all:

* Contra: $A2BA, in 148/148 known sets. It is the continuation of
  `JSR $82A2` at bank 1 $A2B7. That block is reached only through
  `JMP ($0004)` at $E678, whose targets were resolved from ROM pointer
  bytes. Bank 1 $A2BA is `D7 ...` (illegal opcode), so this looks like
  data. It reaches every set through DYN, not through its own RTS: callee
  bank 1 $82A2 has an unresolved `JMP (ind)` at depth 0
  (`dyn_src ('ind', (1,$82A2), ...)`), so its JSR continuations are added
  to every return set.
* Castlevania: $80E6, in 260/260 sets: `JSR $7332` (PRG-RAM, interpreted
  callee) at bank 3 $80E3. Again it reaches every set through DYN. Bank 3
  $80E6 is `SBC $E9 / STY $8081 (bank switch) / $80 (illegal)`, so it does
  not trap in its own bank.

At runtime these continuations enter the interpreter via the dispatch
miss, so they read everything. Given the callee's unresolved indirect jump
(it can switch banks), the bank at the return is unknown. Across all banks
the bytes do not all trap (e.g. a BRK in some bank). So they cannot be
dropped soundly.

I tried a refinement: drop a continuation that traps in the JSR's own
bank when every callee is non-switching (`NES2GBC_RTS_VIEW_TRAPS=1`; default
off here). It drops only 3 (Contra) / 1 (Castlevania) continuations and
does not touch DYN. No effect.

Lead 2 alone ("IF RAM_INTERP" stub fallbacks treated as non-reading) is
worth ~0 on Contra in the unsound experiment (6 stores). It only pays
together with usable return sets and NMI-poll edges (~3.2M and ~11.5M
cycles/run, i.e. ~6% normalized on Contra).

What would make it work: a proof that the speculative indirect targets
(ROM-pointer-resolved `JMP (ind)` from RAM pointers, JSR into PRG-RAM)
are not executed. That needs runtime evidence (profiles) or game
knowledge, so it cannot be done generically and soundly. Alternatively:
a runtime guard that bails out to a slow path that re-materialises flags
when the interpreter is entered from a dispatch miss (e.g. keep a
"flags canonical" epoch). Too large for this session.
