#!/usr/bin/env python3
"""Attribute every profiled GB cycle of one run to translated-code classes
(6502 opcode x addressing mode, overhead tags) and runtime routines.

usage: breakdown.py PREFIX GAME  ->  PREFIX.breakdown.json + markdown on stdout
PREFIX.{gbc,sym,lmap.tsv,cfg.txt,npy,json} come from build.sh/profile_run.py."""
import sys, re, json, bisect, collections
import numpy as np

pre, game = sys.argv[1], sys.argv[2]
prof = np.load(pre + '.npy'); meta = json.load(open(pre + '.json'))
NF = meta['nes_frames']; rom = open(pre + '.gbc', 'rb').read()
FRAME_T = 70224 * (2 if meta['double_speed'] else 1)

# ---- SM83 timing (T-cycles); conditional: (not taken, taken)
def opinfo(b0, b1):
    """(length, cost or (nt, taken))"""
    if b0 == 0xCB:
        return 2, (12 if (b1 & 7) == 6 and 0x40 <= b1 < 0x80 else 16 if (b1 & 7) == 6 else 8)
    x = b0
    if x in (0x20, 0x28, 0x30, 0x38): return 2, (8, 12)
    if x in (0xC0, 0xC8, 0xD0, 0xD8): return 1, (8, 20)
    if x in (0xC2, 0xCA, 0xD2, 0xDA): return 3, (12, 16)
    if x in (0xC4, 0xCC, 0xD4, 0xDC): return 3, (12, 24)
    T = {0x00: (1, 4), 0x08: (3, 20), 0x10: (2, 4), 0x18: (2, 12), 0x27: (1, 4), 0x2F: (1, 4), 0x37: (1, 4),
         0x3F: (1, 4), 0x34: (1, 12), 0x35: (1, 12), 0x36: (2, 12), 0x76: (1, 4), 0xC3: (3, 16), 0xC9: (1, 16),
         0xD9: (1, 16), 0xCD: (3, 24), 0xE0: (2, 12), 0xF0: (2, 12), 0xE2: (1, 8), 0xF2: (1, 8), 0xE8: (2, 16),
         0xE9: (1, 4), 0xEA: (3, 16), 0xFA: (3, 16), 0xF3: (1, 4), 0xFB: (1, 4), 0xF8: (2, 12), 0xF9: (1, 8)}
    if x in T: return T[x]
    lo = x & 0x0F
    if x < 0x40:
        if lo == 1: return 3, 12
        if lo in (2, 0xA, 3, 9, 0xB): return 1, 8
        if lo in (4, 5, 0xC, 0xD): return 1, 4
        if lo in (6, 0xE): return 2, 8
        if lo in (7, 0xF): return 1, 4
    if 0x40 <= x < 0xC0:
        return 1, (8 if (x & 7) == 6 or 0x70 <= x <= 0x77 else 4)
    if lo in (1,): return 1, 12          # pop
    if lo in (5,): return 1, 16          # push
    if lo in (6, 0xE): return 2, 8       # alu d8
    if lo in (7, 0xF): return 1, 16      # rst
    return 1, 4

def key_of(b, o):
    if o < 0x4000: return o
    if o < 0x8000: return b * 0x4000 + o - 0x4000
    return 0x400000 + o

# ---- symbols
gq = {}; glob = []
for l in open(pre + '.sym'):
    p = l.split()
    if len(p) < 2 or ':' not in p[0]: continue
    b, o = p[0].split(':'); k = key_of(int(b, 16), int(o, 16))
    m = re.search(r'\.gq(\d+)$', p[1])
    if m: gq[k] = int(m.group(1)); continue
    if '.' not in p[1]: glob.append((k, p[1], int(o, 16)))
glob.sort(); gkeys = [g[0] for g in glob]
rows = {}
for l in open(pre + '.lmap.tsv'):
    f = l.rstrip('\n').split('\t')
    if len(f) >= 8: rows[int(f[0])] = f
BLK = re.compile(r'^nes_(?:m\d_b[0-9A-F]{2}_|r_)?([0-9A-F]{4})(?:_trace)?$')

# ---- spin blocks (6502 busy-wait loops), as work.py
PURE = {"Lda", "Ldx", "Ldy", "Bit", "And", "Cmp", "Cpx", "Cpy", "Beq", "Bne", "Bpl", "Bmi", "Bcc", "Bcs", "Bvc", "Bvs", "Jmp"}
blocks = {}; cur = None
try:
    for l in open(pre + '.cfg.txt'):
        f = l.split()
        if not f: continue
        if f[0] == 'B': cur = int(f[1], 16); blocks.setdefault(cur, {'ins': [], 'e': []})
        elif f[0] == 'I' and cur is not None: blocks[cur]['ins'].append((f[2], f[3] if len(f) > 3 else ''))
        elif f[0] == 'E' and cur is not None: blocks[cur]['e'].append((f[1], None if f[2] == '-' else int(f[2], 16)))
except FileNotFoundError:
    pass
pure = {s for s, b in blocks.items() if b['ins'] and all(m in PURE and not (m == 'Jmp' and mo == 'Indirect') for m, mo in b['ins'])}
succ = {s: [t for k, t in blocks[s]['e'] if k in ('branch', 'fall', 'jump') and t in pure] for s in pure}
spin = set()
for s in pure:
    st = [(t, 1) for t in succ[s]]; seen = set()
    while st:
        x, d = st.pop()
        if x == s: spin.add(s); break
        if x in seen or d > 6: continue
        seen.add(x); st += [(t, d + 1) for t in succ[x]]

# ---- runtime buckets
BUCKETS = [
    ('idle/wait', r'wait|halt|idle|sleep'),
    ('frame pacing', r'pace'),
    ('dispatch + bank switch', r'dispatch|jump_known|restore_code_bank|jmp_indirect|rti_fast|switch_bank'),
    ('interpreter', r'interp'),
    ('cpu read/write helpers', r'cpu_read|cpu_write|prg_read|conflict|stack_|zp_|mem_'),
    ('flag helpers', r'materialize_p|set_p|_p_from'),
    ('APU', r'apu|snd|sound'),
    ('OAM', r'oam'),
    ('nametable/stitch', r'nametable|stitch|seam|attr|mirror|_nt_'),
    ('PPU reg emulation', r'ppu|chr_ram|inline_ppustatus'),
    ('follow camera', r'follow|view'),
    ('video other', r'video|palette|scroll|chr|gbc_'),
    ('controller', r'controller|input|joy'),
    ('NMI/vblank/ISR', r'nmi|vblank|isr|rti|brk|poll|irq'),
]
def bucket(name):
    for b, rx in BUCKETS:
        if re.search(rx, name, re.I): return b
    return 'runtime other'

tr = collections.Counter()          # translated: (class key) -> cycles
tags = collections.Counter()        # translated overhead tags -> cycles
rt = collections.Counter(); rtb = collections.Counter()
nes_exec = collections.Counter()    # (op, mode) -> NES instr executions
gb_exec = collections.Counter()     # (op, mode) -> GB instr executions (inline)
tr_core = collections.Counter()     # (op, mode) -> inline cycles
calls_by_cls = collections.Counter()
helper_calls = collections.Counter()
spin_cyc = 0; blk_cyc = collections.Counter(); unl = 0
group_first = {}                    # (block, nes_pc) -> exec of first row
total = int(prof.sum())
ex_of_row = {}
nz = np.nonzero(prof)[0]
for k in nz:
    c = int(prof[k])
    if k in gq:
        r = rows[gq[k]]; _, kind, blk, pc, op, mode, tag, text = r[:8]
        if k < 0x400000:
            ln, cost = opinfo(rom[k], rom[k + 1] if k + 1 < len(rom) else 0)
        else:
            ln, cost = 1, 4
        ex = c / cost if isinstance(cost, int) else None
        ex_of_row[gq[k]] = (ex, c, cost)
    else:
        i = bisect.bisect_right(gkeys, k) - 1
        n = glob[i][1] if i >= 0 else '?'
        if BLK.match(n): unl += c; tr[('(unlabeled translated)', '')] += c; continue
        if k >= 0x400000: n = 'RAM code near ' + n
        rt[n] += c; rtb[bucket(n)] += c

# second pass over labelled rows in index order: infer exec counts of
# conditional instructions from the previous row in the same NES instruction
prev = None
for idx in sorted(ex_of_row):
    ex, c, cost = ex_of_row[idx]
    r = rows[idx]; _, kind, blk, pc, op, mode, tag, text = r[:8]
    gk = (blk, pc)
    if ex is None:
        e0 = ex_of_row.get(idx - 1, (None,))[0] if prev == gk else None
        if e0 is None: e0 = group_first.get(gk)
        if e0 is None: e0 = c / cost[1]
        ex = e0
        ex_of_row[idx] = (ex, c, cost)
    if gk not in group_first and pc != '-':
        group_first[gk] = ex; nes_exec[(op, mode)] += ex
    prev = gk
    m = BLK.match(blk); bpc = int(m.group(1), 16) if m else None
    if kind == 'stub':
        tags['mapper2 dispatch stub'] += c; continue
    if bpc in spin:
        spin_cyc += c; tags['spin (6502 busy-wait loop)'] += c; continue
    cls = (op, mode) if pc != '-' else ('(block prologue)', '')
    if tag == 'core':
        tr_core[cls] += c; gb_exec[cls] += ex
    else:
        tags[tag] += c; gb_exec[cls] += ex
        if tag in ('helper_call', 'dispatch_call', 'bank_xfer'):
            calls_by_cls[cls] += ex
            mm = re.search(r'(nes_\w+)', text)
            if mm and tag == 'helper_call': helper_calls[mm.group(1)] += ex
    tr[cls] += c
    blk_cyc[blk] += c

trans_total = sum(tr.values()) + sum(v for t, v in tags.items() if t in ('mapper2 dispatch stub', 'spin (6502 busy-wait loop)'))
out = dict(game=game, meta=meta, total=total, per_frame=total / NF, frame_budget=meta['gb_frames'] * FRAME_T / NF,
           translated=trans_total, unlabeled=unl,
           tr_cycles={'%s %s' % k: v for k, v in tr.items()},
           tr_core={'%s %s' % k: v for k, v in tr_core.items()},
           nes_exec={'%s %s' % k: v for k, v in nes_exec.items()},
           gb_exec={'%s %s' % k: v for k, v in gb_exec.items()},
           calls={'%s %s' % k: v for k, v in calls_by_cls.items()},
           tags=dict(tags), runtime=dict(rt), rbucket={n: bucket(n) for n in rt}, runtime_buckets=dict(rtb),
           helper_calls=dict(helper_calls),
           top_blocks=blk_cyc.most_common(15))
json.dump(out, open(pre + '.breakdown.json', 'w'), indent=0)
print('%s: %d NES frames, %.0f GB T-cycles/NES frame executed (budget %.0f), translated %.1f%%' % (
    game, NF, total / NF, out['frame_budget'], 100 * trans_total / total))
