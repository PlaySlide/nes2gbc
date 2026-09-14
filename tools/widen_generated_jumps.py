#!/usr/bin/env python3
"""Normalize generated NES-label jumps after size-changing peepholes.

The Rust emitter decides whether a same-section target fits JR before the
post-generation peephole passes run. Some of those passes expand code, so an
originally valid JR can become too distant. Conversely, many generated JPs are
still close enough to use the smaller/faster JR after all other rewrites finish.

This pass uses one conservative proof for both directions:

* source and target must still be in the exact same RGBDS SECTION;
* every recognized LR35902 instruction between them is charged the architectural
  maximum of three bytes, regardless of its real encoding;
* labels/comments/blank lines cost zero;
* any unrecognized line in the span makes the proof fail;
* forward displacement must be <= +127;
* backward distance, including the hypothetical two-byte JR itself, must be
  <= 128 bytes.

Existing JRs are preserved only when the proof succeeds; otherwise they widen
to JP exactly as before. Existing direct JPs to generated NES labels shrink to
JR only when the same proof succeeds. Thus every newly-created or preserved JR
is safe even if every instruction in its span takes the largest possible
LR35902 encoding.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


STATIC_JR_RE = re.compile(
    r"^(?P<indent>\s*)jr (?:(?P<cond>z|nz|c|nc), )?(?P<target>nes_[0-9A-Fa-f]{4})(?P<tail>\s*(?:;.*)?)$"
)
STATIC_JP_RE = re.compile(
    r"^(?P<indent>\s*)jp (?:(?P<cond>z|nz|c|nc), )?(?P<target>nes_[0-9A-Fa-f]{4})(?P<tail>\s*(?:;.*)?)$"
)
NES_LABEL_RE = re.compile(
    r"^\s*(?P<label>nes_[0-9A-Fa-f]{4}):\s*(?:;.*)?$"
)
LABEL_RE = re.compile(r"^[A-Za-z_.$@][A-Za-z0-9_.$@]*:$")

# Complete LR35902 mnemonic set used only to distinguish instructions from
# assembler directives/data. Every real instruction is at most three bytes.
LR35902_MNEMONICS = {
    "adc", "add", "and", "bit", "call", "ccf", "cp", "cpl", "daa",
    "dec", "di", "ei", "halt", "inc", "jp", "jr", "ld", "ldh", "nop",
    "or", "pop", "push", "res", "ret", "reti", "rl", "rla", "rlc",
    "rlca", "rr", "rra", "rrc", "rrca", "rst", "sbc", "scf", "set",
    "sla", "sra", "srl", "stop", "sub", "swap", "xor",
}


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def max_line_bytes(line: str) -> int | None:
    """Return a safe per-line byte upper bound, or None if not provable."""
    c = code(line)
    if not c or c.startswith("SECTION "):
        return 0
    if LABEL_RE.fullmatch(c):
        return 0

    mnemonic = c.split(None, 1)[0].lower()
    if mnemonic in LR35902_MNEMONICS:
        return 3

    # Data directives, macro invocations, conditionals, etc. are intentionally
    # not guessed at. Their presence forces the short-jump proof to fail.
    return None


def index_layout(lines: list[str]) -> tuple[list[int], dict[str, tuple[int, int]]]:
    """Map every line to a SECTION id and NES labels to (line, SECTION)."""
    section_ids: list[int] = []
    labels: dict[str, tuple[int, int]] = {}
    section = -1

    for i, line in enumerate(lines):
        if code(line).startswith("SECTION "):
            section += 1
        section_ids.append(section)

        match = NES_LABEL_RE.fullmatch(line.rstrip("\n"))
        if match:
            labels[match.group("label")] = (i, section)

    return section_ids, labels


def provably_in_range(
    lines: list[str],
    source_i: int,
    target: str,
    section_ids: list[int],
    labels: dict[str, tuple[int, int]],
) -> bool:
    """Prove a hypothetical two-byte JR from source_i can reach target."""
    found = labels.get(target)
    if found is None:
        return False

    target_i, target_section = found
    source_section = section_ids[source_i]
    if source_section < 0 or target_section != source_section or target_i == source_i:
        return False

    if target_i > source_i:
        # JR displacement is measured from the byte after the two-byte JR. For a
        # forward target that is exactly the byte count after the JR to the label.
        total = 0
        for i in range(source_i + 1, target_i):
            nbytes = max_line_bytes(lines[i])
            if nbytes is None:
                return False
            total += nbytes
            if total > 127:
                return False
        return True

    # Backward displacement = target - (source + 2). Therefore the bytes from
    # target to the start of JR, plus the two-byte JR itself, must be <= 128.
    total = 0
    for i in range(target_i + 1, source_i):
        nbytes = max_line_bytes(lines[i])
        if nbytes is None:
            return False
        total += nbytes
        if total + 2 > 128:
            return False
    return total + 2 <= 128


def normalize(lines: list[str]) -> tuple[list[str], int, int, int]:
    out = list(lines)
    section_ids, labels = index_layout(lines)
    kept_jr = 0
    widened_jr = 0
    shrunk_jp = 0

    for i, line in enumerate(lines):
        raw = line.rstrip("\n")

        jr = STATIC_JR_RE.fullmatch(raw)
        if jr:
            target = jr.group("target")
            if provably_in_range(lines, i, target, section_ids, labels):
                kept_jr += 1
                continue

            cond = jr.group("cond")
            prefix = "jp " if cond is None else f"jp {cond}, "
            newline = "\n" if line.endswith("\n") else ""
            out[i] = (
                f"{jr.group('indent')}{prefix}{target}{jr.group('tail')}{newline}"
            )
            widened_jr += 1
            continue

        jp = STATIC_JP_RE.fullmatch(raw)
        if not jp:
            continue

        target = jp.group("target")
        if not provably_in_range(lines, i, target, section_ids, labels):
            continue

        cond = jp.group("cond")
        prefix = "jr " if cond is None else f"jr {cond}, "
        newline = "\n" if line.endswith("\n") else ""
        out[i] = (
            f"{jp.group('indent')}{prefix}{target}{jp.group('tail')}{newline}"
        )
        shrunk_jp += 1

    return out, kept_jr, widened_jr, shrunk_jp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asm", type=Path)
    args = parser.parse_args()

    original = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    optimized, kept_jr, widened_jr, shrunk_jp = normalize(original)
    args.asm.write_text("".join(optimized), encoding="utf-8")
    print(
        f"peephole: kept {kept_jr} proven in-range generated JRs, "
        f"widened {widened_jr}, shrunk {shrunk_jp} proven in-range generated JPs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
