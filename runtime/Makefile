OUTPUT := build/runtime.gbc
OBJ := build/runtime.o

.PHONY: all clean
all: $(OUTPUT)

build:
	mkdir -p build

$(OBJ): main.asm hardware.inc io.asm cpu.asm ppu.asm video.asm input.asm generated.asm | build
	rgbasm -o "$@" main.asm

$(OUTPUT): $(OBJ)
	rgblink -o "$@" -n build/runtime.sym -m build/runtime.map "$<"
	rgbfix -v -p 0 -C -m 0x19 -r 0x00 -t NES2GBC "$@"

clean:
	rm -rf build
