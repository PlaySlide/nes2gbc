#!/usr/bin/env python3
"""Small release peepholes for generated LR35902 assembly.

These are deliberately conservative prototypes so we can measure them across
several games before baking the transformations into the Rust emitter.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def _code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def _fuse_shadow_reloads(out: list[str]) -> int:
    fused = 0

    # Emitter sequence for any operation whose Z/N result is still in A:
    #   ldh [nes_z_shadow], a
    #   ldh [nes_n_shadow], a
    #   ; optional comments / blank lines
    #   ldh a, [nes_z_shadow]   or   ldh a, [nes_n_shadow]
    #   and a                   or   bit 7, a
    #
    # Both shadow stores preserve A, so the reload is redundant. Keep the
    # shadow stores themselves: 6502 flags remain architecturally live after
    # the branch and a later block may still consume them.
    for i in range(len(out)):
        load = _code(out[i])
        if load not in {
            "ldh a, [nes_z_shadow]",
            "ldh a, [nes_n_shadow]",
        }:
            continue

        j = i + 1
        while j < len(out) and not _code(out[j]):
            j += 1
        if j >= len(out):
            continue
        test = _code(out[j])
        if load.endswith("[nes_z_shadow]"):
            if test != "and a":
                continue
        else:
            if test != "bit 7, a":
                continue

        # Require the exact final two flag publications from emit_update_nz;
        # no helper, load, arithmetic op, or other possible A clobber may sit
        # between publication and the branch reload.
        k = i - 1
        while k >= 0 and not _code(out[k]):
            k -= 1
        if k < 0 or _code(out[k]) != "ldh [nes_n_shadow], a":
            continue
        k -= 1
        while k >= 0 and not _code(out[k]):
            k -= 1
        if k < 0 or _code(out[k]) != "ldh [nes_z_shadow], a":
            continue

        indent = out[i][: len(out[i]) - len(out[i].lstrip())]
        shadow = "Z" if "nes_z_shadow" in load else "N"
        out[i] = f"{indent}; fused {shadow} branch: A already holds flag result\n"
        fused += 1

    return fused


def _collapse_backward_branch_pairs(out: list[str]) -> int:
    """Turn `jr !cond, :+ ; jr old_label ; :` into one conditional JR.

    We only do this when `old_label` has already appeared in the assembly. The
    original second instruction is already a linked `jr`, so its backward target
    is in range; moving the conditional JR two bytes earlier makes a backward
    displacement *less* negative and therefore cannot create a range failure.
    This targets exactly the hot loop-backedge shape without guessing linker
    distances for forward branches or cross-bank transfers.
    """

    seen_labels: set[str] = set()
    collapsed = 0
    skip_re = re.compile(r"jr (z|nz), :\+")
    target_re = re.compile(r"jr (nes_[0-9A-Fa-f]{4})")

    for i in range(len(out)):
        code = _code(out[i])
        if code.startswith("nes_") and code.endswith(":"):
            seen_labels.add(code[:-1])
            continue

        match = skip_re.fullmatch(code)
        if not match:
            continue

        j = i + 1
        while j < len(out) and not _code(out[j]):
            j += 1
        if j >= len(out):
            continue
        target_match = target_re.fullmatch(_code(out[j]))
        if not target_match:
            continue
        target = target_match.group(1)
        if target not in seen_labels:
            continue

        k = j + 1
        while k < len(out) and not _code(out[k]):
            k += 1
        if k >= len(out) or _code(out[k]) != ":":
            continue

        inverse = "nz" if match.group(1) == "z" else "z"
        indent = out[i][: len(out[i]) - len(out[i].lstrip())]
        out[i] = f"{indent}jr {inverse}, {target}\n"
        out[j] = f"{indent}; fused backward conditional branch\n"
        collapsed += 1

    return collapsed


def optimize_lines(lines: list[str]) -> tuple[list[str], int, int]:
    out = list(lines)
    shadow_fused = _fuse_shadow_reloads(out)
    backedges = _collapse_backward_branch_pairs(out)
    return out, shadow_fused, backedges


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    args = parser.parse_args()

    original = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    optimized, shadow_fused, backedges = optimize_lines(original)
    args.asm.write_text("".join(optimized), encoding="utf-8")
    print(
        "peephole: "
        f"fused {shadow_fused} immediate Z/N shadow reloads, "
        f"collapsed {backedges} backward conditional branches"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
