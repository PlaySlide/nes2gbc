#!/usr/bin/env python3
"""Promote loop-carried registers in poll-bearing single-block self-loops.

Shape (after outline_nmi_polls.py):
    nes_XXXX:  poll(jr nz,.npoll_slow_N)
    .npoll_back_N:  <prefix: loads of HRAM shadows / reg moves / imm loads>
                    <mid> <R: stores of shadows> [<T: ld a,r / and a,a>]
                    jr cc, nes_XXXX            ; back-edge re-runs head poll
The back-edge today re-executes the prefix and the shadow stores every
iteration. We (1) skip the prefix instructions that provably recompute values
already sitting in registers (or whose result is overwritten before use), and
(2) sink the shadow stores R to the loop exits, replaying R on the poll slow
path first so every observation point (NMI entry) sees bit-exact HRAM state.

Preconditions (all checked, otherwise the loop is left untouched):
 - straight-line body, only ld/ldh/inc/dec/ALU/bit/CB ops, WRAM [hl] accesses
   only with a provably constant H in $C0-$DF, no flag reads before a write
 - shadows touched: nes_a/x/y/sp/z_shadow/n_shadow/c_shadow only (none is
   referenced by anything reachable from nes_gbc_vblank_isr: tools/isrreach.py)
 - sunk variables are not read inside the loop; R only defines A itself,
   writes no other register, reads no flags; T (after R) reads no flags and
   defines A before using it, and is replayed on exit so A/flags match.
 - every register the skipped prefix would have rewritten either already holds
   that value at the back-edge (structural value equality) or is overwritten
   before being read; A is always treated as clobbered (poll check).
NES2GBC_SELFLOOP_PROMOTE=0 disables.
"""
import os, re, sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from gbregs import code, split, effect

SHADOW = {"nes_a", "nes_x", "nes_y", "nes_sp", "nes_z_shadow", "nes_n_shadow", "nes_c_shadow"}
R8 = {"a", "b", "c", "d", "e", "h", "l"}
FLAGS = {"zf", "nf", "hf", "cf"}
CC_FLAG = {"z": "zf", "nz": "zf", "c": "cf", "nc": "cf"}


class Abort(Exception):
    pass


def num(t):
    t = t.strip()
    if t.startswith("$"):
        return int(t[1:], 16)
    if re.fullmatch(r"\d+", t):
        return int(t)
    return None


class St:
    def __init__(self):
        self.r = {x: ("in", x) for x in R8}
        self.m = {}
        self.fresh = 0

    def copy(self):
        s = St(); s.r = dict(self.r); s.m = dict(self.m); s.fresh = self.fresh
        return s

    def unk(self):
        self.fresh += 1
        return ("u", self.fresh)

    def val(self, o):
        if o in R8:
            return self.r[o]
        n = num(o)
        return ("k", n) if n is not None else ("sym", o)

    def memv(self, v):
        return self.m.get(v, ("mem0", v))


def ramaddr_ok(st, o):
    """[hl] must have constant H in $C0-$DF; literal [$cxxx] ok."""
    if o == "[hl]":
        h = st.r["h"]
        if h[0] == "k" and 0xC0 <= h[1] <= 0xDF:
            return True
        raise Abort("hl")
    n = num(o[1:-1])
    if n is not None and 0xC000 <= n <= 0xDFFF:
        return True
    raise Abort("mem " + o)


def step(st, c):
    """Symbolically execute one instruction; return True if it stores RAM."""
    op, a = split(c)
    if op in ("ld", "ldh") and len(a) == 2:
        d, s = a
        if d == "hl" and not s.startswith("sp"):
            n = num(s)
            if n is not None:
                st.r["h"], st.r["l"] = ("k", n >> 8), ("k", n & 255)
            else:
                st.r["h"], st.r["l"] = ("hi", s), ("lo", s)
            return
        if d in ("bc", "de", "sp") or s == "sp" or s.startswith("sp"):
            raise Abort(c)
        if op == "ldh":
            if d == "a" and s.startswith("[") and s[1:-1] in SHADOW:
                st.r["a"] = st.memv(s[1:-1]); return
            if s == "a" and d.startswith("[") and d[1:-1] in SHADOW:
                st.m[d[1:-1]] = st.r["a"]; return
            raise Abort(c)
        if d in R8:
            if s in R8 or not s.startswith("["):
                st.r[d] = st.val(s); return
            ramaddr_ok(st, s); st.r[d] = st.unk(); return
        if d.startswith("["):
            if d[1:-1] in ("hli", "hld", "hl+", "hl-", "bc", "de", "c"):
                raise Abort(c)
            ramaddr_ok(st, d)
            if s not in R8 and s.startswith("["):
                raise Abort(c)
            return
        raise Abort(c)
    if op in ("inc", "dec") and len(a) == 1:
        if a[0] in R8:
            st.r[a[0]] = (op, st.r[a[0]]); return
        raise Abort(c)
    if op in ("add", "sub", "and", "or", "xor", "cp", "adc", "sbc"):
        e = effect(c)
        if e is None:
            raise Abort(c)
        s = a[-1]
        if s.startswith("["):
            ramaddr_ok(st, s); sv = st.unk()
        else:
            sv = st.val(s)
        if op in ("adc", "sbc"):
            st.r["a"] = st.unk(); return
        if op == "cp":
            return
        if s == "a" and op in ("xor", "sub"):
            st.r["a"] = ("k", 0); return
        st.r["a"] = (op, st.r["a"], sv); return
    if op == "bit" and len(a) == 2 and a[1] in R8:
        return
    if op in ("sla", "sra", "srl", "swap", "rlc", "rrc") and len(a) == 1 and a[0] in R8:
        st.r[a[0]] = (op, st.r[a[0]]); return
    if op in ("res", "set") and len(a) == 2 and a[1] in R8:
        st.r[a[1]] = (op, a[0], st.r[a[1]]); return
    if op in ("rl", "rr", "rla", "rra", "daa", "rlca", "rrca", "cpl", "scf", "ccf"):
        e = effect(c)
        if e is None:
            raise Abort(c)
        for w in e[1]:
            if w in R8:
                st.r[w] = st.unk()
        return
    raise Abort(c)


def first_ref(codes, reg):
    """'w' if reg is written (without read) before any read, 'r' if read first, None if untouched."""
    for c in codes:
        e = effect(c)
        if e is None:
            raise Abort(c)
        if reg in e[0]:
            return "r"
        if reg in e[1]:
            return "w"
    return None


def analyse(body, cc, poll=True):
    """body: list of instruction code strings (original order). Returns (m, rstart, tstart) or None."""
    # effects sanity + flag read-before-write
    flag_written = set()
    for c in body:
        e = effect(c)
        if e is None:
            raise Abort(c)
        if poll and (e[0] & FLAGS) - flag_written:
            raise Abort("flag read before write: " + c)
        flag_written |= e[1] & FLAGS
    n = len(body)
    # split tail: T = trailing region after last shadow store; R = stores run before it
    # find last 'ldh [V],a' index
    last = max((i for i, c in enumerate(body) if re.match(r"ldh \[(\w+)\], a$", c) and re.match(r"ldh \[(\w+)\]", c).group(1) in SHADOW), default=None)
    cands = []
    if last is not None:
        t0 = last + 1
        T = body[t0:]
        # R: extend start backwards over ld a,r / ldh [V],a (self-contained for A)
        s = last
        while s - 1 >= 0 and (re.match(r"ld a, [bcdehl]$", body[s - 1]) or
                              (re.match(r"ldh \[(\w+)\], a$", body[s - 1]) and re.match(r"ldh \[(\w+)\]", body[s - 1]).group(1) in SHADOW)):
            s -= 1
        # R must start with an A definition
        while s <= last and not re.match(r"ld a, [bcdehl]$", body[s]):
            s += 1
        if s <= last:
            R = body[s:t0]
            # R entries only ld a,r or shadow stores; first is ld a,r by construction
            # T validity: no flag reads, A defined first (or T empty), writes only a + flags
            ok = True
            for c in T:
                e = effect(c)
                if e is None or (e[0] & FLAGS) or (e[1] & R8) - {"a"}:
                    ok = False
            if T and first_ref(T, "a") != "w":
                ok = False
            if ok:
                cands.append((s, t0))
    cands.append((None, None))
    for (s, t0) in cands:
        # prefix candidates: leading ld/ldh loads
        end = s if s is not None else n
        pre = 0
        while pre < end and re.match(r"(ldh a, \[(\w+)\]|ld [abcdehl], [abcdehl]|ld [abcdehl], [^\[\],]+|ld hl, [^\[\],]+)$", body[pre]) and \
                (not body[pre].startswith("ldh") or re.match(r"ldh a, \[(\w+)\]", body[pre]).group(1) in SHADOW):
            if body[pre].startswith("ld ") and re.match(r"ld [abcdehl], \[", body[pre]):
                break
            pre += 1
        # back-edge state: execute whole body symbolically
        sb = St()
        for c in body:
            step(sb, c)
        sunk = set()
        if s is not None:
            for c in body[s:t0]:
                mm = re.match(r"ldh \[(\w+)\], a$", c)
                if mm:
                    sunk.add(mm.group(1))
        # sunk vars must not be read in the loop body outside prefix-skipped region
        for m in range(pre, -1, -1):
            rest = body[m:(s if s is not None else n)] + (body[t0:] if s is not None else [])
            # sunk var read in rest?
            bad = False
            for c in rest:
                mm = re.match(r"ldh a, \[(\w+)\]", c)
                if mm and mm.group(1) in sunk:
                    bad = True
            if bad:
                continue
            # state after executing prefix[0:m] in original semantics from back-edge
            sm = sb.copy()
            written = set()
            for c in body[:m]:
                step(sm, c)
                e = effect(c)
                written |= e[1] & R8
            div = {"a"} if poll else set()
            for r in written:
                if sm.r[r] != sb.r[r]:
                    div.add(r)
            okd = True
            for r in div:
                try:
                    fr = first_ref(rest + ([] if s is None else []), r)
                except Abort:
                    okd = False; break
                # R is also replayed at flush time and reads regs: its reads must see defined values
                if fr == "r":
                    okd = False; break
                if fr is None:
                    # never touched in loop: for a, fine only if exit does not need it => not allowed unless R/T define it
                    okd = False; break
            if not okd:
                continue
            # flags needed by jr cc must be written in rest (executed part)
            need = CC_FLAG[cc]
            wr = set()
            for c in rest:
                wr |= effect(c)[1]
            if poll and need not in wr:
                continue
            if s is None and m == 0:
                continue
            if s is None and m == 0:
                continue
            return m, s, t0
    return None


def main():
    p = Path(sys.argv[1])
    if os.environ.get("NES2GBC_SELFLOOP_PROMOTE", "1") == "0":
        return 0
    L = p.read_text().split("\n")
    out = []
    i = 0
    done = tried = 0
    # index of stub labels
    stub_at = {}
    for j, l in enumerate(L):
        mm = re.match(r"^\.npoll_slow_(\d+):", l)
        if mm:
            stub_at[mm.group(1)] = j
    inserts = {}  # line index -> list of lines to insert before it
    repl = {}     # (start,end) -> new lines
    j = 0
    while j < len(L):
        mm = re.match(r"^\.npoll_back_(\d+):", L[j])
        if not mm or mm.group(1) not in stub_at:
            j += 1; continue
        N = mm.group(1)
        # head: look back for nes_ label and poll
        k = j - 1
        pl = []
        while k >= 0 and (L[k].strip().startswith(";") or code(L[k]) == ""):
            k -= 1
        # expect jr nz,.npoll_slow_N
        if not re.match(r"jr nz, \.npoll_slow_%s$" % N, code(L[k])):
            j += 1; continue
        # find head label upward (skip poll/comments/IF DEF trace)
        h = k
        head = None
        while h >= 0 and k - h < 20:
            mh = re.match(r"^(nes_[A-Za-z0-9_]+):\s*(;.*)?$", L[h])
            if mh:
                head = mh.group(1); break
            h -= 1
        if head is None or head == "nes_poll_nmi_hl":
            j += 1; continue
        # body
        b = j + 1
        body_idx = []
        jr_i = None
        cc = None
        ok = True
        q = b
        while q < len(L):
            c = code(L[q])
            if c == "":
                q += 1; continue
            if re.match(r"j[rp] (z|nz|c|nc), %s$" % re.escape(head), c):
                jr_i = q; cc = c.split()[1].rstrip(","); break
            if re.match(r"^[.\w]+:{1,2}$", c) or c == ":" or c.split()[0].lower() in ("jr", "jp", "call", "ret", "reti", "rst", "halt", "stop", "push", "pop", "di", "ei") \
                    or c.upper().startswith(("IF", "ENDC", "PROFILE", "SECTION", "MACRO", "REPT", "ENDR")) or c.split()[0].upper() in ("IF", "ENDC", "ELSE"):
                ok = False; break
            body_idx.append(q)
            if len(body_idx) > 36:
                ok = False; break
            q += 1
        if not ok or jr_i is None or not L[jr_i].lstrip().startswith("jr "):
            j += 1; continue
        tried += 1
        body = [code(L[x]) for x in body_idx]
        try:
            res = analyse(body, cc)
        except Abort:
            res = None
        if res is None:
            j += 1; continue
        m, s, t0 = res
        sp = stub_at[N]
        # distance sanity for jr to stub
        dist = sum(1 for x in range(jr_i, sp) if code(L[x]) != "") * 3
        if dist > 100 or any(L[x].startswith("SECTION") for x in range(jr_i, sp)):
            j += 1; continue
        R = body[s:t0] if s is not None else []
        T = body[t0:] if s is not None else []
        mid_end = s if s is not None else len(body)
        new = []
        new += [L[x] for x in range(j, j + 1)]            # .npoll_back label
        new += ["    " + c for c in body[:m]]
        new += ["    jr .lp_body_%s ; selfloop_promote: skip re-poll on first entry" % N,
                ".lp_poll_%s:" % N,
                "    ldh a, [nes_host_vblank_pending]",
                "    and a",
                "    jr nz, .lp_slow_%s ; selfloop_promote" % N,
                ".lp_body_%s:" % N]
        new += ["    " + c for c in body[m:mid_end]]
        new += ["    " + c for c in T]
        new += ["    jr %s, .lp_poll_%s ; selfloop_promote: back-edge keeps poll, skips reload" % (cc, N)]
        new += ["    " + c for c in R] + ["    " + c for c in T]
        repl[(j, jr_i)] = new
        inserts[sp] = [".lp_slow_%s:" % N] + ["    " + c for c in R] + ["    jp .npoll_slow_%s ; flush sunk shadow stores, then original poll" % N]
        done += 1
        j = jr_i + 1

    # ---- poll-free self-loops (counted loops whose head poll was elided) ----
    j = 0
    while j < len(L):
        mh = re.match(r"^(nes_[A-Za-z0-9_]+):\s*(;.*)?$", L[j])
        if not mh:
            j += 1; continue
        head = mh.group(1)
        q = j + 1
        # skip trace block / comments
        in_if = False
        while q < len(L):
            c = code(L[q])
            if c == "":
                q += 1; continue
            if c.startswith("IF DEF(NES2GBC_PROFILE_TRACE)"):
                in_if = True; q += 1; continue
            if in_if:
                if c == "ENDC":
                    in_if = False
                q += 1; continue
            break
        body_start = q
        body_idx = []; jr_i = None; cc = None; ok = True
        while q < len(L):
            c = code(L[q])
            if c == "":
                q += 1; continue
            if re.match(r"jr (z|nz|c|nc), %s$" % re.escape(head), c):
                jr_i = q; cc = c.split()[1].rstrip(","); break
            if re.match(r"^[.\w]+:{1,2}$", c) or c == ":" or c.split()[0].lower() in ("jr", "jp", "call", "ret", "reti", "rst", "halt", "stop", "push", "pop", "di", "ei") \
                    or c.split()[0].upper() in ("IF", "ENDC", "ELSE", "SECTION", "MACRO", "REPT", "ENDR") or c.upper().startswith("PROFILE"):
                ok = False; break
            body_idx.append(q); q += 1
            if len(body_idx) > 36:
                ok = False; break
        if not ok or jr_i is None or any((j, jr_i) == k for k in repl):
            j += 1; continue
        if any(a <= jr_i <= b_ or a <= j <= b_ for (a, b_) in repl):
            j += 1; continue
        tried += 1
        body = [code(L[x]) for x in body_idx]
        try:
            res = analyse(body, cc, poll=False)
        except Abort:
            res = None
        if res is None:
            j += 1; continue
        m, s_, t0 = res
        R = body[s_:t0] if s_ is not None else []
        T = body[t0:] if s_ is not None else []
        mid_end = s_ if s_ is not None else len(body)
        new = ["    " + c for c in body[:m]]
        new += [".lp_body_%s:" % head]
        new += ["    " + c for c in body[m:mid_end]] + ["    " + c for c in T]
        new += ["    jr %s, .lp_body_%s ; selfloop_promote (poll-free loop)" % (cc, head)]
        new += ["    " + c for c in R] + ["    " + c for c in T]
        repl[(body_start, jr_i)] = new
        done += 1
        j = jr_i + 1
    res_lines = []
    j = 0
    starts = {a: (a, b) for (a, b) in repl}
    while j < len(L):
        if j in inserts:
            res_lines += inserts[j]
        if j in starts:
            a, b = starts[j]
            res_lines += repl[(a, b)]
            j = b + 1
            continue
        res_lines.append(L[j]); j += 1
    p.write_text("\n".join(res_lines))
    print("selfloop_promote: %d/%d loops transformed" % (done, tried), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
