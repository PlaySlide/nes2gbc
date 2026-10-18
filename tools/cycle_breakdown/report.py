#!/usr/bin/env python3
"""Turn PREFIX.breakdown.json files into the markdown tables of
docs/CYCLE_BREAKDOWN.md.  usage: report.py DIR g1:Name1 g2:Name2 ..."""
import sys, json, collections, re

D = sys.argv[1]
games = [a.split(':') for a in sys.argv[2:]]
G = {n: json.load(open('%s/%s.breakdown.json' % (D, g))) for g, n in games}

# ideal T-cycles per 6502 instruction with A/X/Y/flags in GB registers and NES
# RAM at a fixed WRAM base (see doc text for assumptions)
IDEAL_MODE = {'Immediate': 8, 'ZeroPage': 16, 'Absolute': 16, 'ZeroPageX': 32, 'ZeroPageY': 32,
              'AbsoluteX': 40, 'AbsoluteY': 40, 'IndirectIndexed': 64, 'IndexedIndirect': 64,
              'Accumulator': 8, 'Implied': 4, 'Relative': 10, 'Indirect': 80}
IDEAL_OP = {'Jsr': 40, 'Rts': 32, 'Jmp': 8, 'Rti': 60, 'Pha': 24, 'Pla': 28, 'Php': 40, 'Plp': 40,
            'Brk': 120, 'Nop': 0, 'Txs': 8, 'Tsx': 8, 'Bpl': 18, 'Bmi': 18, 'Bvc': 18, 'Bvs': 18}
RMW = {'Inc', 'Dec', 'Asl', 'Lsr', 'Rol', 'Ror'}
def ideal(op, mode):
    if op == 'Jmp' and mode == 'Indirect': return 80
    if op in IDEAL_OP: return IDEAL_OP[op]
    b = IDEAL_MODE.get(mode, 16)
    if op in RMW and mode not in ('Accumulator',): b += 8
    if op in ('Ldx', 'Ldy', 'Stx', 'Sty') and mode != 'Immediate': b += 4
    if op in ('Adc', 'Sbc', 'And', 'Ora', 'Eor', 'Cmp', 'Cpx', 'Cpy', 'Bit') and mode in ('ZeroPage', 'Absolute'): b += 4
    if op in ('Cmp', 'Cpx', 'Cpy', 'Sbc'): b += 4   # 6502 carry is inverted vs SM83
    return b

TAGNAME = {'nmi_poll': 'NMI poll (block prologue)', 'helper_call': 'helper call/jp instructions',
           'flags_pushpop': 'push/pop af (flag/A preservation)', 'bank_xfer': 'cross-bank trampoline setup',
           'dispatch_call': 'dispatch call setup', 'overlay_guard': 'overlay staleness guard',
           'mapper2 dispatch stub': 'mapper2 dispatch stubs', 'sprite0_poll_wait': 'sprite-0 / $2002 poll wait (inline expansion)', 'spin (6502 busy-wait loop)': '6502 busy-wait loops (spin)'}
def tagname(t):
    if t in TAGNAME: return TAGNAME[t]
    m = re.match(r'(hram|flag)_(save|load)_(\w+)', t)
    if m:
        r = {'a': 'A', 'x': 'X', 'y': 'Y', 'sp': 'SP', 'p': 'P', 'z_shadow': 'Z', 'n_shadow': 'N', 'c_shadow': 'C', 'v_shadow': 'V'}[m.group(3)]
        return ('HRAM %s %s' % ('save' if m.group(2) == 'save' else 'reload', r)) if m.group(1) == 'hram' else \
               ('flag materialize %s (store)' % r if m.group(2) == 'save' else 'flag reload %s' % r)
    return t

def centers(d):
    """every cycle, disjoint: translated core per op x mode, overhead tags,
    runtime routines"""
    c = collections.Counter()
    for k, v in d['tr_core'].items():
        c['T core: ' + (k if not k.startswith('(') else '(block prologue / native routine bodies)')] += v
    for k, v in d['tr_cycles'].items():
        if k.startswith('(unlabeled'): c['T unlabeled'] += v
    for k, v in d['tags'].items():
        c['T ovh: ' + tagname(k)] += v
    for k, v in d['runtime'].items():
        c['R: ' + k] += v
    return c

def buckets(d):
    c = collections.Counter()
    t = d['tags']
    core = sum(d['tr_core'].values())
    c['translated: core 6502 semantics'] = core
    hr = sum(v for k, v in t.items() if k.startswith('hram_'))
    fl = sum(v for k, v in t.items() if k.startswith('flag_') or k == 'flags_pushpop')
    c['translated: HRAM A/X/Y/SP save+reload'] = hr
    c['translated: flag materialize/reload'] = fl
    c['translated: NMI polls'] = t.get('nmi_poll', 0)
    c['translated: helper/dispatch/bank call setup'] = t.get('helper_call', 0) + t.get('dispatch_call', 0) + t.get('bank_xfer', 0)
    c['translated: mapper2 dispatch stubs'] = t.get('mapper2 dispatch stub', 0)
    c['translated: overlay guards'] = t.get('overlay_guard', 0)
    c['6502 busy-wait loops (spin)'] = t.get('spin (6502 busy-wait loop)', 0)
    c['translated: sprite-0 poll wait'] = t.get('sprite0_poll_wait', 0)
    c['translated: unlabeled'] = d['unlabeled']
    for k, v in d['runtime_buckets'].items():
        c['runtime: ' + k] += v
    return c

def pct(v, t): return 100.0 * v / t if t else 0.0
out = []
P = out.append
# ---------------- headline
P('## Headline numbers per game\n')
P('| Game | NES frames | GB T-cycles / NES frame | translated (incl. spin) | spin | runtime | idle/wait | NES instr / frame | T-cycles / NES instr (inline) | (inline + mem helpers) | ideal | ratio |')
P('|---|---|---|---|---|---|---|---|---|---|---|---|')
HEAD = {}
for g, n in games:
    d = G[n]; T = d['total']; NF = d['meta']['nes_frames']
    b = buckets(d)
    spin = d['tags'].get('spin (6502 busy-wait loop)', 0)
    trans = d['translated']
    rt = T - trans
    idle = d['runtime_buckets'].get('idle/wait', 0)
    nexec = sum(d['nes_exec'].values())
    # inline cycles of non-spin translated code (core+tags), excluding stubs & spin
    inline = sum(d['tr_cycles'].values())
    memh = d['runtime_buckets'].get('cpu read/write helpers', 0) + d['runtime_buckets'].get('flag helpers', 0) + \
        d['runtime_buckets'].get('dispatch + bank switch', 0) + d['tags'].get('mapper2 dispatch stub', 0)
    # spin excluded from instruction counts? nes_exec counts all blocks; recompute ideal over all
    idl = sum(v * ideal(*k.split(' ', 1)) for k, v in d['nes_exec'].items())
    HEAD[n] = dict(T=T / NF, inline=inline / max(nexec, 1), ideal=idl / max(nexec, 1), withh=(inline + memh) / max(nexec, 1), nexec=nexec / NF)
    P('| %s | %d | %.0f | %.1f%% | %.1f%% | %.1f%% | %.1f%% | %.0f | %.1f | %.1f | %.1f | %.1fx |' % (
        n, NF, T / NF, pct(trans, T), pct(spin, T), pct(rt, T), pct(idle, T), nexec / NF,
        inline / max(nexec, 1), (inline + memh) / max(nexec, 1), idl / max(nexec, 1), (inline + memh) / max(idl, 1)))
P('\nNES instructions are counted at the first GB instruction of each translated 6502 instruction (spin loops included); "inline" is all GB cycles in translated blocks (core + overhead tags, excluding mapper2 stubs and spin loops) divided by that count; "+ mem helpers" adds cpu read/write, flag, dispatch/bank helpers and stubs.\n')

# ---------------- combined top-level buckets
P('## Combined top-level split (each game weighted equally, % of executed GB cycles)\n')
allb = collections.defaultdict(dict)
for g, n in games:
    b = buckets(G[n]); T = G[n]['total']
    for k, v in b.items(): allb[k][n] = pct(v, T)
P('| Bucket | avg | ' + ' | '.join(n for _, n in games) + ' |')
P('|---|---|' + '---|' * len(games))
for k in sorted(allb, key=lambda k: -sum(allb[k].values())):
    vals = [allb[k].get(n, 0) for _, n in games]
    if max(vals) < 0.05: continue
    P('| %s | %.2f | ' % (k, sum(vals) / len(vals)) + ' | '.join('%.2f' % v for v in vals) + ' |')
P('')

# ---------------- combined cost centers
P('## Combined cost centers (each game weighted equally)\n')
allc = collections.defaultdict(dict)
for g, n in games:
    c = centers(G[n]); T = G[n]['total']
    for k, v in c.items(): allc[k][n] = pct(v, T)
rank = sorted(allc, key=lambda k: -sum(allc[k].values()))
P('| # | Cost center | avg %% | ' + ' | '.join(n for _, n in games) + ' |')
P('|---|---|---|' + '---|' * len(games))
for i, k in enumerate(rank[:40]):
    vals = [allc[k].get(n, 0) for _, n in games]
    P('| %d | %s | %.2f | ' % (i + 1, k, sum(vals) / len(vals)) + ' | '.join('%.2f' % v for v in vals) + ' |')
P('')

# ---------------- combined opcode classes
P('## Combined 6502 opcode x addressing mode (translated inline cycles incl. overhead tags)\n')
P('Per class: share of executed cycles (avg over games), dynamic NES instr per frame (avg), GB instructions per 6502 instruction, T-cycles per 6502 instruction (inline), helper/dispatch calls per instruction, ideal T-cycles, excess.\n')
P('| Class | avg % | NES instr/frame | GB instr / 6502 instr | T-cyc / instr | calls / instr | ideal | excess T-cyc/frame (avg) |')
P('|---|---|---|---|---|---|---|---|')
cls = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
for g, n in games:
    d = G[n]; T = d['total']; NF = d['meta']['nes_frames']
    for k, v in d['tr_cycles'].items():
        if k.startswith('('): continue
        e = d['nes_exec'].get(k, 0)
        x = cls[k]; x[0] += pct(v, T) / len(games); x[1] += e / NF / len(games)
        x[2] += d['gb_exec'].get(k, 0); x[3] += v; x[4] += e; x[5] += d['calls'].get(k, 0)
for k in sorted(cls, key=lambda k: -cls[k][0])[:45]:
    x = cls[k]; op, mode = k.split(' ', 1); idl = ideal(op, mode)
    cyc = x[3] / max(x[4], 1)
    P('| %s | %.2f | %.0f | %.1f | %.1f | %.2f | %d | %.0f |' % (k, x[0], x[1], x[2] / max(x[4], 1), cyc, x[5] / max(x[4], 1), idl, max(0, cyc - idl) * x[1]))
P('')

# ---------------- per game
for g, n in games:
    d = G[n]; T = d['total']; NF = d['meta']['nes_frames']
    P('## %s\n' % n)
    P('%d NES frames, %.0f GB T-cycles per NES frame (%.2f GB frames per NES frame, double speed).\n' % (NF, T / NF, d['meta']['gb_frames'] / NF))
    P('| Bucket | T-cyc/frame | % |'); P('|---|---|---|')
    for k, v in buckets(d).most_common():
        if v: P('| %s | %.0f | %.2f |' % (k, v / NF, pct(v, T)))
    P('\n| Opcode x mode | % | NES instr/frame | GB instr/instr | T-cyc/instr | calls/instr |'); P('|---|---|---|---|---|---|')
    for k, v in sorted(d['tr_cycles'].items(), key=lambda x: -x[1])[:25]:
        e = d['nes_exec'].get(k, 0)
        if k.startswith('('):
            P('| %s | %.2f | | | | |' % (k, pct(v, T))); continue
        P('| %s | %.2f | %.0f | %.1f | %.1f | %.2f |' % (k, pct(v, T), e / NF, d['gb_exec'].get(k, 0) / max(e, 1), v / max(e, 1), d['calls'].get(k, 0) / max(e, 1)))
    P('\n| Translated overhead tag | T-cyc/frame | % |'); P('|---|---|---|')
    for k, v in sorted(d['tags'].items(), key=lambda x: -x[1]):
        if pct(v, T) >= 0.05: P('| %s | %.0f | %.2f |' % (tagname(k), v / NF, pct(v, T)))
    P('\n| Runtime routine | bucket | T-cyc/frame | % |'); P('|---|---|---|---|')
    rb = __import__('breakdown_buckets') if False else None
    for k, v in sorted(d['runtime'].items(), key=lambda x: -x[1])[:25]:
        P('| %s | %s | %.0f | %.2f |' % (k, d.get('rbucket', {}).get(k, ''), v / NF, pct(v, T)))
    P('\nHottest translated blocks: ' + ', '.join('%s %.1f%%' % (b, pct(v, T)) for b, v in d['top_blocks'][:8]) + '\n')
json.dump(HEAD, open(D + '/headline.json', 'w'), indent=1)
print('\n'.join(out))
