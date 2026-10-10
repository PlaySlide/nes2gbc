#!/usr/bin/env python3
"""Branch on the host Z flag instead of reloading the just-stored Z shadow.

Shape (straight line, nothing in between but comments and flag-preserving
loads/stores that leave A alone):

    <op that sets host Z from A: and/or/xor/sub/sbc/add/adc/inc a/dec a>
    ldh [nes_z_shadow], a
    ...flag-preserving...
    ldh a, [nes_z_shadow]      (or the "reload ... already in A removed" note)
    and a
    jr/jp z|nz, ...

The reload + AND are dropped.  The shadow store stays (other paths read it).
Only A's value after the branch differs, so the rewrite requires A to be
rewritten (or a block/control boundary reached) before any read of A on the
fall-through path.  Disable with NES2GBC_HOST_Z_BRANCH=0.
"""
import os, re, sys
if os.environ.get("NES2GBC_HOST_Z_BRANCH", "1") == "0":
    sys.exit(0)
path = sys.argv[1]
L = open(path).read().split("\n")
def ins(l):
    s = l.split(";", 1)[0].strip()
    return s
ZSET = re.compile(r"^(and|or|xor|sub|sbc|add|adc) (a, )?([abcdehl]|\[hl\]|\$[0-9A-Fa-f]+|[0-9]+)$|^(inc|dec) a$")
def flag_safe_noa(s):
    # instruction that preserves flags and does not write A
    if s == "" : return True
    if s.startswith("ld ") or s.startswith("ldh "):
        dst = s.split(None, 1)[1].split(",")[0].strip()
        if dst == "a": return False
        if "sp" in s: return False
        return True
    if s.startswith("push "): return True
    return False
def flag_safe(s):
    # preserves host flags (may write A)
    if s == "": return True
    if s.startswith(("ld ", "ldh ")) and "sp" not in s: return True
    if s.startswith("push "): return True
    return False
def writes_a(s):
    if s.startswith(("ld a,", "ldh a,", "ld a ,")): return "[" not in s.split(",",1)[1] or True
    return s in ("xor a", "pop af") or s.startswith(("call ", "jp ", "ret", "reti", "rst "))
def reads_a(s):
    if not s: return False
    if s.startswith(("ld a,", "ldh a,")): return False
    if s.startswith(("ld ", "ldh ")): return s.rstrip().endswith(", a") or s.rstrip().endswith(",a")
    return True  # anything else: conservatively a read (and/or/cp/inc a/push af/jr...)
def a_dead_after(L, t):
    while t < len(L):
        raw = L[t]; s = ins(raw)
        if "already in A" in raw: return False
        if re.match(r"^\S+:", raw): return True
        if s == ":" or s == "": t += 1; continue
        if s.startswith("ret") or (s.startswith("jp ") and "," not in s): return True
        if writes_a(s): return True
        if reads_a(s): return False
        t += 1
    return True
n = 0
i = 0
out = L[:]
kill = set()
for k in range(len(L) if os.environ.get("NES2GBC_HOST_Z_SIMPLE", "1") != "0" else 0):
    if ins(L[k]) != "and a":
        continue
    # next instruction must be a Z branch
    m = k + 1
    while m < len(L) and ins(L[m]) == "": m += 1
    if m >= len(L) or not re.match(r"^(jr|jp) n?z, ", ins(L[m])): continue
    # optional reload just before
    j = k - 1
    while j >= 0 and ins(L[j]) == "" and "already in A removed: ldh a, [nes_z_shadow]" not in L[j]: j -= 1
    reload = None
    if j >= 0 and ins(L[j]) == "ldh a, [nes_z_shadow]":
        reload = j
    elif j >= 0 and "already in A removed: ldh a, [nes_z_shadow]" in L[j]:
        reload = -1
    else:
        continue
    # walk back to the z store, then to the flag-setting op
    p = (j - 1)
    ok = False
    while p >= 0:
        s = ins(L[p])
        if s == "ldh [nes_z_shadow], a":
            q = p - 1
            while q >= 0 and (ins(L[q]) == "" or (flag_safe_noa(ins(L[q])) and not ins(L[q]).startswith("ldh [nes_z_shadow]"))):
                if ins(L[q]) == "" and L[q].strip().endswith(":") : break
                q -= 1
            ok = q >= 0 and bool(ZSET.match(ins(L[q])))
            break
        if s.endswith(":") or L[p].strip().endswith(":"): break
        if not flag_safe(s): break
        p -= 1
    if not ok:
        continue
    # A after the branch must not be read before being redefined, on the
    # not-taken path and (for "jr cc, :+") after the anonymous target label.
    safe = a_dead_after(L, m + 1)
    if safe and ins(L[m]).endswith(":+"):
        t = m + 1
        while t < len(L) and L[t].strip() != ":":
            if re.match(r"^\S+:", L[t]): t = len(L); break
            t += 1
        safe = t < len(L) and a_dead_after(L, t + 1)
    if not safe: continue
    if n >= int(os.environ.get("NES2GBC_HOST_Z_LIMIT", "999999")): continue
    if reload is not None and reload >= 0: kill.add(reload)
    kill.add(k); n += 1
for x in sorted(kill, reverse=True):
    out[x] = "    ; host-Z branch: " + L[x].strip()

# Compare + carry capture + Z branch: branch first on the live host Z, then
# capture the 6502 carry on each path (host C survives the JR).
L = out
idx = [k for k in range(len(L)) if ins(L[k]) != "" or L[k].strip() == ":"]
seq = [ins(L[k]) if ins(L[k]) else ":" for k in idx]
CAPS = (["ld a, $00", "jr c, :+", "inc a", ":"], ["sbc a", "inc a", ":"], ["sbc a", "inc a"])
n2 = 0
repl = {}
for u in range(1, len(seq) if os.environ.get("NES2GBC_HOST_Z_REORDER", "1") != "0" else 0):
    if seq[u] != "ldh [nes_z_shadow], a" or not ZSET.match(seq[u - 1]) or not seq[u-1].startswith(("sub", "sbc", "cp")):
        continue
    v = u + 1
    if v < len(seq) and seq[v] == "ldh [nes_n_shadow], a": v += 1
    cap = None
    for c in CAPS:
        if seq[v:v + len(c)] == c: cap = c; break
    if not cap: continue
    w = v + len(cap)
    tail = seq[w:w + 6]
    if len(tail) < 6 or tail[0] != "ldh [nes_c_shadow], a" or tail[1] != "ldh a, [nes_z_shadow]" or tail[2] != "and a":
        continue
    mm = re.match(r"^jr (n?z), :\+$", tail[3])
    if not mm or not re.match(r"^jp nes_\w+$", tail[4]) or tail[5] != ":": continue
    if any(k in repl for k in idx[v:w + 6]): continue
    if cap[-1] == ":" and cap[1] != "jr c, :+":
        # stray anonymous label: no earlier JR may still target it
        b = idx[v] - 1; bad = False
        while b >= 0:
            r = L[b].strip()
            if r == ":" or re.match(r"^\S+:", L[b]): break
            if ":+" in ins(L[b]): bad = True; break
            b -= 1
        if bad: continue
    if not a_dead_after(L, idx[w + 5] + 1): continue
    first = idx[v]
    new = [f"    jr {mm.group(1)}, :+ ; host-Z branch before carry capture",
           "    sbc a", "    inc a", "    ldh [nes_c_shadow], a",
           "    " + tail[4], ":", "    sbc a", "    inc a", "    ldh [nes_c_shadow], a"]
    repl[first] = new
    for k in idx[v + 1:w + 6]: repl[k] = []
    # keep comment lines inside the region as-is (they are dropped harmlessly)
    for k in range(first + 1, idx[w + 5] + 1):
        if k not in repl and not L[k].strip().startswith(";") and L[k].strip(): repl[k] = []
    n2 += 1
if repl:
    nl = []
    for k, l in enumerate(L):
        if k in repl: nl.extend(repl[k])
        else: nl.append(l)
    out = nl
print(f"host-z-branch: {n2} compare+carry branch(es) reordered")
open(path, "w").write("\n".join(out))
print(f"host-z-branch: {n} branch(es) use the live host Z flag")
