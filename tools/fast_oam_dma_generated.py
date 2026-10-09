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
        "    ; fully unrolled 256-byte copy (no loop counter)\n",
    ]

    for _ in range(256):
        out.extend([
            "    ld a, [hli]\n",
            "    ld [de], a\n",
            "    inc e\n",
        ])

    out.extend([
        "\n",
        "    ; Canonical OAM is now complete. Keep camera semantics identical:\n",
        "    ; follow first, then project the finished 64-entry NES OAM image.\n",
        "    call nes_video_build_oam_shadow_fast\n",
        "    ld a, $01\n",
        "    ld [nes_oam_dirty], a\n",
        "    ret\n",
        "\n",
        '; Fast steady-state projector for the normal $4014 DMA path.\n',
        '; HL walks canonical NES OAM $C900 (page aligned, so L wrapping to 0 ends the\n',
        '; 64 entries); DE walks the page-aligned CGB shadow (E < $A0, so inc e).\n',
        '; Separate 8x8 / 8x16 loops hoist the per-sprite PPUCTRL test; C holds the\n',
        '; view Y inside the loops.\n',
        'nes_video_build_oam_shadow_fast:\n',
        '    call nes_view_follow_update\n',
        '    ld hl, nes_oam_ram\n',
        'IF !DEF(NES2GBC_NO_PACING)\n',
        '    xor a\n',
        '    ld [nes_oam_live_stale], a\n',
        '    ld a, [nes_oam_live_page]\n',
        '    ld d, a\n',
        '    ld e, 0\n',
        'ELSE\n',
        '    ld de, nes_gbc_oam_shadow\n',
        'ENDC\n',
        '    ldh a, [nes_view_y]\n',
        '    ld c, a\n',
        '    ld a, [nes_ppuctrl]\n',
        '    bit 5, a\n',
        '    jp nz, .scan16\n',
        '    ; 8x8 sprites: CGB VRAM bank from PPUCTRL bit 3 selects table page.\n',
        '    and $08\n',
        '    rrca\n',
        '    rrca\n',
        '    rrca\n',
        '    add HIGH(nes_oam_attr_table)\n',
        '    ld [nes_oam_attr_page8], a\n',
        '    ldh [nes_oam_ppuctrl_tmp], a ; (the slow projector re-seeds this)\n',
        '\n',
        '    ; Unrolled 8x8 scan over the fixed canonical OAM: absolute loads, H =\n',
        '    ; attribute table page, B = view X, C = view Y, DE = CGB shadow.\n',
        '    ld h, a\n',
        '    ldh a, [nes_view_x]\n',
        '    ld b, a\n',
    ] + [
        line for i in range(64) for line in (
            f'    ld a, [nes_oam_ram + ${4 * i:02X}] ; Y\n',
            '    cp $EF\n',
            f'    jr nc, .n8_{i}\n',
            '    inc a\n',
            '    sub c ; C = view Y\n',
            f'    jr c, .n8_{i}\n',
            '    cp $90\n',
            f'    jr nc, .n8_{i}\n',
            '    add $10\n',
            '    ld [de], a ; speculative Y (E only advances once X is visible)\n',
            f'    ld a, [nes_oam_ram + ${4 * i + 3:02X}] ; X\n',
            '    sub b ; B = view X\n',
            f'    jr c, .n8_{i}\n',
            '    cp $A0\n',
            f'    jr nc, .n8_{i}\n',
            '    add $08\n',
            '    inc e\n',
            '    ld [de], a\n',
            '    inc e\n',
            f'    ld a, [nes_oam_ram + ${4 * i + 1:02X}] ; tile\n',
            '    ld [de], a\n',
            '    inc e\n',
            f'    ld a, [nes_oam_ram + ${4 * i + 2:02X}] ; attributes\n',
            '    ld l, a\n',
            '    ld a, [hl] ; 8x8 attribute table page in H\n',
            '    ld [de], a\n',
            '    inc e\n',
        ) + ((
            # Forty emitted CGB sprites is the hardware limit; entry i can
            # only be the 40th emission when i >= 39.
            '    ld a, e\n',
            '    cp $A0\n',
            '    jp z, .full_ready\n',
        ) if i >= 39 else ()) + (f'.n8_{i}:\n',)
    ] + [
        '    jp .clear_unused\n',
        '\n',
        '.scan16:\n',
        '    ld a, [hli] ; Y, HL -> tile\n',
        '    cp $EF\n',
        '    jr nc, .skip316\n',
        '.vis16: ; A = Y, HL -> tile\n',
        '    inc a\n',
        '    sub c ; C = view Y\n',
        '    jr c, .skip316\n',
        '    cp $90\n',
        '    jr nc, .skip316\n',
        '    add $10\n',
        '    ld [de], a ; speculative Y (E only advances once X is visible)\n',
        '    inc l\n',
        '    inc l ; HL -> X\n',
        '    ldh a, [nes_view_x]\n',
        '    ld b, a\n',
        '    ld a, [hl]\n',
        '    sub b\n',
        '    jr c, .skip116\n',
        '    cp $A0\n',
        '    jr nc, .skip116\n',
        '    add $08\n',
        '    inc e\n',
        '    ld [de], a\n',
        '    inc e\n',
        '    dec l\n',
        '    dec l ; HL -> tile\n',
        '    ld a, [hli] ; tile, HL -> attributes\n',
        '    ld b, a\n',
        '    and $FE\n',
        '    ld [de], a\n',
        '    inc e\n',
        '    ld a, b\n',
        '    and $01\n',
        '    add HIGH(nes_oam_attr_table)\n',
        '    ld b, a\n',
        '    ld a, [hli] ; attributes, HL -> X\n',
        '    ld c, a\n',
        '    ld a, [bc]\n',
        '    ld [de], a\n',
        '    inc e\n',
        '    inc l ; HL -> next Y\n',
        '    ldh a, [nes_view_y]\n',
        '    ld c, a\n',
        '    ; Forty emitted CGB sprites is the hardware limit.\n',
        '    ld a, e\n',
        '    cp $A0\n',
        '    jr z, .full_ready\n',
        '    ld a, l\n',
        '    and a\n',
        '    jr nz, .scan16\n',
        '    jr .clear_unused\n',
        '.skip116: ; HL -> X\n',
        '    inc l\n',
        '    ld a, l\n',
        '    and a\n',
        '    jr nz, .scan16\n',
        '    jr .clear_unused\n',
        '.skip316: ; HL -> tile; L wrapping to 0 ends the 64-entry scan\n',
    ] + [
        # Hidden sprites come in runs: unrolled skip without the loop jump.
        line for _ in range(4) for line in (
            '    inc l\n', '    inc l\n', '    inc l ; HL -> next Y, Z on wrap\n',
            '    jr z, .skipend16\n',
            '    ld a, [hli] ; Y, HL -> tile\n',
            '    cp $EF\n',
            '    jr c, .vis16\n',
        )
    ] + [
        '    jr .skip316\n',
        '.skipend16:\n',
        '    jr .clear_unused\n',
        '\n',
        ".clear_unused:\n",
        "    ; Preserve baseline final emit-count scratch once per frame.\n",
        "    ld a, e\n",
        "    srl a\n",
        "    srl a\n",
        "    ldh [nes_oam_emit_count], a\n",
        "    ld b, a\n",
        "    ; Hide unused CGB entries: Y=0 is fully offscreen for 8x8 and 8x16,\n",
        "    ; so only Y is cleared, and only up to this page's high-water mark\n",
        "    ; (entries above it are already hidden). Entry E/4 itself is always\n",
        "    ; cleared: the scan may have left a speculative Y there.\n",
        "    ld a, b\n",
        "    cp 40\n",
        "    jr z, .ready\n",
        "    ld a, d\n",
        "    and $01\n",
        "    add LOW(nes_oam_page_hw)\n",
        "    ld l, a\n",
        "    ld h, HIGH(nes_oam_page_hw)\n",
        "    ld a, [hl]\n",
        "    ld [hl], b\n",
        "    sub b\n",
        "    jr z, .clear_one\n",
        "    jr nc, .clear_n\n",
        ".clear_one:\n",
        "    ld a, 1\n",
        ".clear_n:\n",
        "    ld c, a\n",
        "    ld h, d\n",
        "    ld l, e\n",
        "    xor a\n",
        ".clear_loop:\n",
        "    ld [hl], a\n",
        "    inc l\n",
        "    inc l\n",
        "    inc l\n",
        "    inc l\n",
        "    dec c\n",
        "    jr nz, .clear_loop\n",
        "    jr .ready\n",
        "\n",
        ".full_ready:\n",
        "    ld a, 40\n",
        "    ldh [nes_oam_emit_count], a\n",
        "    ld b, a\n",
        "    ld a, d\n",
        "    and $01\n",
        "    add LOW(nes_oam_page_hw)\n",
        "    ld l, a\n",
        "    ld h, HIGH(nes_oam_page_hw)\n",
        "    ld [hl], b\n",
        "\n",
        ".ready:\n",
        "    ld a, $01\n",
        "    ldh [nes_oam_shadow_ready], a\n",
        "    ret\n",
    ])
    out.append(attr_table())
    return "".join(out)


def gbc_attr(nes_attr: int, bank: int) -> int:
    """NES OAM attribute -> CGB OAM flags (palette, bank, flips, priority)."""
    v = (nes_attr & 0x03) | (bank << 3)
    if nes_attr & 0x20:
        v |= 0x80  # behind background
    if nes_attr & 0x40:
        v |= 0x20  # horizontal flip
    if nes_attr & 0x80:
        v |= 0x40  # vertical flip
    return v


def attr_table() -> str:
    out = [
        # Fixed address: NES internal RAM ($C000-$C7FF) must never host
        # floating runtime variables.
        "\nSECTION \"Generated fast OAM attr page\", WRAM0[$CBE1]\n",
        "nes_oam_attr_page8: ds 1\n",
        "\nSECTION \"Generated OAM attribute table\", ROM0, ALIGN[8]\n",
        "nes_oam_attr_table:\n",
    ]
    for bank in (0, 1):
        for row in range(0, 256, 16):
            vals = ", ".join(f"${gbc_attr(a, bank):02X}" for a in range(row, row + 16))
            out.append(f"    db {vals}\n")
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
    if any("\nnes_video_build_oam_shadow_fast:\n" in line for line in lines):
        # Let the runtime's banked-mapper paths use the register projector
        # too (only referenced under NES2GBC_RAM_INTERP; NROM output unchanged).
        cfg = args.asm.parent / "generated_config.inc"
        text = cfg.read_text() if cfg.exists() else ""
        if "NES2GBC_FAST_OAM_PROJECTOR" not in text:
            cfg.write_text(text + "DEF NES2GBC_FAST_OAM_PROJECTOR EQU 1 ; tools/fast_oam_dma_generated.py\n")
    print(
        f"oam-dma-fast: redirected {rewritten} generated $4014 call site(s) to "
        "exact 16-byte-unrolled internal-RAM DMA + register projector"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
