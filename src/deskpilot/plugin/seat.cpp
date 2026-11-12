// The agent seat's global and the resources clients make on it. See seat.hpp.
//
// The seat and pointer resources are Hyprland's generated wrappers with no data, so every lookup Hyprland's protocols
// make on one (CWLSeatResource::fromResource, CWLPointerResource::fromResource) finds nothing, and a request naming
// the agent seat or pointer is taken as one from no seat. Nothing here sends input: the plugin does.

#include "seat.hpp"

#include <hyprland/protocols/pointer-constraints-unstable-v1.hpp>
#include <hyprland/protocols/pointer-gestures-unstable-v1.hpp>
#include <hyprland/protocols/relative-pointer-unstable-v1.hpp>
#include <hyprutils/memory/SharedPtr.hpp>

#include <algorithm>
#include <fcntl.h>
#include <unistd.h>

using Hyprutils::Memory::CSharedPointer;
using Hyprutils::Memory::CWeakPointer;
using Hyprutils::Memory::makeShared;

namespace {
    std::vector<CSharedPointer<CWlSeat>> g_seats;
    std::vector<CSharedPointer<CWlPointer>> g_pointers;
    std::vector<CSharedPointer<CWlKeyboard>> g_keyboards;
    int g_keymapFd = -1;
    uint32_t g_keymapSize = 0;
    int32_t g_repeatRate = 25, g_repeatDelay = 600;

    template <typename T>
    void forget(std::vector<CSharedPointer<T>>& resources, T* resource) {
        std::erase_if(resources, [resource](const auto& kept) { return kept.get() == resource; });
    }

    void getPointer(CWlSeat* seat, uint32_t id) {
        auto pointer = makeShared<CWlPointer>(seat->client(), seat->version(), id);
        if (!pointer->resource()) {
            seat->noMemory();
            return;
        }
        // The cursor is the user's: an agent pointer's is never shown.
        pointer->setSetCursor([](CWlPointer*, uint32_t, wl_resource*, int32_t, int32_t) {});
        pointer->setRelease([](CWlPointer* self) { forget(g_pointers, self); });
        pointer->setOnDestroy([](CWlPointer* self) { forget(g_pointers, self); });
        g_pointers.emplace_back(std::move(pointer));
    }

    void getKeyboard(CWlSeat* seat, uint32_t id) {
        if (g_keymapFd < 0) {
            seat->error(WL_SEAT_ERROR_MISSING_CAPABILITY, "the deskpilot seat keyboard is not configured");
            return;
        }
        auto keyboard = makeShared<CWlKeyboard>(seat->client(), seat->version(), id);
        if (!keyboard->resource()) {
            seat->noMemory();
            return;
        }
        keyboard->setRelease([](CWlKeyboard* self) { forget(g_keyboards, self); });
        keyboard->setOnDestroy([](CWlKeyboard* self) { forget(g_keyboards, self); });
        g_keyboards.emplace_back(keyboard);
        const auto fd = dup(g_keymapFd);
        if (fd >= 0) {
            keyboard->sendKeymap(WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1, fd, g_keymapSize);
            close(fd);
            if (keyboard->version() >= 4)
                keyboard->sendRepeatInfo(g_repeatRate, g_repeatDelay);
        }
    }

    void bind(wl_client* client, void*, uint32_t version, uint32_t id) {
        auto seat = makeShared<CWlSeat>(client, std::min(version, 9u), id);
        if (!seat->resource()) {
            wl_client_post_no_memory(client);
            return;
        }
        seat->setRelease([](CWlSeat* self) { forget(g_seats, self); });
        seat->setOnDestroy([](CWlSeat* self) { forget(g_seats, self); });
        seat->setGetPointer(getPointer);
        seat->setGetKeyboard(getKeyboard);
        seat->setGetTouch([](CWlSeat* self, uint32_t) { self->error(WL_SEAT_ERROR_MISSING_CAPABILITY, "the deskpilot seat has no touch"); });
        if (seat->version() >= 2)
            seat->sendName("deskpilot");
        seat->sendCapabilities(static_cast<wl_seat_capability>(WL_SEAT_CAPABILITY_POINTER | (g_keymapFd >= 0 ? WL_SEAT_CAPABILITY_KEYBOARD : 0)));
        g_seats.emplace_back(std::move(seat));
    }

    template <typename T>
    std::vector<CSharedPointer<T>> g_inert;

    template <typename T>
    void makeInert(wl_client* client, uint32_t version, uint32_t id) {
        auto object = makeShared<T>(client, version, id);
        if (!object->resource()) {
            wl_client_post_no_memory(client);
            return;
        }
        object->setDestroy([](T* self) { forget(g_inert<T>, self); });
        object->setOnDestroy([](T* self) { forget(g_inert<T>, self); });
        g_inert<T>.emplace_back(std::move(object));
    }

    void inert(eInert kind, wl_client* client, uint32_t version, uint32_t id) {
        switch (kind) {
        case eInert::SWIPE:
            makeInert<CZwpPointerGestureSwipeV1>(client, version, id);
            break;
        case eInert::PINCH:
            makeInert<CZwpPointerGesturePinchV1>(client, version, id);
            break;
        case eInert::HOLD:
            makeInert<CZwpPointerGestureHoldV1>(client, version, id);
            break;
        case eInert::RELATIVE:
            makeInert<CZwpRelativePointerV1>(client, version, id);
            break;
        case eInert::LOCKED:
            makeInert<CZwpLockedPointerV1>(client, version, id);
            break;
        case eInert::CONFINED:
            makeInert<CZwpConfinedPointerV1>(client, version, id);
            break;
        }
    }

    std::vector<CWeakPointer<CWlPointer>> pointersOf(wl_client* client) {
        std::vector<CWeakPointer<CWlPointer>> out;
        for (const auto& pointer : g_pointers) {
            if (pointer->resource() && pointer->client() == client)
                out.emplace_back(pointer);
        }
        return out;
    }

    bool isPointer(wl_resource* resource) {
        return resource && std::ranges::any_of(g_pointers, [resource](const auto& pointer) { return pointer->resource() == resource; });
    }

    std::vector<CWeakPointer<CWlKeyboard>> keyboardsOf(wl_client* client) {
        std::vector<CWeakPointer<CWlKeyboard>> out;
        for (const auto& keyboard : g_keyboards)
            if (keyboard->resource() && keyboard->client() == client)
                out.emplace_back(keyboard);
        return out;
    }

    bool configureKeymap(int fd, uint32_t size, int32_t repeatRate, int32_t repeatDelay) {
        if (fd < 0 || size == 0)
            return false;
        const auto replacement = fcntl(fd, F_DUPFD_CLOEXEC, 3);
        if (replacement < 0)
            return false;
        if (g_keymapFd >= 0)
            close(g_keymapFd);
        g_keymapFd = replacement;
        g_keymapSize = size;
        g_repeatRate = repeatRate;
        g_repeatDelay = repeatDelay;
        for (const auto& seat : g_seats)
            seat->sendCapabilities(static_cast<wl_seat_capability>(WL_SEAT_CAPABILITY_POINTER | WL_SEAT_CAPABILITY_KEYBOARD));
        for (const auto& keyboard : g_keyboards) {
            if (!keyboard->resource())
                continue;
            const auto map = dup(g_keymapFd);
            if (map >= 0) {
                keyboard->sendKeymap(WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1, map, g_keymapSize);
                close(map);
                if (keyboard->version() >= 4)
                    keyboard->sendRepeatInfo(g_repeatRate, g_repeatDelay);
            }
        }
        return true;
    }
} // namespace

extern "C" const SAgentSeat* deskpilot_agent_seat(wl_display* display) {
    static const SAgentSeat SEAT = {
        .pointersOf = pointersOf, .isPointer = isPointer, .inert = inert, .keyboardsOf = keyboardsOf, .configureKeymap = configureKeymap};
    static const bool ADVERTISED = wl_global_create(display, &wl_seat_interface, 9, nullptr, bind) != nullptr;
    return ADVERTISED ? &SEAT : nullptr;
}
