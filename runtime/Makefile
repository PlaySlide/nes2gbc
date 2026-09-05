ROM := build/runtime.gbc
OBJ := build/runtime.o

.PHONY: all clean
all: $(ROM)

build:
	mkdir -p build

$(OBJ): main.asm | build
	rgbasm -o $@ $<

$(ROM): $(OBJ)
	rgblink -o $@ -n build/runtime.sym -m build/runtime.map $<
	rgbfix -v -p 0 -C -m 0x19 -r 0x00 -t NES2GBC $@

clean:
	rm -rf build
