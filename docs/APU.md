# NES APU → GBC sound bridge

v1 maps the NES APU register file onto Game Boy Color sound channels.

## Layout

- Shadow regs `nes_apu_regs` at `WRAM0[$CA00]` ($18 bytes for `$4000-$4017`)
- Runtime in `runtime/apu.asm`; codegen emits `call nes_apu_write` for fixed APU stores
- Boot calls `nes_apu_init` after video init
- Profile PC trace moved from `WRAM0[$CA00]` to `WRAMX[$D000]` bank 7 so this gap is free

## Channel mapping

| NES | Registers | GBC |
|-----|-----------|-----|
| Pulse 1 | `$4000-$4003` | Square 1 `NR10-NR14` |
| Pulse 2 | `$4004-$4007` | Square 2 `NR21-NR24` (no sweep) |
| Triangle | `$4008-$400B` | Wave `NR30-NR34` + triangle wave RAM |
| Noise | `$400C-$400F` | Noise `NR41-NR44` |
| DMC load | `$4011` | `nes_dac` (codegen); optional shadow via bus write |
| Status | `$4015` | Channel enables + `nes_apu_read_status` |
| Frame | `$4017` | Shadow only |

## Frequency

NES 11-bit timer `t` → GBC period:

`n = 2048 - min(2047, ((t+1)*75)/16)` clamped to `0..2047`.

The theoretical NES→GB square ratio is `((t+1)*75)/64`; SMB listening was
about two octaves sharp, so v1 lengthens the period by 4 (`/16`).

Duty bits 6-7 map straight into `NR11`/`NR21`. Constant-volume (bit4) uses bits0-3 as `NRx2` volume; otherwise a simple envelope approximation is used. Trigger (`NRx4` bit7) on length/freq-hi writes (`$4003/$4007/$400B/$400F`) and when `$4015` enables a previously disabled channel.

## Known gaps

- DMC sample playback (`$4010/$4012/$4013`) not synthesized (load byte may touch `nes_dac`)
- Frame sequencer / IRQ from `$4017` not implemented
- Length counters and triangle linear counter are approximate (enable bits gate channels)
- Sweep unit accuracy not modeled (NR10 cleared)
- Noise period table is a rough NR43 encoding (shifted ~2 octaves down with pulse/triangle), not cycle-accurate
- No host-side audio buffering / resampling beyond GB hardware
