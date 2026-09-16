#!/usr/bin/env python3
"""Specialize strict ASL/TAY inline JSR word-table dispatchers.

Some NES games (notably SMB) implement state dispatch like this:

    JSR JumpEngine
    .word state0, state1, ...

JumpEngine starts with ASL A / TAY, pops the JSR return address into a
zero-page base pointer, reads a little-endian target word from the inline table,
and JMPs indirectly through another zero-page pointer.

The CFG already discovers these targets statically, but ordinary generated code
still executes the full virtual JSR + dispatcher + two generic PRG reads +
indirect-PC dispatch at runtime. This pass recognizes only the strict dispatcher
shape above and replaces each eligible JSR site with a direct native switch.

Architectural side effects are preserved for the fast path:
* the two JSR return bytes remain written in virtual stack page $0100;
* virtual SP is unchanged, matching JSR followed by the dispatcher's two PLAs;
* the dispatcher's base/target zero-page bytes are written;
* Y becomes index*2+2;
* A/Z/N become the selected target high byte;
* C becomes the carry from ASL (zero for the accepted index range < 128).

Out-of-range indices fall back to the untouched original generated JSR path.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

INSN_RE = re.compile(r"^\s*; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) (\w+) (\w+)")
NES_LABEL_RE = re.compile(r"^\s*nes_([0-9A-Fa-f]{4}):\s*$")
TRACE_LABEL_RE = re.compile(r"^\s*nes_[0-9A-Fa-f]{4}_trace:\s*$")
SECTION_RE = re.compile(r"^\s*SECTION\b")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def load_prg(path: Path) -> tuple[int, bytes] | None:
    data = path.read_bytes()
    if len(data) < 16 or data[:4] != b"NES\x1a":
        return None
    h = data[:16]
    mapper = (h[6] >> 4) | (h[7] & 0xF0)
    if (h[7] & 0x0C) == 0x08:
        mapper |= (h[8] & 0x0F) << 8
    if mapper not in (0, 3):
        return None
    prg_len = h[4] * 0x4000
    if prg_len not in (0x4000, 0x8000):
        return None
    start = 16 + (512 if (h[6] & 0x04) else 0)
    end = start + prg_len
    if end > len(data):
        return None
    return mapper, data[start:end]


def off(prg: bytes, addr: int) -> int | None:
    if addr < 0x8000:
        return None
    x = addr - 0x8000
    if len(prg) == 0x4000:
        return x & 0x3FFF
    if x < len(prg):
        return x
    return None


def byte_at(prg: bytes, addr: int) -> int | None:
    o = off(prg, addr)
    return None if o is None else prg[o]


def word_at(prg: bytes, addr: int) -> int | None:
    lo = byte_at(prg, addr)
    hi = byte_at(prg, (addr + 1) & 0xFFFF)
    if lo is None or hi is None:
        return None
    return lo | (hi << 8)


def strict_dispatcher(prg: bytes, entry: int) -> tuple[int, int] | None:
    """Return (base_zp, target_zp) for strict SMB-style JumpEngine shape."""
    o = off(prg, entry)
    if o is None or o + 16 > len(prg):
        return None

    # The fast path below relies on this exact index transform.
    if prg[o:o + 2] != bytes((0x0A, 0xA8)):  # ASL A / TAY
        return None

    end = min(o + 48, len(prg))
    pop_i = None
    base = None
    for i in range(o + 2, end - 5):
        if (
            prg[i] == 0x68 and prg[i + 1] == 0x85
            and prg[i + 3] == 0x68 and prg[i + 4] == 0x85
            and prg[i + 5] == ((prg[i + 2] + 1) & 0xFF)
        ):
            pop_i = i
            base = prg[i + 2]
            break
    if pop_i is None or base is None:
        return None

    low_i = None
    pointer = None
    for i in range(pop_i + 6, end - 3):
        if prg[i] == 0xB1 and prg[i + 1] == base and prg[i + 2] == 0x85:
            low_i = i
            pointer = prg[i + 3]
            break
    if low_i is None or pointer is None or pointer == 0xFF:
        return None

    high_i = None
    for i in range(low_i + 4, end - 3):
        if (
            prg[i] == 0xB1 and prg[i + 1] == base
            and prg[i + 2] == 0x85 and prg[i + 3] == pointer + 1
        ):
            high_i = i
            break
    if high_i is None:
        return None

    for i in range(high_i + 4, end - 2):
        if prg[i] == 0x6C and prg[i + 1] == pointer and prg[i + 2] == 0x00:
            return base, pointer
    return None


def body_end(lines: list[str], start: int) -> int:
    """Return first line after this source instruction's generated body."""
    i = start + 1
    while i < len(lines):
        if (
            INSN_RE.match(lines[i])
            or NES_LABEL_RE.match(lines[i])
            or TRACE_LABEL_RE.match(lines[i])
            or SECTION_RE.match(code(lines[i]))
        ):
            return i
        i += 1
    return len(lines)


def generated_labels(lines: list[str]) -> set[int]:
    out: set[int] = set()
    for line in lines:
        m = NES_LABEL_RE.match(line)
        if m:
            out.add(int(m.group(1), 16))
    return out


def inline_targets(prg: bytes, base: int, labels: set[int]) -> list[int]:
    out: list[int] = []
    for index in range(128):
        target = word_at(prg, (base + index * 2) & 0xFFFF)
        if target is None or target not in labels:
            break
        out.append(target)
    return out


def make_fast_body(
    indent: str,
    pc: int,
    dispatcher: int,
    return_addr: int,
    base_zp: int,
    target_zp: int,
    targets: list[int],
    original: list[str],
) -> list[str]:
    tag = f"nes_inline_dispatch_{pc:04X}"
    fallback = f"{tag}_fallback"
    lines: list[str] = []

    lines.append(f"{indent}; strict inline-dispatch fast path for JSR ${dispatcher:04X}: {len(targets)} target(s)\n")
    lines.append(f"{indent}ldh a, [nes_a]\n")
    lines.append(f"{indent}cp ${len(targets):02X}\n")
    lines.append(f"{indent}jp nc, {fallback}\n")
    lines.append(f"{indent}ld e, a ; preserve dispatcher state index\n")

    # Preserve the bytes that a real JSR would leave in $0100 even though the
    # dispatcher immediately PLA/PLAs them and restores SP to its old value.
    lines.append(f"{indent}PROFILE_INC nes_profile_jsr_push\n")
    lines.append(f"{indent}ldh a, [nes_sp]\n")
    lines.append(f"{indent}ld l, a\n")
    lines.append(f"{indent}ld h, $C1\n")
    lines.append(f"{indent}ld a, ${(return_addr >> 8) & 0xFF:02X}\n")
    lines.append(f"{indent}ld [hl], a\n")
    lines.append(f"{indent}dec l\n")
    lines.append(f"{indent}ld a, ${return_addr & 0xFF:02X}\n")
    lines.append(f"{indent}ld [hl], a\n")

    # The dispatcher's PLAs materialize the JSR return address in zero page.
    lines.append(f"{indent}ld a, ${return_addr & 0xFF:02X}\n")
    lines.append(f"{indent}ld [${0xC000 + base_zp:04X}], a\n")
    lines.append(f"{indent}ld a, ${(return_addr >> 8) & 0xFF:02X}\n")
    lines.append(f"{indent}ld [${0xC000 + base_zp + 1:04X}], a\n")

    # ASL A / TAY / INY / INY. Accepted table indices are <128, so ASL carry=0.
    lines.append(f"{indent}ld a, e\n")
    lines.append(f"{indent}add a\n")
    lines.append(f"{indent}add $02\n")
    lines.append(f"{indent}ldh [nes_y], a\n")
    lines.append(f"{indent}xor a\n")
    lines.append(f"{indent}ldh [nes_c_shadow], a\n")

    # Keep profiler's architectural bus-read totals meaningful. These macros
    # disappear entirely in release builds.
    lines.append(f"{indent}PROFILE_INC nes_profile_cpu_read\n")
    lines.append(f"{indent}PROFILE_INC nes_profile_read_prg\n")
    lines.append(f"{indent}PROFILE_INC nes_profile_cpu_read\n")
    lines.append(f"{indent}PROFILE_INC nes_profile_read_prg\n")

    lines.append(f"{indent}ld a, e\n")
    for idx in range(len(targets)):
        lines.append(f"{indent}cp ${idx:02X}\n")
        lines.append(f"{indent}jp z, {tag}_case_{idx}\n")
    # Defensive only; the range check above makes this unreachable.
    lines.append(f"{indent}jp {fallback}\n")

    for idx, target in enumerate(targets):
        lines.append(f"{tag}_case_{idx}:\n")
        lines.append(f"{indent}ld a, ${target & 0xFF:02X}\n")
        lines.append(f"{indent}ld [${0xC000 + target_zp:04X}], a\n")
        lines.append(f"{indent}ld a, ${(target >> 8) & 0xFF:02X}\n")
        lines.append(f"{indent}ld [${0xC000 + target_zp + 1:04X}], a\n")
        # Final LDA in the original dispatcher leaves A=target high and Z/N
        # derived from it. All PRG targets are >=$8000, hence N is naturally set.
        lines.append(f"{indent}ldh [nes_a], a\n")
        lines.append(f"{indent}ldh [nes_z_shadow], a\n")
        lines.append(f"{indent}ldh [nes_n_shadow], a\n")
        lines.append(f"{indent}ld a, BANK(nes_{target:04X})\n")
        lines.append(f"{indent}ld hl, nes_{target:04X}\n")
        lines.append(f"{indent}jp nes_jump_known_hl_a\n")

    lines.append(f"{fallback}:\n")
    lines.extend(original)
    return lines


def rewrite(path: Path, rom: Path) -> tuple[int, int, int]:
    loaded = load_prg(rom)
    if loaded is None:
        print("inline-dispatch: skipped (unsupported ROM)")
        return 0, 0, 0
    _, prg = loaded

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    labels = generated_labels(lines)
    sites = 0
    total_targets = 0
    dispatchers: set[int] = set()

    # Work backwards so replacements do not invalidate earlier line indices.
    candidates: list[tuple[int, int, int, int, int, list[int], int]] = []
    for i, line in enumerate(lines):
        m = INSN_RE.match(line)
        if not m or m.group(3) != "Jsr" or m.group(4) != "Absolute":
            continue
        pc = int(m.group(1), 16)
        if byte_at(prg, pc) != 0x20:
            continue
        dispatcher = word_at(prg, pc + 1)
        if dispatcher is None:
            continue
        shape = strict_dispatcher(prg, dispatcher)
        if shape is None:
            continue
        base_zp, target_zp = shape
        table_base = (pc + 3) & 0xFFFF
        targets = inline_targets(prg, table_base, labels)
        # One-entry tables are cheaper as ordinary control flow and more likely
        # to be an accidental data/code coincidence; require a real switch.
        if len(targets) < 2 or len(targets) > 127:
            continue
        end = body_end(lines, i)
        candidates.append((i, end, pc, dispatcher, base_zp, targets, target_zp))

    for i, end, pc, dispatcher, base_zp, targets, target_zp in reversed(candidates):
        indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        original = lines[i + 1:end]
        replacement = make_fast_body(
            indent,
            pc,
            dispatcher,
            (pc + 2) & 0xFFFF,
            base_zp,
            target_zp,
            targets,
            original,
        )
        lines[i + 1:end] = replacement
        sites += 1
        total_targets += len(targets)
        dispatchers.add(dispatcher)

    path.write_text("".join(lines), encoding="utf-8")
    print(
        f"inline-dispatch: specialized {sites} JSR site(s) across {len(dispatchers)} strict ASL/TAY dispatcher(s); "
        f"{total_targets} inline target entries, preserved stack/ZP/A/Y/C semantics with original fallback"
    )
    return sites, len(dispatchers), total_targets


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    p.add_argument("rom", type=Path)
    args = p.parse_args()
    rewrite(args.asm, args.rom)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
