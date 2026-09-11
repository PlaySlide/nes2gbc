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
    """Inline the exact semantics of nes_compare_a_e."""

    inlined = 0
    for i in range(len(out)):
        if _code(out[i]) != "call nes_compare_a_e":
            continue
        indent = out[i][: len(out[i]) - len(out[i].lstrip())]
        out[i] = (
            f"{indent}PROFILE_INC nes_profile_compare\n"
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
        )
        inlined += 1
    return inlined


def _inline_static_jsr_pushes(out: list[str]) -> int:
    """Inline the exact two-byte 6502 JSR return-address push."""

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
            f"{indent}PROFILE_INC nes_profile_jsr_push\n"
            f"{indent}; inline static 6502 JSR return push ${ret:04X}\n"
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
            f"{indent}PROFILE_INC nes_profile_rts_pop\n"
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
        )
        inlined += 1
    return inlined


def _inline_byte_stack_helpers(out: list[str]) -> tuple[int, int]:
    """Inline PHA/PHP/PLA/PLP one-byte virtual stack helpers exactly."""

    pushes = 0
    pops = 0
    for i in range(len(out)):
        code = _code(out[i])
        indent = out[i][: len(out[i]) - len(out[i].lstrip())]
        if code == "call nes_stack_push_a":
            out[i] = (
                f"{indent}; inline nes_stack_push_a\n"
                f"{indent}ld e, a\n"
                f"{indent}ldh a, [nes_sp]\n"
                f"{indent}ld l, a\n"
                f"{indent}ld h, $C1\n"
                f"{indent}ld a, e\n"
                f"{indent}ld [hl], a\n"
                f"{indent}dec l\n"
                f"{indent}ld a, l\n"
                f"{indent}ldh [nes_sp], a\n"
            )
            pushes += 1
        elif code == "call nes_stack_pop_a":
            out[i] = (
                f"{indent}; inline nes_stack_pop_a\n"
                f"{indent}ldh a, [nes_sp]\n"
                f"{indent}inc a\n"
                f"{indent}ldh [nes_sp], a\n"
                f"{indent}ld l, a\n"
                f"{indent}ld h, $C1\n"
                f"{indent}ld a, [hl]\n"
            )
            pops += 1
    return pushes, pops


def _direct_fixed_ppu_calls(out: list[str]) -> tuple[int, int]:
    """Call fixed PPU register handlers directly instead of redispatching L.

    The Rust emitter already knows the exact $2000-$2007 register. Generated
    code currently loads that constant into L and then calls a generic helper
    which immediately compares L against every register number. RGBDS local
    labels have stable fully-qualified names, so fixed accesses can enter the
    same handler body directly. PROFILE_INC preserves the generic counters.
    """

    write_targets = {
        0x00: "nes_ppu_cpu_write.ctrl",
        0x01: "nes_ppu_cpu_write.mask",
        0x03: "nes_ppu_cpu_write.oamaddr",
        0x04: "nes_ppu_cpu_write.oamdata_write",
        0x05: "nes_ppu_cpu_write.scroll",
        0x06: "nes_ppu_cpu_write.addr",
        0x07: "nes_ppu_write_data",
    }
    read_targets = {
        0x02: "nes_ppu_cpu_read.status",
        0x04: "nes_ppu_cpu_read.oamdata",
        0x07: "nes_ppu_read_data",
    }
    l_re = re.compile(r"ld l, \$([0-9A-Fa-f]{2})")
    writes = 0
    reads = 0

    for i in range(len(out)):
        call = _code(out[i])
        if call not in {"call nes_ppu_cpu_write", "call nes_ppu_cpu_read"}:
            continue
        k = i - 1
        while k >= 0 and not _code(out[k]):
            k -= 1
        if k < 0:
            continue
        match = l_re.fullmatch(_code(out[k]))
        if not match:
            continue
        reg = int(match.group(1), 16) & 0x07
        targets = write_targets if call.endswith("write") else read_targets
        target = targets.get(reg)
        if target is None:
            continue

        indent = out[k][: len(out[k]) - len(out[k].lstrip())]
        counter = "nes_profile_ppu_write" if call.endswith("write") else "nes_profile_ppu_read"
        out[k] = f"{indent}PROFILE_INC {counter}\n{indent}; fixed PPU register ${reg:02X}\n"
        out[i] = f"{indent}call {target}\n"
        if call.endswith("write"):
            writes += 1
        else:
            reads += 1

    return writes, reads


def _fuse_shadow_reloads(out: list[str]) -> int:
    fused = 0
    for i in range(len(out)):
        load = _code(out[i])
        if load not in {"ldh a, [nes_z_shadow]", "ldh a, [nes_n_shadow]"}:
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
        elif test != "bit 7, a":
            continue

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


def optimize_lines(lines: list[str]) -> tuple[list[str], dict[str, int]]:
    out = list(lines)
    stats: dict[str, int] = {}
    stats["compares"] = _inline_compare_helpers(out)
    stats["jsr_pushes"] = _inline_static_jsr_pushes(out)
    stats["rts_pops"] = _inline_rts_pops(out)
    stats["byte_pushes"], stats["byte_pops"] = _inline_byte_stack_helpers(out)
    stats["ppu_writes"], stats["ppu_reads"] = _direct_fixed_ppu_calls(out)
    stats["shadow_fused"] = _fuse_shadow_reloads(out)
    stats["zero_retests"] = _remove_redundant_zero_retests(out)
    stats["backedges"] = _collapse_backward_branch_pairs(out)
    return out, stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    args = parser.parse_args()
    original = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    optimized, s = optimize_lines(original)
    args.asm.write_text("".join(optimized), encoding="utf-8")
    print(
        "peephole: "
        f"{s['compares']} cmp, {s['jsr_pushes']} JSR, {s['rts_pops']} RTS, "
        f"{s['byte_pushes']}/{s['byte_pops']} byte stack, "
        f"{s['ppu_writes']}/{s['ppu_reads']} fixed PPU write/read, "
        f"{s['shadow_fused']} Z/N reloads, {s['zero_retests']} zero retests, "
        f"{s['backedges']} backward branches"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
