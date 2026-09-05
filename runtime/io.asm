; NES CPU-visible I/O compatibility shims.
; Stable call targets for generated code while PPU/APU behavior is filled in.

SECTION "NES virtual state", WRAM0[$C800]
nes_ppu_status: ds 1
nes_ppuctrl:    ds 1
nes_ppumask:    ds 1
nes_dac:        ds 1

SECTION "NES IO shims", ROM0

nes_io_read_2002:
    ld a, [nes_ppu_status]
    ret

nes_io_write_2000:
    ld [nes_ppuctrl], a
    ret

nes_io_write_2001:
    ld [nes_ppumask], a
    ret

nes_io_write_4011:
    ld [nes_dac], a
    ret
