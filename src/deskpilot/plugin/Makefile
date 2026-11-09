# Builds deskpilot.so against the installed Hyprland headers. Rebuild after every Hyprland upgrade: the plugin
# refuses to load into a Hyprland other than the one it was built for. `-z now` resolves every symbol at load, so a
# function this Hyprland does not export fails the load instead of crashing the compositor on first use.
CXXFLAGS ?= -O2
SOURCE_HASH ?= unknown
OUT ?= deskpilot.so

$(OUT): deskpilot.cpp
	$(CXX) -shared -fPIC -std=c++26 $(CXXFLAGS) -DDESKPILOT_SOURCE='"$(SOURCE_HASH)"' $(shell pkg-config --cflags pixman-1 libdrm hyprland xcb xkbcommon) -Wl,-z,now -o $@ $< $(shell pkg-config --libs xcb xkbcommon)

clean:
	command rm -f -- $(OUT)

.PHONY: clean
