#!/usr/bin/env python3
from pathlib import Path

for name in ("dead_terminal_zn.py", "lazy_overflow_updates.py"):
    p = Path("tools") / name
    s = p.read_text()
    marker = 'BLOCK_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")\n'
    assert marker in s, name
    s = s.replace(
        marker,
        marker + 'TRACE_LABEL_RE = re.compile(r"^nes_[0-9A-Fa-f]{4}_trace:$")\n',
        1,
    )
    old = '''        for j in range(label_i + 1, raw_end):\n            if code(lines[j]).startswith("SECTION "):\n                end_i = j\n                break\n'''
    new = '''        for j in range(label_i + 1, raw_end):\n            c = code(lines[j])\n            if c.startswith("SECTION ") or TRACE_LABEL_RE.fullmatch(c):\n                # Private trace labels are alternate CFG entries.  Do not let\n                # inter-block liveness reason through them as if execution\n                # could only arrive from the preceding canonical label.\n                end_i = j\n                break\n'''
    assert old in s, name
    s = s.replace(old, new, 1)
    p.write_text(s)
