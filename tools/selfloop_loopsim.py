#!/usr/bin/env python3
"""Differential tester for selfloop_promote.py: mini LR35902 simulator.
For every loop the pass transforms in <asm>, run the ORIGINAL and TRANSFORMED section from the same random
initial state (regs, flags, HRAM, WRAM) with scripted poll/vblank events, and require identical observable
event sequences (polls: HL + B/C/D/E + HRAM + WRAM; exits/NMI: all regs incl. A and F + HRAM + WRAM).
usage: selfloop_loopsim.py asm_without_pass [trials]  (asm built with NES2GBC_SELFLOOP_PROMOTE=0)"""
import sys, re, random, subprocess, shutil, os, hashlib, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gbregs import code

class Halt(Exception): pass

def h8(s): return int(hashlib.md5(s.encode()).hexdigest()[:2], 16)
def h16(s): return int(hashlib.md5(s.encode()).hexdigest()[:4], 16)

def parse_region(lines):
    ins = []; labels = {}; anon = []
    skip = 0
    for l in lines:
        c = code(l)
        if not c: continue
        if c.startswith('IF DEF(NES2GBC_PROFILE_TRACE)') or c.startswith('IF DEF(') or c.startswith('IF !DEF('):
            skip += 1; continue
        if c == 'ENDC':
            skip -= 1; continue
        if skip: continue
        if c.startswith('PROFILE_INC'): continue
        if c.startswith('SECTION'): continue
        if c == ':':
            anon.append(len(ins)); labels.setdefault(':%d' % (len(anon)-1), len(ins)); continue
        m = re.match(r'^([.\w]+):{1,2}$', c)
        if m: labels[m.group(1)] = len(ins); continue
        ins.append(c)
    return ins, labels, anon

class Sim:
    def __init__(self, ins, labels, anon, rng_init, script, head):
        self.ins, self.labels, self.anon = ins, labels, anon
        self.r = dict(rng_init['regs']); self.f = dict(rng_init['flags'])
        self.hram = dict(rng_init['hram']); self.ram = dict(rng_init['ram'])
        self.seed = rng_init['seed']; self.script = script; self.sc = 0; self.pc = labels[head]
        self.events = []; self.steps = 0
    # memory
    def rd(self, a):
        a &= 0xFFFF
        if a >= 0xFF00:
            return self.hram.setdefault(a, h8('hr%d_%d' % (self.seed, a)))
        return self.ram.setdefault(a, h8('ram%d_%d' % (self.seed, a)))
    def wr(self, a, v):
        a &= 0xFFFF
        (self.hram if a >= 0xFF00 else self.ram)[a] = v & 255
    def hl(self): return (self.r['h'] << 8) | self.r['l']
    def val(self, o):
        o = o.replace(' ', '').lower()
        if o in self.r: return self.r[o]
        if o == '[hl]': return self.rd(self.hl())
        if o.startswith('['): return self.rd(self.addr(o[1:-1]))
        n = self.num(o)
        return n & 255
    def num(self, t):
        t = t.strip()
        if t.startswith('$'): return int(t[1:], 16)
        if re.fullmatch(r'-?\d+', t): return int(t)
        if t.startswith('%'): return int(t[1:], 2)
        return h16(t)
    def addr(self, t):
        t = t.strip()
        if t.startswith('$') or t.isdigit(): return self.num(t)
        return 0xFF00 + h8('hv' + t)   # symbolic ldh var -> hram slot
    def snap(self, full):
        d = {k: self.r[k] for k in 'bcdehl'}
        if full:
            d['a'] = self.r['a']; d['f'] = tuple(sorted(self.f.items()))
        return (tuple(sorted(d.items())), tuple(sorted(self.hram.items())), tuple(sorted(self.ram.items())))
    def ev(self, kind, full, extra=''):
        self.events.append((kind, extra, self.snap(full)))
    def target(self, t, fwd_from=None):
        t = t.strip()
        if t in (':+', ':-', ':++', ':--'):
            raise Halt('anon')  # resolved by caller
        return t
    def flagz(self, v): self.f['z'] = int((v & 255) == 0)
    def alu(self, op, a, b):
        c = self.f['c']
        if op == 'add': r = a + b; self.f.update(n=0, h=int((a & 15) + (b & 15) > 15), c=int(r > 255))
        elif op == 'adc': r = a + b + c; self.f.update(n=0, h=int((a & 15) + (b & 15) + c > 15), c=int(r > 255))
        elif op in ('sub', 'cp'): r = a - b; self.f.update(n=1, h=int((a & 15) < (b & 15)), c=int(r < 0))
        elif op == 'sbc': r = a - b - c; self.f.update(n=1, h=int((a & 15) < (b & 15) + c), c=int(r < 0))
        elif op == 'and': r = a & b; self.f.update(n=0, h=1, c=0)
        elif op == 'or': r = a | b; self.f.update(n=0, h=0, c=0)
        elif op == 'xor': r = a ^ b; self.f.update(n=0, h=0, c=0)
        self.flagz(r)
        return r & 255
    def cond(self, cc):
        return {'z': self.f['z'], 'nz': not self.f['z'], 'c': self.f['c'], 'nc': not self.f['c']}[cc]
    def run(self, maxsteps=40000):
        ins = self.ins
        while True:
            self.steps += 1
            if self.steps > maxsteps: raise Halt('steps')
            if self.pc >= len(ins):
                self.ev('FALLOFF', True); return
            c = ins[self.pc]; self.pc += 1
            m = re.match(r'^(\w+)\s*(.*)$', c)
            op = m.group(1).lower(); ops = [x.strip() for x in m.group(2).split(',')] if m.group(2) else []
            r = self.r
            if op in ('ld', 'ldh'):
                d, s = ops[0].lower(), ops[1]
                sl = s.lower()
                if d == 'hl':
                    n = self.num(s) & 0xFFFF; r['h'], r['l'] = n >> 8, n & 255
                elif d in r:
                    if op == 'ldh': r[d] = self.rd(self.addr(s[1:-1]))
                    elif sl in r or not s.startswith('['): r[d] = self.val(s)
                    else:
                        r[d] = self.val(s)
                elif d.startswith('['):
                    inner = d[1:-1].lower()
                    v = self.val(s)
                    if op == 'ldh': self.wr(self.addr(d[1:-1]), v)
                    elif inner == 'hl': self.wr(self.hl(), v)
                    else: self.wr(self.addr(d[1:-1]), v)
                else: raise Halt('ld ' + c)
            elif op in ('inc', 'dec'):
                d = ops[0].lower()
                if d in ('hl', 'bc', 'de'):
                    hi, lo = d[0], d[1]
                    v16 = ((r[hi] << 8) | r[lo]) + (1 if op == 'inc' else -1)
                    r[hi], r[lo] = (v16 >> 8) & 255, v16 & 255
                    continue
                v = self.val(d) if d != '[hl]' else self.rd(self.hl())
                nv = (v + (1 if op == 'inc' else -1)) & 255
                self.f['z'] = int(nv == 0); self.f['n'] = int(op == 'dec')
                self.f['h'] = int(((v & 15) == 0) if op == 'dec' else ((v & 15) == 15))
                if d == '[hl]': self.wr(self.hl(), nv)
                else: r[d] = nv
            elif op in ('add', 'adc', 'sub', 'sbc', 'and', 'or', 'xor', 'cp'):
                if len(ops) == 2: ops = ops[1:]
                if op == 'add' and len(m.group(2).split(',')) == 2 and m.group(2).split(',')[0].strip().lower() == 'hl': raise Halt(c)
                res = self.alu(op, r['a'], self.val(ops[0]))
                if op != 'cp': r['a'] = res
            elif op == 'bit':
                n = int(ops[0]); v = self.val(ops[1]); self.f.update(z=int(not (v >> n) & 1), n=0, h=1)
            elif op in ('set', 'res'):
                n = int(ops[0]); d = ops[1].lower()
                if d == '[hl]': raise Halt(c)
                r[d] = (r[d] | (1 << n)) & 255 if op == 'set' else r[d] & ~(1 << n) & 255
            elif op in ('sla', 'sra', 'srl', 'swap', 'rlc', 'rrc', 'rl', 'rr'):
                d = ops[0].lower()
                if d == '[hl]': raise Halt(c)
                v = r[d]; cin = self.f['c']
                if op == 'sla': nv, co = (v << 1) & 255, v >> 7
                elif op == 'sra': nv, co = (v >> 1) | (v & 128), v & 1
                elif op == 'srl': nv, co = v >> 1, v & 1
                elif op == 'swap': nv, co = ((v << 4) | (v >> 4)) & 255, 0
                elif op == 'rlc': nv, co = ((v << 1) | (v >> 7)) & 255, v >> 7
                elif op == 'rrc': nv, co = ((v >> 1) | (v << 7)) & 255, v & 1
                elif op == 'rl': nv, co = ((v << 1) | cin) & 255, v >> 7
                else: nv, co = (v >> 1) | (cin << 7), v & 1
                r[d] = nv; self.f.update(z=int(nv == 0), n=0, h=0, c=co)
            elif op in ('rla', 'rra', 'rlca', 'rrca'):
                v = r['a']; cin = self.f['c']
                if op == 'rla': nv, co = ((v << 1) | cin) & 255, v >> 7
                elif op == 'rra': nv, co = (v >> 1) | (cin << 7), v & 1
                elif op == 'rlca': nv, co = ((v << 1) | (v >> 7)) & 255, v >> 7
                else: nv, co = ((v >> 1) | (v << 7)) & 255, v & 1
                r['a'] = nv; self.f.update(z=0, n=0, h=0, c=co)
            elif op == 'cpl': r['a'] ^= 255; self.f.update(n=1, h=1)
            elif op == 'scf': self.f.update(n=0, h=0, c=1)
            elif op == 'ccf': self.f.update(n=0, h=0, c=1 - self.f['c'])
            elif op == 'jr' or op == 'jp':
                if len(ops) == 2: cc, t = ops[0].lower(), ops[1]
                else: cc, t = None, ops[0]
                if cc is None or self.cond(cc):
                    if t.startswith(':'):
                        # anonymous: find nearest
                        k = len(t) - 1
                        if t[1] == '+':
                            cands = [i for i in self.anon if i >= self.pc]; dest = cands[k - 1] if len(cands) >= k else None
                            if dest is None: raise Halt('anon')
                        else:
                            cands = [i for i in self.anon if i < self.pc]; dest = cands[-k] if len(cands) >= k else None
                            if dest is None: raise Halt('anon')
                        self.pc = dest
                    elif t in self.labels and not t.startswith('nes_') or t == self.head_name:
                        self.pc = self.labels[t]
                    elif t.startswith('nes_') and t in self.labels and t in self.local_nes:
                        self.pc = self.labels[t]
                    else:
                        self.ev('EXIT', True, t); return
            elif op == 'call':
                if ops[0] == 'nes_poll_nmi_hl':
                    self.ev('POLL', False)
                    rv = self.script[self.sc % len(self.script)]; self.sc += 1
                    r['a'] = rv[0]
                    self.f['z'] = 0
                    # helper preserves b,c,d,e; clobbers h,l? treat as preserved; consumes pending
                else:
                    self.ev('CALL', True, ops[0]); return
            elif op in ('ret', 'reti', 'halt', 'stop', 'push', 'pop', 'di', 'ei'):
                raise Halt(c)
            else:
                raise Halt('op ' + c)
            if op == 'ldh' and len(ops) == 2 and ops[1].lower().startswith('[nes_host_vblank_pending]'):
                v = self.script[(self.sc) % len(self.script)][1]; self.sc += 1
                r['a'] = v

def region(lines, head):
    """lines from head label through the end of its section"""
    i = next(k for k, l in enumerate(lines) if re.match(r'^%s:' % re.escape(head), l))
    j = i
    while j < len(lines) and not lines[j].startswith('SECTION') and j - i < 4000: j += 1
    return lines[i:j]

def main():
    asm = sys.argv[1]; trials = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    work = os.path.join(tempfile.mkdtemp(prefix='loopsim_'), 'work.asm'); shutil.copy(asm, work)
    env = dict(os.environ); env['NES2GBC_SELFLOOP_PROMOTE'] = '1'
    subprocess.run(['python3', os.path.join(HERE, 'selfloop_promote.py'), work], check=True, env=env, stderr=subprocess.DEVNULL)
    L0 = open(asm).read().split('\n'); L1 = open(work).read().split('\n')
    heads = [re.match(r'^(nes_\w+):', l).group(1) for l in L1 if re.match(r'^nes_\w+:', l)]
    # transformed loops: heads whose section contains selfloop_promote marker
    sect_has = {}
    cur = None; marks = {}
    for k, l in enumerate(L1):
        m = re.match(r'^(nes_\w+):', l)
        if m: cur = m.group(1)
        if 'selfloop_promote' in l and cur: marks.setdefault(cur, 0); marks[cur] += 1
    skipped = {}; tested = 0; bad = 0; halts = 0; polls = 0; nmis = 0; exits = 0; iters = 0
    # a section may host several loops; test each marked head
    for head in sorted(marks):
        # for the poll variant, the marker may live in the head's own block; take region from head
        ra, rb = region(L0, head), region(L1, head)
        ia = parse_region(ra); ib = parse_region(rb)
        # for equal simulation both must start at head
        local = set(k for k in ia[1] if k.startswith('nes_')) | set(k for k in ib[1] if k.startswith('nes_'))
        for trial in range(trials):
            rnd = random.Random(trial * 7919 + h16(head))
            init = dict(regs={k: rnd.randrange(256) for k in 'abcdehl'}, flags={k: rnd.randrange(2) for k in 'znhc'},
                        hram={}, ram={}, seed=trial)
            # bias h to WRAM-ish sometimes: leave random (loops set their own h)
            script = []
            for _ in range(64):
                pend = 1 if rnd.random() < (0.0 if trial % 3 == 0 else 0.25) else 0
                script.append((rnd.choice([0, 0, 0, 1]), pend))
            # script entries: (poll-helper return, pending-flag) consumed alternately by order -> keep one stream
            evs = []
            for ins, labels, anon in (ia, ib):
                s = Sim(ins, labels, anon, init, script, head)
                s.head_name = head; s.local_nes = local
                try: s.run()
                except Halt as e:
                    evs.append(('HALT', str(e)[:30])); continue
                evs.append(s.events)
            tested += 1
            for e in evs:
                if e and e[0] == 'HALT': halts += 1
            if evs[0] and evs[0][0] != 'HALT':
                polls += sum(1 for x in evs[0] if x[0] == 'POLL'); exits += sum(1 for x in evs[0] if x[0] in ('EXIT','CALL'))
            if any(e and e[0]=='HALT' for e in evs):
                skipped.setdefault(str([e[1] for e in evs if e and e[0]=='HALT']),0); skipped[str([e[1] for e in evs if e and e[0]=='HALT'])]+=1; continue
            if evs[0] != evs[1]:
                bad += 1
                if bad <= 3:
                    print('MISMATCH', head, 'trial', trial)
                    for e0, e1 in zip(evs[0], evs[1]):
                        if e0 != e1:
                            print(' orig', e0[0], e0[1]); print(' new ', e1[0], e1[1]); break
                    else:
                        print(' len', len(evs[0]), len(evs[1]))
    nh = sum(1 for k in marks)
    print('skipped(sim limit):',skipped)
    print('halts',halts,'poll events',polls,'exits',exits)
    print('%s: %d marked heads, %d trials, %d mismatches' % (os.path.basename(asm), nh, tested, bad))

if __name__ == '__main__':
    main()
