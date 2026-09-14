#!/usr/bin/env python3
"""Redirect translated $4014 writes to faster exact internal-RAM OAM handling.

The common NES path copies 256 bytes from internal RAM to canonical virtual OAM,
then projects up to 40 visible NES sprites into the GBC OAM shadow. This pass
keeps the exact canonical copy and unusual-source fallback, but:
  * copies 16 bytes per DMA loop iteration, and
  * uses a register-heavy projector on the normal DMA path.

The fast projector preserves the established ordering: follow-camera update runs
after the full 256-byte NES OAM copy, then all 64 source entries are considered
in OAM order until 40 visible CGB entries have been emitted. Direct $2004 writes,
PPUCTRL-triggered rebuilds, and VBlank fallback continue to use the runtime's
original projector.
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
        out.extend([
            "    ld a, [hli]\n",
            "    ld [de], a\n",
            "    inc e\n",
        ])

    out.extend([
        "    dec b\n",
        "    jr nz, .copy16\n",
        "\n",
        "    ; Canonical OAM is now complete. Keep camera semantics identical:\n",
        "    ; follow first, then project the finished 64-entry NES OAM image.\n",
        "    call nes_video_build_oam_shadow_fast\n",
        "    ld a, $01\n",
        "    ld [nes_oam_dirty], a\n",
        "    ret\n",
        "\n",
        "; Fast steady-state projector for the normal $4014 DMA path.\n",
        "; HL walks canonical NES OAM $C900-$C9FF; reaching $CA00 replaces the\n",
        "; old 64-entry B counter and frees B/C for per-sprite state.\n",
        "nes_video_build_oam_shadow_fast:\n",
        "    call nes_view_follow_update\n",
        "\n",
        "    ld a, [nes_ppuctrl]\n",
        "    ldh [nes_oam_ppuctrl_tmp], a\n",
        "    ld hl, nes_oam_ram\n",
        "    ld de, nes_gbc_oam_shadow\n",
        "\n",
        ".scan:\n",
        "    ld a, [hli]\n",
        "    cp $EF\n",
        "    jp nc, .skip_three_source_bytes\n",
        "    inc a\n",
        "    ld b, a\n",
        "    ldh a, [nes_view_y]\n",
        "    ld c, a\n",
        "    ld a, b\n",
        "    sub c\n",
        "    jp c, .skip_three_source_bytes\n",
        "    cp $90\n",
        "    jp nc, .skip_three_source_bytes\n",
        "    add $10\n",
        "    ld b, a\n",
        "\n",
        "    ; Check X before loading tile/attributes. HL currently points at tile.\n",
        "    inc hl\n",
        "    inc hl\n",
        "    ldh a, [nes_view_x]\n",
        "    ld c, a\n",
        "    ld a, [hl]\n",
        "    sub c\n",
        "    jr c, .skip_x_source_byte\n",
        "    cp $A0\n",
        "    jr nc, .skip_x_source_byte\n",
        "    add $08\n",
        "    ld c, a\n",
        "\n",
        "    dec hl\n",
        "    dec hl\n",
        "\n",
        "    ld a, b\n",
        "    ld [de], a\n",
        "    inc de\n",
        "    ld a, c\n",
        "    ld [de], a\n",
        "    inc de\n",
        "\n",
        "    ; Tile number and CGB VRAM bank.\n",
        "    ld a, [hli]\n",
        "    ld c, a\n",
        "    ldh a, [nes_oam_ppuctrl_tmp]\n",
        "    bit 5, a\n",
        "    jr z, .sprite_8x8\n",
        "\n",
        "    ld a, c\n",
        "    and $FE\n",
        "    ld [de], a\n",
        "    inc de\n",
        "    bit 0, c\n",
        "    ld b, $00\n",
        "    jr z, .bank_ready\n",
        "    ld b, $08\n",
        "    jr .bank_ready\n",
        "\n",
        ".sprite_8x8:\n",
        "    ld a, c\n",
        "    ld [de], a\n",
        "    inc de\n",
        "    ldh a, [nes_oam_ppuctrl_tmp]\n",
        "    and $08\n",
        "    ld b, a\n",
        "\n",
        ".bank_ready:\n",
        "    ld a, [hli]\n",
        "    ld c, a\n",
        "    and $03\n",
        "    or b\n",
        "    ld b, a\n",
        "\n",
        "    bit 5, c\n",
        "    jr z, .no_priority\n",
        "    ld a, b\n",
        "    or $80\n",
        "    ld b, a\n",
        ".no_priority:\n",
        "    bit 6, c\n",
        "    jr z, .no_hflip\n",
        "    ld a, b\n",
        "    or $20\n",
        "    ld b, a\n",
        ".no_hflip:\n",
        "    bit 7, c\n",
        "    jr z, .no_vflip\n",
        "    ld a, b\n",
        "    or $40\n",
        "    ld b, a\n",
        ".no_vflip:\n",
        "    ld a, b\n",
        "    ld [de], a\n",
        "    inc de\n",
        "\n",
        "    ; HL points at source X; advance to next 4-byte record.\n",
        "    inc hl\n",
        "\n",
        "    ; Forty emitted CGB sprites is the hardware limit.\n",
        "    ld a, e\n",
        "    cp $A0\n",
        "    jr z, .full_ready\n",
        "\n",
        ".source_advance_check:\n",
        "    ld a, h\n",
        "    cp $CA\n",
        "    jp nz, .scan\n",
        "    jr .clear_unused\n",
        "\n",
        ".skip_x_source_byte:\n",
        "    inc hl\n",
        "    jr .source_advance_check\n",
        "\n",
        ".skip_three_source_bytes:\n",
        "    inc hl\n",
        "    inc hl\n",
        "    inc hl\n",
        "    jr .source_advance_check\n",
        "\n",
        ".clear_unused:\n",
        "    ; Preserve baseline final emit-count scratch once per frame.\n",
        "    ld a, e\n",
        "    srl a\n",
        "    srl a\n",
        "    ldh [nes_oam_emit_count], a\n",
        "\n",
        "    ld a, e\n",
        "    cp $A0\n",
        "    jr z, .ready\n",
        "    xor a\n",
        ".clear_loop:\n",
        "    ld [de], a\n",
        "    inc de\n",
        "    ld [de], a\n",
        "    inc de\n",
        "    ld [de], a\n",
        "    inc de\n",
        "    ld [de], a\n",
        "    inc de\n",
        "    ld a, e\n",
        "    cp $A0\n",
        "    jr z, .ready\n",
        "    xor a\n",
        "    jr .clear_loop\n",
        "\n",
        ".full_ready:\n",
        "    ld a, 40\n",
        "    ldh [nes_oam_emit_count], a\n",
        "\n",
        ".ready:\n",
        "    ld a, $01\n",
        "    ldh [nes_oam_shadow_ready], a\n",
        "    ret\n",
    ])
    return "".join(out)


def optimize(lines: list[str]) -> int:
    rewritten = 0
    for i, line in enumerate(lines):
        if code(line) != "call nes_oam_dma":
            continue
        indent = line[: len(line) - len(line.lstrip())]
        lines[i] = (
            f"{indent}call nes_oam_dma_fast_unrolled "
            "; exact unrolled $4014 DMA + register projector\n"
        )
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
        "exact 16-byte-unrolled internal-RAM DMA + register projector"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
