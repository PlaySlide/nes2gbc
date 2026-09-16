#!/usr/bin/env python3
from pathlib import Path

p = Path("tools/specialize_inline_dispatchers.py")
s = p.read_text()

old = '''INSN_RE = re.compile(r"^\\s*; \\$([0-9A-Fa-f]{4}): \\$([0-9A-Fa-f]{2}) (\\w+) (\\w+)")
NES_LABEL_RE = re.compile(r"^\\s*nes_([0-9A-Fa-f]{4}):\\s*$")
SECTION_RE = re.compile(r"^\\s*SECTION\\b")
'''
new = '''INSN_RE = re.compile(r"^\\s*; \\$([0-9A-Fa-f]{4}): \\$([0-9A-Fa-f]{2}) (\\w+) (\\w+)")
NES_LABEL_RE = re.compile(r"^\\s*nes_([0-9A-Fa-f]{4}):\\s*$")
TRACE_LABEL_RE = re.compile(r"^\\s*nes_[0-9A-Fa-f]{4}_trace:\\s*$")
SECTION_RE = re.compile(r"^\\s*SECTION\\b")
'''
assert old in s
s = s.replace(old, new, 1)

old = '''        if INSN_RE.match(lines[i]) or NES_LABEL_RE.match(lines[i]) or SECTION_RE.match(code(lines[i])):
            return i
'''
new = '''        if (
            INSN_RE.match(lines[i])
            or NES_LABEL_RE.match(lines[i])
            or TRACE_LABEL_RE.match(lines[i])
            or SECTION_RE.match(code(lines[i]))
        ):
            return i
'''
assert old in s
s = s.replace(old, new, 1)

p.write_text(s)
