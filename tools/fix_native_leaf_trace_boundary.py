#!/usr/bin/env python3
from pathlib import Path

p = Path("tools/native_leaf_calls.py")
s = p.read_text()

old = """SECTION_BANK_RE = re.compile(r'^SECTION .*BANK\\[(\\d+)\\]')
BLOCK_LABEL_RE = re.compile(r'^nes_([0-9A-Fa-f]{4}):$')
INSN_RE = re.compile(r'; \\$([0-9A-Fa-f]{4}): \\$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)')
"""
new = """SECTION_BANK_RE = re.compile(r'^SECTION .*BANK\\[(\\d+)\\]')
BLOCK_LABEL_RE = re.compile(r'^nes_([0-9A-Fa-f]{4}):$')
TRACE_LABEL_RE = re.compile(r'^nes_[0-9A-Fa-f]{4}_trace:$')
INSN_RE = re.compile(r'; \\$([0-9A-Fa-f]{4}): \\$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)')
"""
assert old in s
s = s.replace(old, new, 1)

old = """        for j in range(label_i + 1, end_i):
            if code(lines[j]).startswith('SECTION '):
                end_i = j
                break
"""
new = """        for j in range(label_i + 1, end_i):
            c = code(lines[j])
            if c.startswith('SECTION ') or TRACE_LABEL_RE.fullmatch(c):
                # A private superblock trace is a distinct generated block.
                # Never fold/copy it into a canonical native-leaf body.
                end_i = j
                break
"""
assert old in s
s = s.replace(old, new, 1)

# Belt-and-suspenders: a native leaf body must never contain a private trace
# label even if the parser changes later.
old = """    body = strip_profile_trace(lines[block.label_i + 1:rts_comment_i])
    joined = ''.join(body)

    forbidden = (
"""
new = """    body = strip_profile_trace(lines[block.label_i + 1:rts_comment_i])
    if any(TRACE_LABEL_RE.fullmatch(code(line)) for line in body):
        return None
    joined = ''.join(body)

    forbidden = (
"""
assert old in s
s = s.replace(old, new, 1)

p.write_text(s)
