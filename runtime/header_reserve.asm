; Reserve the cartridge logo/header payload written later by rgbfix.
; Without this section, rgblink may place unpinned ROM0 helpers in $0104-$014F;
; PROFILE=1 makes the profile helper large enough to trigger that placement.
SECTION "Cartridge header reserve", ROM0[$0104]
    ds $4C, $00
