#!/usr/bin/env python3
"""Use native LR35902 CALL/RET for a very strict subset of 6502 leaf JSRs.

Unlike the old broad native-call experiment, this keeps the 6502-visible stack
memory writes byte-for-byte: the JSR return high/low bytes are still written to
page $01 at the same addresses. We merely leave virtual nes_sp unchanged while
executing a duplicated leaf body natively, then continue at the exact static
continuation. This is safe only when the leaf cannot observe the temporary SP
state or stack page and cannot take a translated NMI poll.

Eligibility is deliberately narrow:
  * one straight-line basic block ending in RTS;
  * caller, leaf, and continuation all in the same translated code bank;
  * no 6502 stack/SP instructions, nested JSR, BRK/RTI, or dynamic indexed modes;
  * no generated helper CALLs, generic [HL] memory, page-$01 accesses, nes_sp,
    NMI polling, or debug-PC instrumentation in the copied body.

The original translated leaf remains untouched for dynamic/other entries. Only
eligible direct static JSR sites call the native duplicate.
"""

from __future__ import annotations

import argparse
import collections
import re
from dataclasses import dataclass
from pathlib import Path

SECTION_BANK_RE = re.compile(r'^SECTION .*BANK\[(\d+)\]')
BLOCK_LABEL_RE = re.compile(r'^nes_([0-9A-Fa-f]{4}):$')
INSN_RE = re.compile(r'; \$([0-9A-Fa-f]{4}): \$([0-9A-Fa-f]{2}) ([A-Za-z0-9_]+) ([A-Za-z0-9_]+)')
TARGET_RE = re.compile(r'\bnes_([0-9A-Fa-f]{4})\b')

STACK_MNEMONICS = {'Pha', 'Php', 'Pla', 'Plp', 'Tsx', 'Txs'}
CONTROL_REJECT = {'Jsr', 'Brk', 'Rti', 'Jmp', 'Bcc', 'Bcs', 'Beq', 'Bmi', 'Bne', 'Bpl', 'Bvc', 'Bvs'}
DYNAMIC_MODES = {'IndexedIndirect', 'IndirectIndexed', 'AbsoluteX', 'AbsoluteY'}


def code(line: str) -> str:
    return line.split(';', 1)[0].strip()


@dataclass
class Block:
    addr: int
    bank: int
    label_i: int
    end_i: int
    insns: list[tuple[int, int, str, str]]


def parse_blocks(lines: list[str]) -> tuple[dict[int, Block], dict[int, int]]:
    labels: list[tuple[int, int, int]] = []
    label_bank: dict[int, int] = {}
    bank: int | None = None
    for i, line in enumerate(lines):
        sm = SECTION_BANK_RE.match(code(line))
        if sm:
            bank = int(sm.group(1))
        lm = BLOCK_LABEL_RE.match(code(line))
        if lm and bank is not None:
            addr = int(lm.group(1), 16)
            labels.append((i, addr, bank))
            label_bank[addr] = bank

    blocks: dict[int, Block] = {}
    for n, (label_i, addr, block_bank) in enumerate(labels):
        end_i = labels[n + 1][0] if n + 1 < len(labels) else len(lines)
        for j in range(label_i + 1, end_i):
            if code(lines[j]).startswith('SECTION '):
                end_i = j
                break
        insns: list[tuple[int, int, str, str]] = []
        for j in range(label_i + 1, end_i):
            m = INSN_RE.search(lines[j])
            if m:
                insns.append((j, int(m.group(1), 16), m.group(3), m.group(4)))
        blocks[addr] = Block(addr, block_bank, label_i, end_i, insns)
    return blocks, label_bank


def insn_end(block: Block, k: int) -> int:
    if k + 1 < len(block.insns):
        return block.insns[k + 1][0]
    return block.end_i


def strip_profile_trace(lines: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for line in lines:
        c = code(line)
        if c == 'IF DEF(NES2GBC_PROFILE_TRACE)':
            skip = True
            continue
        if skip:
            if c == 'ENDC':
                skip = False
            continue
        out.append(line)
    return out


def leaf_body(lines: list[str], block: Block) -> tuple[list[str], int] | None:
    if not block.insns or block.insns[-1][2] != 'Rts':
        return None

    for _line_i, _pc, mnemonic, mode in block.insns[:-1]:
        if mnemonic in STACK_MNEMONICS or mnemonic in CONTROL_REJECT:
            return None
        if mode in DYNAMIC_MODES:
            return None

    rts_comment_i = block.insns[-1][0]
    body = strip_profile_trace(lines[block.label_i + 1:rts_comment_i])
    joined = ''.join(body)

    forbidden = (
        'nes_host_vblank_pending', 'nes_poll_nmi_hl', 'nes_sp', 'nes_stack',
        'nes_debug_pc_', '$C1', 'call ', '[hl]', '[hli]', '[hld]', '[de]', '[bc]'
    )
    if any(token in joined for token in forbidden):
        return None

    # No external generated control labels in a supposedly straight-line copy.
    for line in body:
        c = code(line)
        if c.startswith('jp nes_') or c.startswith('jr nes_'):
            return None

    real = sum(
        1 for line in body
        if (c := code(line)) and not c.startswith(';') and not c.endswith(':')
        and not c.startswith('IF ') and c != 'ENDC'
    )
    return body, real


@dataclass
class CallSite:
    block_addr: int
    bank: int
    start_i: int
    end_i: int
    target: int
    continuation: int
    stacked_pc: int


def collect_calls(lines: list[str], blocks: dict[int, Block], label_bank: dict[int, int]) -> list[CallSite]:
    out: list[CallSite] = []
    labels = set(label_bank)
    for block in blocks.values():
        for k, (comment_i, pc, mnemonic, _mode) in enumerate(block.insns):
            if mnemonic != 'Jsr':
                continue
            end_i = insn_end(block, k)
            segment = lines[comment_i + 1:end_i]
            text = ''.join(segment)
            if 'inline static 6502 JSR return push' not in text:
                continue

            continuation = (pc + 3) & 0xFFFF
            if continuation not in labels or label_bank[continuation] != block.bank:
                continue

            targets: set[int] = set()
            for line in segment:
                for m in TARGET_RE.finditer(code(line)):
                    t = int(m.group(1), 16)
                    if t in blocks and t != continuation:
                        targets.add(t)
            if len(targets) != 1:
                continue
            target = next(iter(targets))
            if label_bank.get(target) != block.bank:
                continue

            # Only replace an ordinary direct same-bank transfer to the leaf.
            direct = f'jp nes_{target:04X}' in text or f'jr nes_{target:04X}' in text
            if not direct:
                continue

            out.append(CallSite(
                block.addr, block.bank, comment_i + 1, end_i, target,
                continuation, (continuation - 1) & 0xFFFF,
            ))
    return out


def replacement(site: CallSite, thunk: str) -> str:
    hi = (site.stacked_pc >> 8) & 0xFF
    lo = site.stacked_pc & 0xFF
    return ''.join([
        '    PROFILE_INC nes_profile_jsr_push\n',
        f'    ; strict native leaf JSR ${site.target:04X}: preserve stack bytes, keep SP canonical\n',
        '    ldh a, [nes_sp]\n',
        '    ld l, a\n',
        '    ld h, $C1\n',
        f'    ld [hl], ${hi:02X}\n',
        '    dec l\n',
        f'    ld [hl], ${lo:02X}\n',
        '    dec l\n',
        '    ld a, l\n',
        '    ; omit temporary nes_sp=SP-2: eligible leaf cannot observe it\n',
        f'    call {thunk}\n',
        '    PROFILE_INC nes_profile_rts_pop\n',
        f'    jp nes_{site.continuation:04X}\n',
    ])


def thunk_text(target: int, bank: int, body: list[str]) -> str:
    label = f'nes_native_leaf_{target:04X}_b{bank:02X}'
    out = [
        '\n',
        f'SECTION "Native leaf {target:04X} bank {bank}", ROMX, BANK[{bank}]\n',
        f'{label}:\n',
    ]
    out.extend(body)
    out.extend([
        '    ; original translated RTS is replaced only in this private duplicate\n',
        '    ret\n',
    ])
    return ''.join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('asm', type=Path)
    p.add_argument('--bank-budget', type=int, default=768,
                   help='pessimistic extra native-leaf bytes allowed per code bank')
    args = p.parse_args()

    lines = args.asm.read_text(encoding='utf-8').splitlines(keepends=True)
    blocks, label_bank = parse_blocks(lines)
    calls = collect_calls(lines, blocks, label_bank)

    safe: dict[tuple[int, int], tuple[list[str], int]] = {}
    for site in calls:
        key = (site.target, site.bank)
        if key in safe:
            continue
        result = leaf_body(lines, blocks[site.target])
        if result is not None:
            safe[key] = result

    grouped: dict[tuple[int, int], list[CallSite]] = collections.defaultdict(list)
    for site in calls:
        key = (site.target, site.bank)
        if key in safe:
            grouped[key].append(site)

    # Prefer leaves with more static call sites, then smaller duplicate bodies.
    ranked = sorted(
        grouped.items(),
        key=lambda item: (-len(item[1]), safe[item[0]][1], item[0]),
    )
    used: dict[int, int] = collections.defaultdict(int)
    selected: dict[tuple[int, int], list[CallSite]] = {}
    for key, sites in ranked:
        target, bank = key
        _body, real = safe[key]
        pessimistic = (real + 1) * 3
        if used[bank] + pessimistic > args.bank_budget:
            continue
        used[bank] += pessimistic
        selected[key] = sites

    edits: list[tuple[int, int, str]] = []
    thunks: list[str] = []
    for (target, bank), sites in selected.items():
        body, _real = safe[(target, bank)]
        label = f'nes_native_leaf_{target:04X}_b{bank:02X}'
        thunks.append(thunk_text(target, bank, body))
        for site in sites:
            edits.append((site.start_i, site.end_i, replacement(site, label)))

    for start, end, text in sorted(edits, reverse=True):
        lines[start:end] = [text]
    lines.extend(thunks)

    args.asm.write_text(''.join(lines), encoding='utf-8')
    print(
        f'native-leaf: converted {len(edits)} static JSR site(s) across '
        f'{len(selected)} strict leaf thunk(s); preserved JSR stack bytes, '
        f'kept virtual SP canonical; considered {len(calls)} same-bank static call(s)'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
