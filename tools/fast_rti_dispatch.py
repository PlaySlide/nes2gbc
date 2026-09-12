#!/usr/bin/env python3
"""Fast-path common RTI return PCs without changing 6502 interrupt semantics.

Translated NMI delivery only occurs at compiler-emitted poll points. RTI still
pops the *actual* 6502 PC/status through nes_rti_pop_hl; this pass merely checks
that popped PC against a small set of likely poll-point return addresses and,
on an exact match, uses the existing known-bank jump helper instead of the full
translated-PC dispatcher. Any unmatched or stack-modified return falls back to
nes_dispatch_hl exactly as before.

The direct path deliberately uses nes_jump_known_hl_a even when the destination
happens to share the current code bank. Besides keeping the implementation
simple, that helper's XOR A normalizes host flags the same way the dynamic
dispatcher does before entering translated code.
"""

from __future__ import annotations

import argparse
import collections
import re
from pathlib import Path

BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")
IMM_HL_RE = re.compile(r"^ld hl, \$([0-9A-Fa-f]{4})$")
TARGET_RE = re.compile(r"\bnes_([0-9A-Fa-f]{4})\b")


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def prev_code(lines: list[str], start: int, floor: int = 0) -> int | None:
    i = start
    while i >= floor:
        if code(lines[i]):
            return i
        i -= 1
    return None


def next_code(lines: list[str], start: int, ceiling: int | None = None) -> int | None:
    end = len(lines) if ceiling is None else min(ceiling, len(lines))
    i = start
    while i < end:
        if code(lines[i]):
            return i
        i += 1
    return None


def block_ranges(lines: list[str]) -> list[tuple[int, int, int]]:
    labels: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = BLOCK_RE.fullmatch(code(line))
        if m:
            labels.append((i, int(m.group(1), 16)))

    out: list[tuple[int, int, int]] = []
    for n, (start, addr) in enumerate(labels):
        end = labels[n + 1][0] if n + 1 < len(labels) else len(lines)
        for j in range(start + 1, end):
            if code(lines[j]).startswith("SECTION "):
                end = j
                break
        out.append((start, end, addr))
    return out


def collect_poll_points(lines: list[str]) -> list[int]:
    labels = {
        int(m.group(1), 16)
        for line in lines
        if (m := BLOCK_RE.fullmatch(code(line)))
    }
    points: set[int] = set()
    for i, line in enumerate(lines):
        if code(line) != "call nes_poll_nmi_hl":
            continue
        j = prev_code(lines, i - 1, max(0, i - 8))
        if j is None:
            continue
        m = IMM_HL_RE.fullmatch(code(lines[j]))
        if not m:
            continue
        pc = int(m.group(1), 16)
        if pc in labels:
            points.add(pc)
    return sorted(points)


def rank_poll_points(lines: list[str], points: list[int]) -> list[tuple[int, int]]:
    """Rank likely hot poll points by static incoming control-flow references.

    Back/self edges are weighted more heavily because VBlank is most likely to
    be observed while translated execution is spending time in loops. This is
    only a performance heuristic: every miss still falls back to the dispatcher.
    """
    wanted = set(points)
    scores: collections.Counter[int] = collections.Counter()

    for start, end, source in block_ranges(lines):
        for j in range(start + 1, end):
            c = code(lines[j])
            if not c or c.endswith(":"):
                continue
            # Restrict to actual generated control-transfer forms. Do not count
            # dispatch-table BANK()/DW references or unrelated comments/data.
            if not (
                c.startswith("jr ")
                or c.startswith("jp ")
                or c.startswith("ld hl, nes_")
            ):
                continue
            for m in TARGET_RE.finditer(c):
                target = int(m.group(1), 16)
                if target not in wanted:
                    continue
                scores[target] += 5 if target <= source else 1

    # Give every real poll point a nonzero baseline so sparse CFGs still work.
    ranked = [(scores[p] + 1, p) for p in points]
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return ranked


def find_rti_dispatches(lines: list[str]) -> list[int]:
    out: list[int] = []
    for i, line in enumerate(lines):
        if code(line) != "call nes_rti_pop_hl":
            continue
        j = next_code(lines, i + 1, min(len(lines), i + 8))
        if j is not None and code(lines[j]) == "jp nes_dispatch_hl":
            out.append(j)
    return out


def helper(candidates: list[int]) -> str:
    out: list[str] = [
        "\nSECTION \"Guarded RTI return fast dispatch\", ROM0\n",
        "nes_rti_fast_dispatch:\n",
        "    ; HL is the exact PC popped by nes_rti_pop_hl.\n",
    ]
    for n, pc in enumerate(candidates):
        nxt = f".next_{n}"
        out.extend([
            "    ld a, h\n",
            f"    cp ${(pc >> 8) & 0xFF:02X}\n",
            f"    jr nz, {nxt}\n",
            "    ld a, l\n",
            f"    cp ${pc & 0xFF:02X}\n",
            f"    jr nz, {nxt}\n",
            f"    ld a, BANK(nes_{pc:04X})\n",
            f"    ld hl, nes_{pc:04X}\n",
            "    jp nes_jump_known_hl_a\n",
            f"{nxt}:\n",
        ])
    out.extend([
        "    ; Uncommon poll point, IRQ/BRK RTI, or modified stacked PC.\n",
        "    jp nes_dispatch_hl\n",
    ])
    return "".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    p.add_argument(
        "--max-targets", type=int, default=8,
        help="maximum statically ranked poll-point PCs to probe before fallback",
    )
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    points = collect_poll_points(lines)
    dispatches = find_rti_dispatches(lines)

    if not points or not dispatches or args.max_targets <= 0:
        print(
            f"rti-fast: no rewrite ({len(points)} poll points, "
            f"{len(dispatches)} RTI dispatch sites)"
        )
        return 0

    ranked = rank_poll_points(lines, points)
    candidates = [pc for _score, pc in ranked[: args.max_targets]]

    for i in dispatches:
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines[i] = f"{ind}jp nes_rti_fast_dispatch ; guarded exact RTI return\n"

    lines.append(helper(candidates))
    args.asm.write_text("".join(lines), encoding="utf-8")

    pcs = ",".join(f"${pc:04X}" for pc in candidates)
    print(
        f"rti-fast: rewrote {len(dispatches)} RTI dispatch site(s); "
        f"probing {len(candidates)}/{len(points)} ranked poll returns [{pcs}]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
