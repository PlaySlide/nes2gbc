# NES APU → GBC sound bridge

The runtime maps the NES APU register file onto Game Boy Color sound channels while keeping NES-side channel state in software.

## Layout

- Shadow regs `nes_apu_regs` at `WRAM0[$CA00]` (`$4000-$4017`)
- Runtime in `runtime/apu.asm`; codegen emits `call nes_apu_write` for fixed APU stores
- Build flag: sound is off by default (`APU=0`, silent, no APU code or cost); build with `make gbc ROM=... APU=1` to enable it (see README "Sound (APU=1)")
- Boot calls `nes_apu_init` after video init
- `$4015` reads are derived from the emulated length counters

## Channel mapping

| NES | Registers | GBC |
|-----|-----------|-----|
| Pulse 1 | `$4000-$4003` | Square 1 `NR10-NR14` |
| Pulse 2 | `$4004-$4007` | Square 2 `NR21-NR24` |
| Triangle | `$4008-$400B` | Wave `NR30-NR34` + triangle wave RAM |
| Noise | `$400C-$400F` | Noise `NR41-NR44` |
| DMC load | `$4011` | `nes_dac` only |
| Status | `$4015` | NES channel enable/length state |
| Frame | `$4017` | Software 4-step/5-step frame sequencer |

## Frequency

For pulse and triangle channels, NES timer `t` maps to the GBC frequency-register divisor with the theoretical NTSC conversion:

`divisor = min(2047, ((t+1)*75)/64)`

`gb_frequency_register = 2048 - divisor`

There is no title-specific pitch correction in the normal build.

## Host fast-forward test compensation

When translated games are still too slow to test comfortably at mGBA 1x, build-time compensation can keep the GBC audio close to normal wall-clock pitch/timing while mGBA runs faster:

```sh
make gbc ROM="path/to/game.nes" APU=1 APU_TEST_SPEED=2
```

Supported values are `1`, `2`, and `4`; default is `1`.

`APU_TEST_SPEED=2` lowers pulse/triangle/noise rendering by one octave, and stretches the GBC envelope approximation by 2. `APU_TEST_SPEED=4` applies the corresponding two-octave / 4x timing compensation. This is a testing aid only: it does not change NES register state or the normal 1x APU model. (The frame sequencer is now per NES frame, so APU_TEST_SPEED only affects pitch and envelopes.)

## Current semantics

- Length counters use the NES length table and clock on half-frame steps.
- Triangle uses a software linear counter and reload flag.
- Pulse sweep uses software divider/reload state, including pulse-1 one's-complement negate and pulse-2 two's-complement negate.
- Pulse timers below 8 and positive sweep targets above `$7FF` mute output.
- Noise periods map to the nearest practical GBC `NR43` clock/LFSR setting.
- Constant volume maps directly to GBC volume; NES envelope mode uses the closest practical GBC hardware-envelope timing.

## Known gaps

- DMC sample playback (`$4010/$4012/$4013`) is not synthesized.
- Frame IRQ generation/timing from `$4017` is not implemented cycle-accurately.
- GBC hardware envelopes are only an approximation of the NES envelope generator.
- Noise uses the GBC LFSR, so period/short-mode behavior cannot be cycle-identical to the NES.
- There is no host-side audio buffering/resampling beyond GBC hardware.

## Code placement

The APU bridge is in ROM0, so APU register writes never switch ROM banks. To make room, APU=1 builds move boot-only init (`Start` RAM/HRAM setup, now `nes_boot_init_banked`) to helper bank $FF and drop the DEBUG_TRACE-only `nes_diag_snapshot_frame`; APU=0 output is unchanged. The frame sequencer runs one NES frame (four ~240 Hz steps) per delivered NES NMI, from `nes_poll_nmi_hl`, so sound timing follows emulated NES frames and scales with emulator speed instead of host VBlanks. Rewrites of an unchanged value to side-effect-free registers (even $4000-$400E, $4010-$4013) are skipped.
