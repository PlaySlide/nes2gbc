#!/usr/bin/env python3
"""Preserve clean host A across hot backward conditional branches.

This is a deliberately conservative release-only extension of emitter A
residency. Existing generated branch lowering evaluates NES flags by loading a
shadow byte into host A, which destroys an otherwise clean resident 6502 A.
For backward same-bank branches (the loop-shaped cases most likely to be taken
repeatedly), this pass:

* requires a visible canonical ``ldh [nes_a], a`` before the branch flag test,
  with only A-preserving stores/copies in between;
* saves clean A in E, evaluates the existing shadow test, and restores A before
  the host conditional transfer (LD does not change LR35902 flags);
* redirects the taken edge to a private target entry that skips only the
  target's redundant first release-path ``ldh a, [nes_a]``;
* reuses emitter-created ``nes_XXXX_fast_a`` entries when available; otherwise
  creates ``nes_XXXX_branch_fast_a`` only when the target's first release-path
  instruction is exactly that canonical A reload.

The normal pipeline collapses most ``jr skip / jp target`` pairs early into a
single ``jp cc, nes_XXXX``. Match that final form directly; the older four-line
shape is also accepted for completeness.

The pass runs after the existing DE cache and dirty-A pass. DE caching treats
host control flow as a hard barrier, so E is dead at the branch boundary; the
dirty-A pass never sees these new conditional edges and therefore cannot defer a
source spill needed by the not-taken path.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

CANON_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
FAST_RE = re.compile(r"^nes_([0-9A-Fa-f]{4})_fast_a:$")
SECTION_BANK_RE = re.compile(r"^SECTION .*BANK\[(\d+)\]")
TRANSFER_RE = re.compile(r"^(jr|jp) nes_([0-9A-Fa-f]{4})$")
COND_TRANSFER_RE = re.compile(
    r"^(jr|jp) (z|nz|c|nc), nes_([0-9A-Fa-f]{4})$", re.IGNORECASE
)
COND_SKIP_RE = re.compile(r"^jr (?:z|nz|c|nc), :\+$", re.IGNORECASE)
LOAD_A = "ldh a, [nes_a]"
STORE_A = "ldh [nes_a], a"
PROFILE_IF = "IF DEF(NES2GBC_PROFILE_TRACE)"

SHADOW_TESTS = {
    "ldh a, [nes_c_shadow]": {"and a"},
    "ldh a, [nes_z_shadow]": {"and a"},
    "ldh a, [nes_n_shadow]": {"bit 7, a"},
    "ldh a, [nes_p]": None,  # followed by AND $mask
}


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


@dataclass
class Block:
    addr: int
    start: int
    end: int
    bank: int | None


def index_blocks(lines: list[str]) -> tuple[list[Block], dict[int, Block]]:
    starts: list[tuple[int, int, int | None]] = []
    bank: int | None = None
    for i, line in enumerate(lines):
        c = code(line)
        if m := SECTION_BANK_RE.match(c):
            bank = int(m.group(1))
        if m := CANON_RE.fullmatch(c):
            starts.append((i, int(m.group(1), 16), bank))

    blocks: list[Block] = []
    for n, (start, addr, b) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        blocks.append(Block(addr, start, end, b))
    return blocks, {b.addr: b for b in blocks}


def release_instruction_indices(lines: list[str], block: Block) -> list[int]:
    """Return instructions/labels that exist in PROFILE_TRACE=0 release code."""
    out: list[int] = []
    # Stack entries are (parent_active, this_condition, seen_else).
    stack: list[tuple[bool, bool, bool]] = []
    active = True

    for i in range(block.start + 1, block.end):
        c = code(lines[i])
        if not c:
            continue

        if c.startswith("IF "):
            parent = active
            if c == PROFILE_IF or c == "IF 0":
                cond = False
            elif c == "IF 1":
                cond = True
            else:
                # Unknown build-time condition: do not reason through it.
                cond = False
            stack.append((parent, cond, False))
            active = parent and cond
            continue
        if c == "ELSE":
            if not stack:
                continue
            parent, cond, _ = stack[-1]
            stack[-1] = (parent, cond, True)
            active = parent and not cond
            continue
        if c == "ENDC":
            if stack:
                parent, _cond, _else = stack.pop()
                active = parent
            continue

        if active:
            out.append(i)

    return out


def first_release_reload(lines: list[str], block: Block) -> int | None:
    for i in release_instruction_indices(lines, block):
        c = code(lines[i])
        if c.endswith(":"):
            continue
        return i if c == LOAD_A else None
    return None


def existing_fast_label(lines: list[str], block: Block) -> str | None:
    wanted = f"nes_{block.addr:04X}_fast_a:"
    for i in range(block.start + 1, block.end):
        if code(lines[i]) == wanted:
            return wanted[:-1]
    return None


def source_a_is_clean(lines: list[str], active: list[int], shadow_pos: int) -> bool:
    """Require a recent canonical A spill and no A-changing instruction after it."""
    for p in range(shadow_pos - 1, -1, -1):
        c = code(lines[active[p]])
        low = c.lower()
        if c == STORE_A:
            return True
        if c.endswith(":"):
            return False
        # Stores/copies from A do not change A.
        if re.fullmatch(r"ldh? \[[^\]]+\], a", low):
            continue
        if re.fullmatch(r"ld [bcdehl], a", low):
            continue
        if low == "nop":
            continue
        return False
    return False


def is_shadow_test_pair(load: str, test: str) -> bool:
    allowed = SHADOW_TESTS.get(load)
    if load not in SHADOW_TESTS:
        return False
    if allowed is None:
        return re.fullmatch(r"and \$[0-9a-f]{2}", test.lower()) is not None
    return test.lower() in allowed


def optimize(lines: list[str]) -> tuple[int, int, int, int, int]:
    blocks, by_addr = index_blocks(lines)
    new_target_reload: dict[int, int] = {}
    existing_targets: dict[int, str] = {}

    for block in blocks:
        if label := existing_fast_label(lines, block):
            existing_targets[block.addr] = label
        elif (reload_i := first_release_reload(lines, block)) is not None:
            new_target_reload[block.addr] = reload_i

    # (save_before_line, restore_after_line, transfer_line, target_label,
    #  opcode, condition)
    edits: list[tuple[int, int, int, str, str, str]] = []
    target_addrs: set[int] = set()
    collapsed_seen = 0
    legacy_seen = 0

    for block in blocks:
        active = release_instruction_indices(lines, block)

        # Common post-collapse form:
        #   ldh a,[shadow]
        #   and/bit ...
        #   jp cc, nes_XXXX
        for p in range(0, max(0, len(active) - 2)):
            load_i, test_i, transfer_i = active[p:p + 3]
            if not is_shadow_test_pair(code(lines[load_i]), code(lines[test_i])):
                continue
            tm = COND_TRANSFER_RE.fullmatch(code(lines[transfer_i]))
            if not tm:
                continue
            collapsed_seen += 1

            target = int(tm.group(3), 16)
            if target >= block.addr:
                continue
            target_block = by_addr.get(target)
            if target_block is None or target_block.bank != block.bank:
                continue
            if target not in existing_targets and target not in new_target_reload:
                continue
            if not source_a_is_clean(lines, active, p):
                continue

            label = existing_targets.get(target, f"nes_{target:04X}_branch_fast_a")
            edits.append((load_i, test_i, transfer_i, label, tm.group(1), tm.group(2)))
            target_addrs.add(target)

        # Older emitter shape retained for builds where collapse_conditional_jp
        # did not fire:
        #   ldh a,[shadow]
        #   and/bit ...
        #   jr inverse,:+
        #   jp/jr nes_XXXX
        for p in range(0, max(0, len(active) - 3)):
            load_i, test_i, skip_i, transfer_i = active[p:p + 4]
            if not is_shadow_test_pair(code(lines[load_i]), code(lines[test_i])):
                continue
            skip_m = COND_SKIP_RE.fullmatch(code(lines[skip_i]))
            if not skip_m:
                continue
            tm = TRANSFER_RE.fullmatch(code(lines[transfer_i]))
            if not tm:
                continue
            legacy_seen += 1

            target = int(tm.group(2), 16)
            if target >= block.addr:
                continue
            target_block = by_addr.get(target)
            if target_block is None or target_block.bank != block.bank:
                continue
            if target not in existing_targets and target not in new_target_reload:
                continue
            if not source_a_is_clean(lines, active, p):
                continue

            # Original skip condition means transfer is taken on its inverse.
            skip_cond = code(lines[skip_i]).split()[1].rstrip(",").lower()
            take_cond = {"z": "nz", "nz": "z", "c": "nc", "nc": "c"}[skip_cond]
            label = existing_targets.get(target, f"nes_{target:04X}_branch_fast_a")
            edits.append((load_i, test_i, transfer_i, label, tm.group(1), take_cond))
            target_addrs.add(target)

    # Avoid double-editing a source if both recognizers somehow see it.
    unique: dict[int, tuple[int, int, int, str, str, str]] = {}
    for edit in edits:
        unique.setdefault(edit[2], edit)
    edits = list(unique.values())

    if not edits:
        return 0, 0, 0, collapsed_seen, legacy_seen

    insert_before: dict[int, list[str]] = {}
    insert_after: dict[int, list[str]] = {}
    for load_i, test_i, transfer_i, label, opcode, cond in edits:
        ind = indent_of(lines[load_i])
        insert_before.setdefault(load_i, []).append(
            f"{ind}ld e, a ; branch A residency save\n"
        )
        insert_after.setdefault(test_i, []).append(
            f"{ind}ld a, e ; branch A residency restore (flags preserved)\n"
        )
        tind = indent_of(lines[transfer_i])
        lines[transfer_i] = f"{tind}{opcode} {cond}, {label}\n"

    # Add one private label per newly-created target, after its canonical reload.
    for target in target_addrs:
        if target in existing_targets:
            continue
        reload_i = new_target_reload[target]
        ind = indent_of(lines[reload_i])
        insert_after.setdefault(reload_i, []).append(
            f"{ind}nes_{target:04X}_branch_fast_a:\n"
        )

    out: list[str] = []
    for i, line in enumerate(lines):
        out.extend(insert_before.get(i, ()))
        out.append(line)
        out.extend(insert_after.get(i, ()))
    lines[:] = out

    existing_used = sum(1 for t in target_addrs if t in existing_targets)
    created = len(target_addrs) - existing_used
    return len(edits), existing_used, created, collapsed_seen, legacy_seen


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    edges, reused, created, collapsed_seen, legacy_seen = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"branch-a: preserved clean A across {edges} backward conditional edge(s); "
        f"reused {reused} emitter fast target(s), created {created} branch target entrie(s); "
        f"saw {collapsed_seen} collapsed + {legacy_seen} legacy conditional shape(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
