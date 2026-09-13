#!/usr/bin/env python3
"""Inline exact hot 6502 ALU helpers in generated LR35902 assembly.

This pass only replaces helper calls whose complete semantics are local to the
call. It preserves the canonical lazy C/Z/N shadows and overflow bit exactly;
we are removing CALL/RET and branchy carry shims, not relaxing CPU semantics.

After those helpers are inline, zero-page indexed read/modify/write instructions
have another exact simplification: the first effective address remains live in
HL across the whole RMW body. The emitter currently recomputes that same address
for the write-back and wraps the second computation in PUSH/POP AF. For a single
source INC/DEC/ASL/LSR/ROL/ROR ZeroPageX/ZeroPageY instruction, reuse the first
HL instead and remove only that redundant second address calculation.
"""

from __future__ import annotations

import argparse
import re
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


_SOURCE_RE = re.compile(
    r"; \$[0-9A-Fa-f]{4}: \$[0-9A-Fa-f]{2} ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)"
)
_INDEX_LOADS = {"ldh a, [nes_x]", "ldh a, [nes_y]"}
_ADD_ZP_RE = re.compile(r"add \$[0-9A-Fa-f]{2}$")


def _is_indexed_rmw_source(lines: list[str], start: int) -> bool:
    """Require the nearest source marker to be one indexed 6502 RMW opcode."""
    for i in range(start - 1, max(-1, start - 17), -1):
        m = _SOURCE_RE.search(lines[i])
        if m:
            return m.group(1) in {"Inc", "Dec", "Asl", "Lsr", "Rol", "Ror"} and m.group(2) in {
                "ZeroPageX",
                "ZeroPageY",
            }
        c = _code(lines[i])
        if c.startswith("SECTION ") or (c.startswith("nes_") and c.endswith(":")):
            break
    return False


def _body_preserves_hl(lines: list[str], start: int, end: int) -> bool:
    """Prove the inlined RMW body leaves the first effective address in HL."""
    reg_re = re.compile(r"\b(?:hl|h|l)\b", re.IGNORECASE)
    for line in lines[start:end]:
        if _SOURCE_RE.search(line):
            return False
        c = _code(line)
        if not c:
            continue
        low = c.lower()
        if low.endswith(":") or low.startswith(("call ", "jp ", "jr ", "ret", "reti")):
            return False
        if reg_re.search(low):
            return False
    return True


def _fuse_indexed_rmw_address_recalc(lines: list[str]) -> int:
    """Reuse HL across one ZeroPageX/Y read-modify-write instruction."""
    fused = 0
    i = 0
    while i + 12 < len(lines):
        first_index = _code(lines[i])
        if first_index not in _INDEX_LOADS or not _ADD_ZP_RE.fullmatch(_code(lines[i + 1])):
            i += 1
            continue
        if [_code(lines[i + n]) for n in range(2, 5)] != [
            "ld l, a",
            "ld h, $C0",
            "ld a, [hl]",
        ]:
            i += 1
            continue
        if not _is_indexed_rmw_source(lines, i):
            i += 1
            continue

        push_i = None
        ceiling = min(len(lines) - 6, i + 36)
        for j in range(i + 5, ceiling):
            if _SOURCE_RE.search(lines[j]):
                break
            if _code(lines[j]) == "push af":
                push_i = j
                break
        if push_i is None or not _body_preserves_hl(lines, i + 5, push_i):
            i += 1
            continue

        second = [_code(lines[push_i + n]) for n in range(1, 7)]
        if second != [
            first_index,
            _code(lines[i + 1]),
            "ld l, a",
            "ld h, $C0",
            "pop af",
            "ld [hl], a",
        ]:
            i += 1
            continue

        ind = _indent(lines[push_i])
        lines[push_i] = f"{ind}; reused first indexed RMW effective address in HL\n"
        for j in range(push_i + 1, push_i + 6):
            lines[j] = f"{ind}; redundant indexed RMW address recompute removed\n"
        fused += 1
        i = push_i + 7
    return fused


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

    # Helper inlining above can introduce multiple physical lines in one list
    # element. Re-split before looking for the exact duplicated RMW address path.
    out = "".join(out).splitlines(keepends=True)
    stats["indexed_rmw"] = _fuse_indexed_rmw_address_recalc(out)
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
        f"{s['asl']} ASL, {s['lsr']} LSR, {s['rol']} ROL, {s['ror']} ROR, "
        f"fused {s['indexed_rmw']} indexed RMW address recalculation(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
