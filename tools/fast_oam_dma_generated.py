#!/usr/bin/env python3
"""Redirect translated $4014 writes to a faster exact internal-RAM OAM DMA.

The runtime's common DMA path copies 256 bytes one at a time and branches once
per byte.  NES games overwhelmingly DMA from internal RAM (normally page $02).
For those pages the source mapping is already exact and the destination is the
same canonical 256-byte virtual OAM at $C900, including arbitrary OAMADDR wrap.

This pass changes no OAM semantics. It redirects generated calls to a ROM0
helper that performs the same copy 16 bytes per loop iteration, then invokes the
same projection routine and dirty publication path. Non-RAM source pages jump
straight to the original nes_oam_dma helper.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def code(line: str) -> str:
    return line.split(";", 1)[0].strip()


def helper() -> str:
    out: list[str] = [
        "\nSECTION \"Generated fast OAM DMA\", ROM0\n",
        "nes_oam_dma_fast_unrolled:\n",
        "    ; Input A = NES DMA source page. Preserve exact runtime semantics.\n",
        "    ld h, a\n",
        "    ld l, $00\n",
        "    ld d, $C9\n",
        "    ld a, [nes_oamaddr]\n",
        "    ld e, a\n",
        "\n",
        "    ; Internal NES RAM is the common path. Anything else retains the\n",
        "    ; existing generic bus-aware implementation. A still holds page.\n",
        "    ld a, h\n",
        "    cp $20\n",
        "    jp nc, nes_oam_dma\n",
        "\n",
        "    PROFILE_INC nes_profile_oam_dma_fast\n",
        "    and $07\n",
        "    or $C0\n",
        "    ld h, a\n",
        "    ld b, $10\n",
        ".copy16:\n",
    ]

    for _ in range(16):
        out.extend(
            [
                "    ld a, [hli]\n",
                "    ld [de], a\n",
                "    inc e\n",
            ]
        )

    out.extend(
        [
            "    dec b\n",
            "    jr nz, .copy16\n",
            "\n",
            "    ; Same post-DMA projection/publication contract as nes_oam_dma.\n",
            "    call nes_video_build_oam_shadow\n",
            "    ld a, $01\n",
            "    ld [nes_oam_dirty], a\n",
            "    ret\n",
        ]
    )
    return "".join(out)


def optimize(lines: list[str]) -> int:
    rewritten = 0
    for i, line in enumerate(lines):
        if code(line) != "call nes_oam_dma":
            continue
        indent = line[: len(line) - len(line.lstrip())]
        lines[i] = f"{indent}call nes_oam_dma_fast_unrolled ; exact 16-byte-unrolled $4014 DMA\n"
        rewritten += 1

    if rewritten and not any(code(line) == "nes_oam_dma_fast_unrolled:" for line in lines):
        lines.append(helper())
    return rewritten


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("asm", type=Path)
    args = p.parse_args()

    lines = args.asm.read_text(encoding="utf-8").splitlines(keepends=True)
    rewritten = optimize(lines)
    args.asm.write_text("".join(lines), encoding="utf-8")
    print(
        f"oam-dma-fast: redirected {rewritten} generated $4014 call site(s) to "
        "exact 16-byte-unrolled internal-RAM DMA"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
