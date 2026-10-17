#!/usr/bin/env python3
"""Regression test: dead_terminal_* liveness must follow HL-dispatch edges.

Contra E3E7 `SBC #$00 / BCS $E3ED` reaches E3ED (`STA / BNE`) through
`ld hl, $E3ED / jp nes_dispatch_hl` because the target lives in another bank.
The CFG parser used to see only `nes_XXXX` label names, missed that edge, and
dead_terminal_carry_zn dropped the Z store BNE reads (enemy HP never hit 0).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dead_terminal_carry_zn  # noqa: E402
from dead_terminal_zn import parse_blocks  # noqa: E402

ASM = """\
nes_E3E7:
    ; $E3E7: $E9 Sbc Immediate
    ld a, l
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ; $E3E9: $B0 Bcs Relative
    ldh a, [nes_c_shadow]
    and a
    jr z, :+
    ld hl, $E3ED
    jp nes_dispatch_hl
:
    ld hl, $E3EB
    jp nes_dispatch_hl

nes_E3EB:
    ; $E3EB: $A9 Lda Immediate
    ld a, $00
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ld hl, $E3ED
    jp nes_dispatch_hl

nes_E3ED:
    ; $E3ED: $99 Sta AbsoluteY
    ld [hl], a
    ; $E3F0: $D0 Bne Relative
    ldh a, [nes_z_shadow]
    and a
    jr z, :+
    ld hl, $E3FA
    jp nes_dispatch_hl
:
    ld hl, $E3F2
    jp nes_dispatch_hl

nes_E3F2:
    ; $E3F2: $A6 Ldx ZeroPage
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ; $E3F4: $4C Jmp Absolute
    jp nes_E3F2

nes_E3FA:
    ; $E3FA: $B9 Lda AbsoluteY
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ; $E3FD: $4C Jmp Absolute
    jp nes_E3FA
"""


class HlDispatchEdges(unittest.TestCase):
    def test_hl_dispatch_target_is_successor(self):
        blocks = parse_blocks(ASM.splitlines(keepends=True))
        self.assertIn(0xE3ED, blocks[0xE3E7].succ)
        self.assertIn(0xE3EB, blocks[0xE3E7].succ)

    def test_live_z_kept_before_carry_branch(self):
        lines = ASM.splitlines(keepends=True)
        dead_terminal_carry_zn.optimize(lines)
        out = "".join(lines)
        sbc = out.split("nes_E3EB:")[0]
        self.assertIn("ldh [nes_z_shadow], a", sbc)  # BNE at E3F0 reads it
        self.assertNotIn("ldh [nes_n_shadow], a", sbc)  # N is dead: still removed

    def test_unmapped_dispatch_is_unknown_exit(self):
        asm = ASM.replace("ld hl, $E3ED\n    jp nes_dispatch_hl\n:", "ld hl, $9000\n    jp nes_dispatch_hl\n:", 1)
        blocks = parse_blocks(asm.splitlines(keepends=True))
        self.assertTrue(blocks[0xE3E7].unknown_exit)


if __name__ == "__main__":
    unittest.main()
