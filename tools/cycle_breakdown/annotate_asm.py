#!/usr/bin/env python3
"""Insert a local label before every SM83 instruction of translated code in
runtime/generated.asm (after all post-passes), and write a line map.

Labels do not change the bytes: the annotated ROM must be byte-identical to
the plain build (build.sh checks this with cmp).  The map (<out>.lmap.tsv)
has one row per label:

  idx  kind  block  nes_pc  nes_op  nes_mode  tag  gb_text

kind  : block (NES block / mapper bank block), overlay, stub (mapper2
        dispatch stub)
block : enclosing global label (nes_XXXX / nes_m2_bNN_XXXX / nes_r_XXXX)
nes_* : the 6502 instruction whose '; $PC: $OP Mnem Mode' comment precedes
        the GB instruction ('-' for block prologue code)
tag   : overhead class assigned from the GB text and its context (see TAGS)

usage: annotate_asm.py generated.asm out_prefix   (rewrites generated.asm)
"""
import re, sys

MNEM = set("""ld ldh ldi ldd jp jr call ret reti push pop add adc sub sbc and or
xor cp inc dec rla rra rlca rrca rl rr rlc rrc sla sra srl swap bit set res
cpl ccf scf daa nop halt stop di ei rst""".split())
SEC = re.compile(r'^\s*SECTION\s+"([^"]*)"')
GLOB = re.compile(r'^([A-Za-z_]\w*):{1,2}')
NESI = re.compile(r'^\s*;\s*\$([0-9A-F]{4}): \$([0-9A-F]{2}) (\w+) (\w+)')
HR_ST = re.compile(r'^ldh \[nes_(a|x|y|sp|p|z_shadow|n_shadow|c_shadow|v_shadow)\], a')
HR_LD = re.compile(r'^ldh a, \[nes_(a|x|y|sp|p|z_shadow|n_shadow|c_shadow|v_shadow)\]')
FLAGREG = {"z_shadow", "n_shadow", "c_shadow", "v_shadow", "p"}


def classify(text, ctx):
    t = re.sub(r'\s*;.*$', '', text).strip()
    t = re.sub(r'\s+', ' ', t)
    if ctx == 'nmipoll':
        return 'nmi_poll'
    if 'sprite0_poll' in t or ctx == 'sprite0':
        return 'sprite0_poll_wait'
    if ctx == 'ovlguard':
        return 'overlay_guard'
    m = HR_ST.match(t)
    if m:
        r = m.group(1)
        return ('flag_save_' if r in FLAGREG else 'hram_save_') + r
    m = HR_LD.match(t)
    if m:
        r = m.group(1)
        return ('flag_load_' if r in FLAGREG else 'hram_load_') + r
    if 'BANK(' in t or 'nes_jump_known' in t or 'nes_restore_code_bank' in t:
        return 'bank_xfer'
    if 'nes_dispatch_hl' in t or 'nes_overlay_dispatch' in t:
        return 'dispatch_call'
    m = re.match(r'^(call|jp)( [a-z]+,)? (nes_\w+)', t)
    if m and not re.match(r'nes_(m\d_b[0-9A-F]{2}_|r_)?[0-9A-F]{4}$', m.group(3)):
        return 'helper_call'
    if re.match(r'^(push|pop) af', t):
        return 'flags_pushpop'
    return 'core'


def main():
    src, outp = sys.argv[1], sys.argv[2]
    lines = open(src).read().split('\n')
    out, rows = [], []
    kind = None; rglob = None; seck = None; first = False; glob = None; nes = ('-', '-', '-'); ctx = None
    depth_macro = 0; n = 0
    for raw in lines:
        s = raw.strip()
        up = s.split(None, 1)[0].upper() if s else ''
        if up in ('MACRO', 'REPT', 'FOR') or s.endswith(': MACRO') or re.match(r'^\w+:\s*MACRO', s) or re.match(r'^MACRO\b', s):
            depth_macro += 1
        if up in ('ENDM', 'ENDR'):
            depth_macro = max(0, depth_macro - 1)
        m = SEC.match(raw)
        if m:
            name = m.group(1)
            if re.match(r'^NES (mapper\d+ b[0-9A-F]{2} )?block ', name):
                kind = 'block'
            elif name.startswith('NES overlay block'):
                kind = 'overlay'
            elif name.startswith('NES mapper2 dispatch stub'):
                kind = 'stub'
            else:
                kind = None
            seck = kind; first = False
            glob = None; nes = ('-', '-', '-'); ctx = None
            out.append(raw); continue
        m = GLOB.match(raw)
        if m and seck:
            # fallthrough_layout moves blocks into stub sections (and stubs
            # next to blocks): classify per global label, not per section.
            rglob = m.group(1)
            if not re.match(r'^nes_(?:m\d+_b[0-9A-F]{2}_|r_)?[0-9A-F]{4}(?:_trace)?$', rglob) and glob:
                # helper-style global inside a translated block (e.g. the
                # sprite-0 poll expansion): keep the block and 6502 context
                out.append(raw); continue
            glob = rglob; nes = ('-', '-', '-')
            if re.match(r'nes_m\d+_b[0-9A-F]{2}_', glob):
                kind = 'block'
            elif seck == 'stub':
                kind = 'stub'
            else:
                kind = seck
            first = True
            ctx = 'ovlguard' if kind == 'overlay' else None
            out.append(raw); continue
        mn = re.match(r'^\s*;.*native (\w+) loop \$([0-9A-F]{4})', raw)
        if mn and kind:
            nes = (mn.group(2), 'NativeLoop', mn.group(1))
        m = NESI.match(raw)
        if m and kind:
            nes = (m.group(1), m.group(3), m.group(4))
            if ctx == 'ovlguard':
                ctx = None
        if kind and glob and not depth_macro and raw[:1] in (' ', '\t') and s and not s.startswith(';'):
            tok = s.split(None, 1)[0].lower()
            if tok in MNEM:
                if first and s.startswith('ld a, [nes_prg_bank]') and not re.match(r'nes_m\d+_b', glob):
                    kind = 'stub'
                first = False
                if s.startswith('ldh a, [nes_host_vblank_pending]'):
                    ctx = 'nmipoll'
                tag = classify(s, 'sprite0' if 'sprite0_poll' in (rglob or '') and ctx is None else ctx)
                out.append('.gq%d:' % n)
                rows.append('%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s' % (n, kind, glob, nes[0], nes[1], nes[2], tag, re.sub(r'\s+', ' ', s)))
                n += 1
                out.append(raw); continue
        if ctx == 'nmipoll' and s == ':':
            ctx = None
        out.append(raw)
    open(src, 'w').write('\n'.join(out))
    open(outp + '.lmap.tsv', 'w').write('\n'.join(rows) + '\n')
    print('annotated', n, 'instructions')


if __name__ == '__main__':
    main()
