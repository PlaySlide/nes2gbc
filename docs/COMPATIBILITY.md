# Compatibility / Debug Notes

This is a lightweight test list for commercial ROM bring-up. The current priority is
**boot and run first**, then renderer/view correctness, then mapper expansion.

## Mapper 0 / NROM

| Game | Status | Known issue / next action |
|---|---|---|
| Bomberman | Title boots; gameplay transition currently faults | Computed sound dispatch uses stack-built RTS jump tables. CFG support in progress on `fix/nrom-inline-dispatch-x-temp`. |
| Dig Dug | Boots and runs | Severe intermittent renderer/camera jump makes the screen move around. Defer until NROM boot coverage is broader. |
| Tennis | Boots and runs | NES 240-line scene is heavily cropped on the 144-line GBC viewport. Revisit with fit-screen work. |

## Bring-up policy

1. Fix shared CFG/runtime causes that prevent boot/gameplay.
2. Record renderer/view glitches instead of blocking compatibility bring-up.
3. After NROM coverage is broad, return to rendering correctness.
4. Then add Mapper 2/UxROM (Mega Man, Contra), followed by Mapper 1/MMC1 (Zelda).
