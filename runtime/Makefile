OUTPUT := build/runtime.gbc
OBJ := build/runtime.o
TRACE ?= 0
PROFILE ?= 0
PROFILE_TRACE ?= 0

RGBASM_FLAGS :=
ifeq ($(TRACE),1)
RGBASM_FLAGS += -D NES2GBC_DEBUG_TRACE=1
endif
ifeq ($(PROFILE),1)
RGBASM_FLAGS += -D NES2GBC_PROFILE=1
endif
ifeq ($(PROFILE_TRACE),1)
RGBASM_FLAGS += -D NES2GBC_PROFILE=1 -D NES2GBC_PROFILE_TRACE=1
endif

.PHONY: all clean
all: $(OUTPUT)

build:
	mkdir -p build

$(OBJ): main.asm hardware.inc io.asm profile.asm cpu.asm ppu.asm video.asm input.asm generated.asm | build
	rgbasm $(RGBASM_FLAGS) -o "$@" main.asm

$(OUTPUT): $(OBJ)
	rgblink -o "$@" -n build/runtime.sym -m build/runtime.map "$<"
	rgbfix -v -p 0 -C -m 0x19 -r 0x00 -t NES2GBC "$@"

clean:
	rm -rf build
