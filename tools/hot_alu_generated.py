#!/usr/bin/env python3
"""Inline exact hot 6502 ALU helpers in generated LR35902 assembly.

This pass only replaces helper calls whose complete semantics are local to the
call.  It preserves the canonical lazy C/Z/N shadows and overflow bit exactly;
we are removing CALL/RET and branchy carry shims, not relaxing CPU semantics.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _capture_czn(indent: str) -> str:
    # Entry: A=result, GB C=6502 carry-out. Exit: A=result.
    return (
        f"{indent}ld c, a\n"
        f"{indent}ld a, $00\n"
        f"{indent}rl a\n"
        f"{indent}ldh [nes_c_shadow], a\n"
        f"{indent}ld a, c\n"
        f"{indent}ldh [nes_z_shadow], a\n"
        f"{indent}ldh [nes_n_shadow], a\n"
    )


def _inline_adc(indent: str) -> str:
    # Input A=lhs, E=rhs. rra copies lazy carry bit0 into GB C; LD preserves C.
    return (
        f"{indent}PROFILE_INC nes_profile_adc\n"
        f"{indent}; inline exact 6502 ADC\n"
        f"{indent}ld d, a\n"
        f"{indent}ldh a, [nes_c_shadow]\n"
        f"{indent}rra\n"
        f"{indent}ld a, d\n"
        f"{indent}adc e\n"
        f"{indent}ld c, a\n"
        f"{indent}ld a, $00\n"
        f"{indent}rl a\n"
        f"{indent}ldh [nes_c_shadow], a\n"
        f"{indent}ldh a, [nes_p]\n"
        f"{indent}and $BF\n"
        f"{indent}ld b, a\n"
        f"{indent}ld a, d\n"
        f"{indent}xor e\n"
        f"{indent}cpl\n"
        f"{indent}ld h, a\n"
        f"{indent}ld a, d\n"
        f"{indent}xor c\n"
        f"{indent}and h\n"
        f"{indent}and $80\n"
        f"{indent}jr z, :+\n"
        f"{indent}ld a, b\n"
        f"{indent}or $40\n"
        f"{indent}ld b, a\n"
        ":\n"
        f"{indent}ld a, b\n"
        f"{indent}ldh [nes_p], a\n"
        f"{indent}ld a, c\n"
        f"{indent}ldh [nes_z_shadow], a\n"
        f"{indent}ldh [nes_n_shadow], a\n"
    )


def _inline_sbc(indent: str) -> str:
    # 6502 SBC = lhs-rhs-(1-C). GB SBC uses C as borrow-in and reports borrow,
    # so complement C before and after the subtraction.
    return (
        f"{indent}PROFILE_INC nes_profile_sbc\n"
        f"{indent}; inline exact 6502 SBC\n"
        f"{indent}ld d, a\n"
        f"{indent}ldh a, [nes_c_shadow]\n"
        f"{indent}rra\n"
        f"{indent}ccf\n"
        f"{indent}ld a, d\n"
        f"{indent}sbc e\n"
        f"{indent}ld c, a\n"
        f"{indent}ccf\n"
        f"{indent}ld a, $00\n"
        f"{indent}rl a\n"
        f"{indent}ldh [nes_c_shadow], a\n"
        f"{indent}ldh a, [nes_p]\n"
        f"{indent}and $BF\n"
        f"{indent}ld b, a\n"
        f"{indent}ld a, d\n"
        f"{indent}xor e\n"
        f"{indent}ld h, a\n"
        f"{indent}ld a, d\n"
        f"{indent}xor c\n"
        f"{indent}and h\n"
        f"{indent}and $80\n"
        f"{indent}jr z, :+\n"
        f"{indent}ld a, b\n"
        f"{indent}or $40\n"
        f"{indent}ld b, a\n"
        ":\n"
        f"{indent}ld a, b\n"
        f"{indent}ldh [nes_p], a\n"
        f"{indent}ld a, c\n"
        f"{indent}ldh [nes_z_shadow], a\n"
        f"{indent}ldh [nes_n_shadow], a\n"
    )


def _inline_bit(indent: str) -> str:
    return (
        f"{indent}PROFILE_INC nes_profile_bit\n"
        f"{indent}; inline exact 6502 BIT\n"
        f"{indent}ld d, a\n"
        f"{indent}and e\n"
        f"{indent}ldh [nes_z_shadow], a\n"
        f"{indent}ld a, e\n"
        f"{indent}ldh [nes_n_shadow], a\n"
        f"{indent}ldh a, [nes_p]\n"
        f"{indent}and $BF\n"
        f"{indent}bit 6, e\n"
        f"{indent}jr z, :+\n"
        f"{indent}or $40\n"
        ":\n"
        f"{indent}ldh [nes_p], a\n"
    )


def _inline_asl(indent: str) -> str:
    return (
        f"{indent}; inline exact 6502 ASL\n"
        f"{indent}add a\n"
        + _capture_czn(indent)
    )


def _inline_lsr(indent: str) -> str:
    return (
        f"{indent}; inline exact 6502 LSR\n"
        f"{indent}srl a\n"
        + _capture_czn(indent)
    )


def _inline_rol(indent: str) -> str:
    return (
        f"{indent}; inline exact 6502 ROL\n"
        f"{indent}ld d, a\n"
        f"{indent}ldh a, [nes_c_shadow]\n"
        f"{indent}rra\n"
        f"{indent}ld a, d\n"
        f"{indent}rl a\n"
        + _capture_czn(indent)
    )


def _inline_ror(indent: str) -> str:
    return (
        f"{indent}; inline exact 6502 ROR\n"
        f"{indent}ld d, a\n"
        f"{indent}ldh a, [nes_c_shadow]\n"
        f"{indent}rra\n"
        f"{indent}ld a, d\n"
        f"{indent}rr a\n"
        + _capture_czn(indent)
    )


def optimize(lines: list[str]) -> tuple[list[str], dict[str, int]]:
    out = list(lines)
    stats = {name: 0 for name in ("adc", "sbc", "bit", "asl", "lsr", "rol", "ror")}
    replacements = {
        "call nes_adc_a_e": ("adc", _inline_adc),
        "call nes_sbc_a_e": ("sbc", _inline_sbc),
        "call nes_bit_a_e": ("bit", _inline_bit),
        "call nes_asl_a": ("asl", _inline_asl),
        "call nes_lsr_a": ("lsr", _inline_lsr),
        "call nes_rol_a": ("rol", _inline_rol),
        "call nes_ror_a": ("ror", _inline_ror),
    }

    for i, line in enumerate(out):
        entry = replacements.get(_code(line))
        if entry is None:
            continue
        name, emitter = entry
        out[i] = emitter(_indent(line))
        stats[name] += 1

    return out, stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    args = parser.parse_args()
    original = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    optimized, s = optimize(original)
    args.asm.write_text("".join(optimized), encoding="utf-8")
    print(
        "hot-alu: "
        f"{s['adc']} ADC, {s['sbc']} SBC, {s['bit']} BIT, "
        f"{s['asl']} ASL, {s['lsr']} LSR, {s['rol']} ROL, {s['ror']} ROR"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
