; Replaced by host recompiler output during a real conversion.
SECTION "Generated NES stub", ROM0
nes_generated_init:
    xor a
    ld [nes_chr_bank], a
    ld a, $01
    ld [nes_chr_gbc_bank_base], a
    ret

nes_nmi_entry:
    ret

nes_irq_entry:
    ret

nes_reset:
    ret
