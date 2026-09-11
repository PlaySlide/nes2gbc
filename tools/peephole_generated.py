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


def _inline_compare_helpers(out: list[str]) -> int:
    """Inline the exact release semantics of nes_compare_a_e.

    The earlier broad CMP experiment changed register/liveness behavior along
    with inlining and regressed games. This prototype intentionally does not:
    it leaves A holding the subtraction result exactly like the runtime helper
    and publishes C/Z/N in the same order. PROFILE builds retain the helper so
    the existing compare counter remains meaningful.
    """

    inlined = 0
    for i in range(len(out)):
        if _code(out[i]) != "call nes_compare_a_e":
            continue
        indent = out[i][: len(out[i]) - len(out[i].lstrip())]
        out[i] = (
            "IF DEF(NES2GBC_PROFILE)\n"
            f"{indent}call nes_compare_a_e\n"
            "ELSE\n"
            f"{indent}; inlined exact nes_compare_a_e\n"
            f"{indent}ld d, a\n"
            f"{indent}sub e\n"
            f"{indent}ld c, a\n"
            f"{indent}ld a, d\n"
            f"{indent}cp e\n"
            f"{indent}ld a, $00\n"
            f"{indent}jr c, :+\n"
            f"{indent}inc a\n"
            ":\n"
            f"{indent}ldh [nes_c_shadow], a\n"
            f"{indent}ld a, c\n"
            f"{indent}ldh [nes_z_shadow], a\n"
            f"{indent}ldh [nes_n_shadow], a\n"
            "ENDC\n"
        )
        inlined += 1
    return inlined


def _inline_static_jsr_pushes(out: list[str]) -> int:
    """Inline the exact two-byte 6502 JSR return-address push.

    Static JSRs already know the stacked return value at compile time. The
    runtime helper calls the one-byte stack helper twice; doing the same writes
    directly saves several CALL/RET pairs while preserving the virtual stack
    byte-for-byte, including $0100-page wrap. PROFILE keeps the helper path so
    its existing counter remains accurate.
    """

    inlined = 0
    ret_re = re.compile(r"ld hl, \$([0-9A-Fa-f]{4})")
    for i in range(1, len(out)):
        if _code(out[i]) != "call nes_stack_push_return_hl":
            continue

        k = i - 1
        while k >= 0 and not _code(out[k]):
            k -= 1
        if k < 0:
            continue
        match = ret_re.fullmatch(_code(out[k]))
        if not match:
            continue

        ret = int(match.group(1), 16)
        hi = (ret >> 8) & 0xFF
        lo = ret & 0xFF
        indent = out[k][: len(out[k]) - len(out[k].lstrip())]
        out[k] = (
            "IF DEF(NES2GBC_PROFILE)\n"
            f"{indent}ld hl, ${ret:04X}\n"
            f"{indent}call nes_stack_push_return_hl\n"
            "ELSE\n"
            f"{indent}; inline static 6502 JSR return push\n"
            f"{indent}ldh a, [nes_sp]\n"
            f"{indent}ld l, a\n"
            f"{indent}ld h, $C1\n"
            f"{indent}ld a, ${hi:02X}\n"
            f"{indent}ld [hl], a\n"
            f"{indent}dec l\n"
            f"{indent}ld a, ${lo:02X}\n"
            f"{indent}ld [hl], a\n"
            f"{indent}dec l\n"
            f"{indent}ld a, l\n"
            f"{indent}ldh [nes_sp], a\n"
            "ENDC\n"
        )
        out[i] = f"{indent}; static JSR push inlined above\n"
        inlined += 1
    return inlined


def _inline_rts_pops(out: list[str]) -> int:
    """Inline the exact virtual-stack pop used by translated RTS."""

    inlined = 0
    for i in range(len(out)):
        if _code(out[i]) != "call nes_stack_pop_return_hl":
            continue
        indent = out[i][: len(out[i]) - len(out[i].lstrip())]
        out[i] = (
            "IF DEF(NES2GBC_PROFILE)\n"
            f"{indent}call nes_stack_pop_return_hl\n"
            "ELSE\n"
            f"{indent}; inline 6502 RTS return pop\n"
            f"{indent}ldh a, [nes_sp]\n"
            f"{indent}inc a\n"
            f"{indent}ld l, a\n"
            f"{indent}ld h, $C1\n"
            f"{indent}ld a, [hl]\n"
            f"{indent}ld c, a\n"
            f"{indent}inc l\n"
            f"{indent}ld a, l\n"
            f"{indent}ldh [nes_sp], a\n"
            f"{indent}ld a, [hl]\n"
            f"{indent}ld h, a\n"
            f"{indent}ld l, c\n"
            "ENDC\n"
        )
        inlined += 1
    return inlined


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


def _remove_redundant_zero_retests(out: list[str]) -> int:
    """Drop `and a` when a nearby producer already set GB Z from the same A.

    This catches DEX/DEY/INX/INY and immediate logical-result branches. We walk
    backward only across stores that provably preserve LR35902 flags, then
    require a producer whose hardware Z flag is exactly `(A == 0)`.
    """

    removed = 0
    producer_re = re.compile(
        r"(?:inc a|dec a|and (?:\$[0-9A-Fa-f]{2}|[bcdehl])|"
        r"or (?:\$[0-9A-Fa-f]{2}|[bcdehl])|xor (?:\$[0-9A-Fa-f]{2}|[bcdehl]))"
    )
    flag_preserving_store_re = re.compile(
        r"(?:ldh \[(?:nes_a|nes_x|nes_y|nes_z_shadow|nes_n_shadow)\], a|"
        r"ld \[\$C[0-9A-Fa-f]{3}\], a)"
    )

    for i in range(len(out)):
        if _code(out[i]) != "and a":
            continue

        k = i - 1
        while k >= 0:
            code = _code(out[k])
            if not code:
                k -= 1
                continue
            if flag_preserving_store_re.fullmatch(code):
                k -= 1
                continue
            break
        if k < 0 or not producer_re.fullmatch(_code(out[k])):
            continue

        indent = out[i][: len(out[i]) - len(out[i].lstrip())]
        out[i] = f"{indent}; fused zero test: producer already set GB Z\n"
        removed += 1

    return removed


def _collapse_backward_branch_pairs(out: list[str]) -> int:
    """Turn `jr !cond, :+ ; jr old_label ; :` into one conditional JR.

    We only do this when `old_label` has already appeared in the assembly. The
    original second instruction is already a linked `jr`, so its backward target
    is in range; moving the conditional JR two bytes earlier makes a backward
    displacement less negative and therefore cannot create a range failure.
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


def optimize_lines(lines: list[str]) -> tuple[list[str], int, int, int, int, int, int]:
    out = list(lines)
    compares = _inline_compare_helpers(out)
    jsr_pushes = _inline_static_jsr_pushes(out)
    rts_pops = _inline_rts_pops(out)
    shadow_fused = _fuse_shadow_reloads(out)
    zero_retests = _remove_redundant_zero_retests(out)
    backedges = _collapse_backward_branch_pairs(out)
    return out, compares, jsr_pushes, rts_pops, shadow_fused, zero_retests, backedges


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    args = parser.parse_args()

    original = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    (
        optimized,
        compares,
        jsr_pushes,
        rts_pops,
        shadow_fused,
        zero_retests,
        backedges,
    ) = optimize_lines(original)
    args.asm.write_text("".join(optimized), encoding="utf-8")
    print(
        "peephole: "
        f"inlined {compares} compares, "
        f"{jsr_pushes} static JSR pushes, {rts_pops} RTS pops; "
        f"fused {shadow_fused} Z/N reloads, "
        f"removed {zero_retests} zero retests, "
        f"collapsed {backedges} backward branches"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
