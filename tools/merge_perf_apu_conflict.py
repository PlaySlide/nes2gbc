from pathlib import Path

p = Path("src/lr35902.rs")
s = p.read_text()
start = s.index("<<<<<<< HEAD\n")
end_marker = ">>>>>>> origin/perf/branch-fusion\n"
end = s.index(end_marker, start) + len(end_marker)
block = s[start:end]
assert "0x4000..=0x4013 | 0x4015 | 0x4017" in block
assert "_ => {}" in block

replacement = '''                    0x4000..=0x4013 | 0x4015 | 0x4017 => {
                        if src == Register::A {
                            if !a_live {
                                writeln!(out, "    ldh a, [nes_a]").unwrap();
                            }
                        } else {
                            writeln!(out, "    ldh a, [{}]", state_label(src)).unwrap();
                        }
                        writeln!(out, "    ld e, a").unwrap();
                        writeln!(out, "    ld l, ${:02X}", addr as u8).unwrap();
                        writeln!(out, "    call nes_apu_write").unwrap();
                        a_live = false;
                    }
                    _ => {}
'''

p.write_text(s[:start] + replacement + s[end:])
