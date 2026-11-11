// The agent seat: a wl_seat named "deskpilot" with a pointer and nothing else, which the plugin drives instead of the
// user's seat for a client that bound it (see "agent seat" in deskpilot.cpp).
//
// GTK 4 binds the seats it finds when it starts and never one announced later, so a seat that went with each plugin
// unload would be lost to every app already running. The seat lives in this library instead, which the plugin opens
// with RTLD_NODELETE: it stays for the life of the compositor, and every plugin build since opens the same instance,
// with the same global and the clients' resources. The library is named after its own source, so it changes only when
// this file or seat.cpp does; a changed one is a second library with a second seat, which only apps started since bind.

#pragma once

#include <hyprland/protocols/wayland.hpp>
#include <cstdint>
#include <hyprutils/memory/WeakPtr.hpp>

#include <vector>

// Objects other protocols make for a wl_pointer, which an agent pointer gets in place of Hyprland's own.
enum class eInert : uint8_t { SWIPE, PINCH, HOLD, RELATIVE, LOCKED, CONFINED };

struct SAgentSeat {
    // The agent pointers a client holds, in the order it asked for them.
    std::vector<Hyprutils::Memory::CWeakPointer<CWlPointer>> (*pointersOf)(wl_client* client);
    // Whether a wl_pointer resource is an agent pointer.
    bool (*isPointer)(wl_resource* resource);
    // Makes the client a protocol object it asked for with an agent pointer, which is never sent anything and goes
    // when the client destroys it.
    void (*inert)(eInert kind, wl_client* client, uint32_t version, uint32_t id);
};

// Advertises the seat on its first call and returns it; later calls, from any plugin build, return the same one.
extern "C" const SAgentSeat* deskpilot_agent_seat(wl_display* display);
