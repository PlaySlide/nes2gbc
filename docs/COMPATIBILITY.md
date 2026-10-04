# Compatibility / Debug Notes

This is a lightweight test list for commercial ROM bring-up. The current priority is
**boot and run first**, then renderer/view correctness, then mapper expansion.

## Mapper 0 / NROM

| Game | Status | Known issue / next action |
|---|---|---|
| Bomberman | Boots, starts stages, plays through deaths/game over/password | Pushed RTS continuation ($CFE3) fixed. Camera follows an enemy instead of the player (no PocketNES follow hint; generic acquisition), so the player is usually off-screen. Death/transition frame shows black field briefly. |
| Dig Dug | Boots and runs | Severe intermittent renderer/camera jump makes the screen move around. Defer until NROM boot coverage is broader. |
| Lode Runner | Boots and runs | Sprite following does not acquire the player. PocketNES Menu Maker DB has no follow value for the US/JP Lode Runner entries, so this needs a derived hint or generic tracker improvement. |
| Tennis | Boots and runs | NES 240-line scene is heavily cropped on the 144-line GBC viewport. Revisit with fit-screen work. |
| Excitebike | Boots, track select, races (timer runs, track scrolls); 30k-frame soak clean | Gapped/shared-tail/split jump tables fixed. Title screen lower half corrupted; status bar split shows green garbage on some frames; player bike sprite not visible during race (view/follow). ~45% native speed. |
| Kung Fu | Boots, 1P game, plays (score/time/enemies); 30k-frame soak clean | Gapped table tail + exclusive-pointer JMP ($5A) fixed. ~42% native speed. |
| Ice Hockey | Boots, team/speed/time select, lineup, faceoff and play; 30k-frame soak clean | Dereferencing dispatcher tables ($8359) fixed. ~32% native speed. |

## Mapper 3 / CNROM

| Game | Status | Known issue / next action |
|---|---|---|
| Donkey Kong Classics | Both DK and DK Jr. boot and play | None seen in a 2.4k-frame run. |

## Not yet supported

Mega Man (mapper 2) and The Legend of Zelda (mapper 1) are rejected by CFG
discovery (mapper check) until UxROM/MMC1 support lands.

## Bring-up policy

1. Fix shared CFG/runtime causes that prevent boot/gameplay.
2. Record renderer/view glitches instead of blocking compatibility bring-up.
3. After NROM coverage is broad, return to rendering correctness.
4. Then add Mapper 2/UxROM (Mega Man, Contra), followed by Mapper 1/MMC1 (Zelda).
