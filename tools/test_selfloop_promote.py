#!/usr/bin/env python3
"""Regression test for tools/selfloop_promote.py.

Fixture: a counted store loop with an outlined NMI poll (the shape of the SMB
OAM-clear loop).  Checks that the pass promotes it, that the opt-out
NES2GBC_SELFLOOP_PROMOTE=0 leaves the file byte-identical, and that the pass
is idempotent (a second run changes nothing).
"""
import os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PASS = os.path.join(HERE, "selfloop_promote.py")

FIXTURE = """SECTION "NES block 8227", ROMX, BANK[1]
nes_8227:
    ldh a, [nes_host_vblank_pending]
    and a
    jr nz, .npoll_slow_4 ; outline_nmi_polls
.npoll_back_4:
    ldh a, [nes_y]
    ld c, a
    ld hl, $C200
    ld l, c
    ldh a, [nes_a]
    ld [hl], a
    inc c
    inc c
    inc c
    inc c
    ld a, c
    ldh [nes_z_shadow], a
    ldh [nes_n_shadow], a
    ldh [nes_y], a
    jr nz, nes_8227
:
    jp nes_8230
.npoll_slow_4:
    ld hl, $8227
    call nes_poll_nmi_hl
    and a
    jp nz, nes_nmi_entry
    jp .npoll_back_4
"""


def run(path, optout):
    env = dict(os.environ)
    env["NES2GBC_SELFLOOP_PROMOTE"] = "0" if optout else "1"
    subprocess.run([sys.executable, PASS, path], check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.asm")
        open(p, "w").write(FIXTURE)
        run(p, True)
        assert open(p).read() == FIXTURE, "opt-out changed the file"
        run(p, False)
        once = open(p).read()
        assert once != FIXTURE, "pass did not transform the fixture"
        for needle in (".lp_poll_4:", ".lp_body_4:", ".lp_slow_4:", "jp .npoll_slow_4"):
            assert needle in once, "missing " + needle
        run(p, False)
        assert open(p).read() == once, "pass is not idempotent"
    print("test_selfloop_promote: OK")


if __name__ == "__main__":
    main()
