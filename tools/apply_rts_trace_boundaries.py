#!/usr/bin/env python3
from pathlib import Path

for name in ("fast_leaf_rts_dispatch.py", "fast_subroutine_rts_dispatch.py"):
    p = Path("tools") / name
    s = p.read_text()

    old_re = 'BLOCK_LABEL_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")\n'
    new_re = (
        'BLOCK_LABEL_RE = re.compile(r"^nes_([0-9A-Fa-f]{4})(?:_trace)?:$")\n'
        'CANON_LABEL_RE = re.compile(r"^nes_([0-9A-Fa-f]{4}):$")\n'
    )
    assert old_re in s, name
    s = s.replace(old_re, new_re, 1)

    old_scan = '''    labels: list[tuple[int, int, int]] = []\n    label_bank: dict[int, int] = {}\n    bank: int | None = None\n\n    for i, line in enumerate(lines):\n        sm = SECTION_BANK_RE.match(code(line))\n        if sm:\n            bank = int(sm.group(1))\n        lm = BLOCK_LABEL_RE.match(code(line))\n        if lm and bank is not None:\n            addr = int(lm.group(1), 16)\n            labels.append((i, addr, bank))\n            label_bank[addr] = bank\n\n    blocks: dict[int, Block] = {}\n    for n, (label_i, addr, block_bank) in enumerate(labels):\n        end_i = labels[n + 1][0] if n + 1 < len(labels) else len(lines)\n        for j in range(label_i + 1, end_i):\n            if code(lines[j]).startswith("SECTION "):\n                end_i = j\n                break\n\n        insns: list[tuple[int, int, str, str]] = []\n        for j in range(label_i + 1, end_i):\n            m = INSN_RE.search(lines[j])\n            if m:\n                insns.append((j, int(m.group(1), 16), m.group(3), m.group(4)))\n        blocks[addr] = Block(addr, block_bank, label_i, end_i, insns)\n\n    return blocks, label_bank\n'''

    new_scan = '''    # Physical translated bodies may begin at either nes_XXXX: or\n    # nes_XXXX_trace:.  A later canonical nes_XXXX: adapter can share the same\n    # NES address but contains no source instruction comments.  Segment on every\n    # physical entry label, then retain the code-bearing segment for each address.\n    labels: list[tuple[int, int, int]] = []\n    label_bank: dict[int, int] = {}\n    bank: int | None = None\n\n    for i, line in enumerate(lines):\n        sm = SECTION_BANK_RE.match(code(line))\n        if sm:\n            bank = int(sm.group(1))\n        c = code(line)\n        lm = BLOCK_LABEL_RE.fullmatch(c)\n        if lm and bank is not None:\n            addr = int(lm.group(1), 16)\n            labels.append((i, addr, bank))\n        cm = CANON_LABEL_RE.fullmatch(c)\n        if cm and bank is not None:\n            label_bank[int(cm.group(1), 16)] = bank\n\n    blocks: dict[int, Block] = {}\n    for n, (label_i, addr, block_bank) in enumerate(labels):\n        end_i = labels[n + 1][0] if n + 1 < len(labels) else len(lines)\n        for j in range(label_i + 1, end_i):\n            if code(lines[j]).startswith("SECTION "):\n                end_i = j\n                break\n\n        insns: list[tuple[int, int, str, str]] = []\n        for j in range(label_i + 1, end_i):\n            m = INSN_RE.search(lines[j])\n            if m:\n                insns.append((j, int(m.group(1), 16), m.group(3), m.group(4)))\n        if insns:\n            # Exactly one physical segment should carry translated source for a\n            # given NES address.  Canonical adapters are intentionally ignored.\n            prev = blocks.get(addr)\n            assert prev is None, f"multiple code-bearing segments for NES ${addr:04X}"\n            blocks[addr] = Block(addr, block_bank, label_i, end_i, insns)\n\n    return blocks, label_bank\n'''
    assert old_scan in s, name
    s = s.replace(old_scan, new_scan, 1)
    p.write_text(s)

p = Path("tools/defer_subroutine_rts_increment.py")
s = p.read_text()
s = s.replace(
    'CANON_LABEL_RE = re.compile(r"^nes_[0-9A-Fa-f]{4}:$")\n',
    'BLOCK_ENTRY_RE = re.compile(r"^nes_[0-9A-Fa-f]{4}(?:_trace)?:$")\n',
    1,
)
s = s.replace('CANON_LABEL_RE.fullmatch(c)', 'BLOCK_ENTRY_RE.fullmatch(c)')
p.write_text(s)
