# Builds deskpilot.so against the installed Hyprland headers. Rebuild after every Hyprland upgrade: the plugin
# refuses to load into a Hyprland other than the one it was built for. `-z now` resolves every symbol at load, so a
# function this Hyprland does not export fails the load instead of crashing the compositor on first use.
# `-fno-gnu-unique` keeps C++ inline statics out of STB_GNU_UNIQUE: with one such symbol glibc never unmaps the library,
# so unloading it leaves its code and globals behind, and loading the same build again reuses globals its last
# PLUGIN_EXIT already freed.
#
# The agent seat library (seat.cpp, see seat.hpp) stays loaded for the life of Hyprland once the plugin opens it, so an
# existing one is never rebuilt: writing over a mapped library would change code Hyprland is running. Its path names
# its source, so changed source builds a new one.
CXXFLAGS ?= -O2
SOURCE_HASH ?= unknown
OUT ?= deskpilot.so
SEAT ?= $(abspath deskpilot-seat.so)
FLAGS = -shared -fPIC -std=c++26 $(CXXFLAGS) $(shell pkg-config --cflags pixman-1 libdrm hyprland xcb xkbcommon cairo) -Wl,-z,now

$(OUT): deskpilot.cpp seat.hpp | $(SEAT)
	$(CXX) $(FLAGS) -fno-gnu-unique -DDESKPILOT_SOURCE='"$(SOURCE_HASH)"' -DDESKPILOT_SEAT='"$(SEAT)"' -o $@ $< $(shell pkg-config --libs xcb xkbcommon cairo)

$(SEAT):
	$(CXX) $(FLAGS) -o $@.tmp seat.cpp
	mv -f -- $@.tmp $@

clean:
	command rm -f -- $(OUT) $(SEAT)

.PHONY: clean
