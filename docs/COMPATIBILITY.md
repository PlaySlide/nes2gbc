# Compatibility / Debug Notes

This is a lightweight test list for commercial ROM bring-up. The current priority is
**boot and run first**, then renderer/view correctness, then mapper expansion.

## Mapper 0 / NROM

| Game | Status | Known issue / next action |
|---|---|---|
| Bomberman | Gets into stage, then fails/white-screens | Stack-built RTS dispatch CFG gaps fixed. Non-TRACE can still fall into data; TRACE currently stalls without a fault. Exact instruction breadcrumbs added to isolate it. |
| Dig Dug | Boots and runs | Severe intermittent renderer/camera jump makes the screen move around. Defer until NROM boot coverage is broader. |
| Lode Runner | Boots and runs | Sprite following does not acquire the player. PocketNES Menu Maker DB has no follow value for the US/JP Lode Runner entries, so this needs a derived hint or generic tracker improvement. |
| Tennis | Boots and runs | NES 240-line scene is heavily cropped on the 144-line GBC viewport. Revisit with fit-screen work. |

## Bring-up policy

1. Fix shared CFG/runtime causes that prevent boot/gameplay.
2. Record renderer/view glitches instead of blocking compatibility bring-up.
3. After NROM coverage is broad, return to rendering correctness.
4. Then add Mapper 2/UxROM (Mega Man, Contra), followed by Mapper 1/MMC1 (Zelda).
