// deskpilot Hyprland plugin: clicks, scrolls and keys that leave the user's pointer and focus where they are.
//
// `hyprctl deskpilot click <window> <x> <y> <button> <count>` and `hyprctl deskpilot scroll <window> <x> <y> <dy>`
// run one input sequence on the user's own seat, so every client receives them, XWayland and Wine included:
//
//   1. The user's mouse input is held through hooks on CInputManager's onMouseMoved, onMouseButton and
//      onMouseWheel, which see the device as well as the event: a replayed release must name the device that
//      pressed, or Hyprland drops it and the button stays held. Nothing the user does can move the pointer mid-click.
//   2. The cursor image is hidden, the pointer warps to the point, and the window under it must be <window>;
//      anything else on top refuses before any press is sent. Only the seat's pointer focus moves: Hyprland's
//      focus-follows-mouse and click-to-focus never run, so keyboard focus stays on the user's window and the
//      target is not raised.
//   3. Presses, releases and wheel notches go straight to the seat, as the end of Hyprland's own handlers does, so
//      compositor mouse binds and scroll factors never see them. They are spaced by event-loop timers, so the client
//      sees them as separate events. Before each press or wheel notch Hyprland's hit tester must still find <window>
//      at the point and the seat's pointer focus must still be on it, so a window, layer surface or popup that
//      appears mid-sequence never has the click passed on to it. Releases always go, so no button is left down.
//   4. The pointer goes back to where the user left it plus the motion they made meanwhile, keyboard focus goes
//      back to the surface that had it if anything moved it, the cursor's visibility is restored and the held input
//      is replayed.
//
// `hyprctl deskpilot keys <window> <hold-ms> <gap-ms> <txn|none> <event>...` presses and releases keys in <window>
// without focusing it. An event is `+code` or `-code` for an evdev key code, `pHEX` or `rHEX` to press or release the
// key that gives character U+HEX unmodified (a chord's ctrl+a), or `tHEX` to type U+HEX. Characters are resolved on the
// active keyboard's keymap, the one every client has, when the action starts: a typed character's key is pressed with
// the modifiers it needs set directly, over the user's locks (Caps Lock) and layout, so text arrives as written on any
// layout. The events run as strokes: a stroke ends when a release leaves no key down. Each
// stroke goes straight to the window client's own wl_keyboard resources: enter with no keys pressed, modifiers, the
// keys and leave. The seat's keyboard focus never moves, so the user's focused client sees no leave, enter or
// modifiers, and Hyprland's window focus never changes. Each press is held <hold-ms> before the next event, and
// strokes are <gap-ms> apart. Modifiers come from an xkb state on the keyboard's own keymap, fed with these keys.
//
//   - Each stroke waits until the window's client has read everything sent before it, so a fast sequence cannot fill
//     the client's buffer, which would make Hyprland disconnect it.
//   - An XWayland window also needs the X input focus, which the window manager sets over its own X connection while
//     the keys travel over Wayland, and the X server does not order the two. So each step waits on what the X server
//     reports through deskpilot's own X connection: the focus is the window before the first key, each key is down
//     or up before the next, and the focus is back before the user's input is let go. If the user's focus is another
//     X window, Xwayland's keyboard leaves it for the stroke and enters it again after, as one wl_keyboard has one
//     focused surface.
//   - The user's keys and mouse are held during a stroke and replayed after it, and a keyboard focus change Hyprland
//     makes meanwhile waits for the stroke to end. A step that does not happen in time ends the sequence with an
//     error.
//
// `hyprctl deskpilot paste <window> <file> <hold-ms> <gap-ms> <txn|none> <event>...` sends the same key events (the
// window's paste chord) with the file's text offered as the clipboard of the window's client alone, just before them.
// The seat's selection never changes, so the user's clipboard and every other client's stay as they are. Once the
// client has read the text, or after PASTE_WAIT, it gets the clipboard it would otherwise have: the user's if it has
// keyboard focus, none if not. An XWayland window is refused, since X clients read one shared clipboard.
//
// A key sequence too long for one hyprctl request is sent ahead in parts with `hyprctl deskpilot stage <txn>
// <event>...`, and the `keys` or `paste` request naming <txn> queues all of it as one action.
//
// Actions run one at a time in the order they arrive; up to MAX_QUEUED wait behind the running one and any more are
// refused. Each request is answered when its own action has finished, or after REPLY_WAIT with
// {"ok":true,"pending":<ticket>}, collected with `hyprctl deskpilot wait <ticket>`, since hyprctl stops reading a
// reply after five seconds. Replies are JSON. `hyprctl deskpilot status` reports the plugin's version and queue.

#include <hyprland/src/plugins/PluginAPI.hpp>

#include <hyprland/src/Compositor.hpp>
#include <hyprland/src/desktop/state/FocusState.hpp>
#include <hyprland/src/desktop/state/ViewHitTester.hpp>
#include <hyprland/src/desktop/view/LayerSurface.hpp>
#include <hyprland/src/desktop/view/WLSurface.hpp>
#include <hyprland/src/desktop/state/ViewQuery.hpp>
#include <hyprland/src/desktop/state/ViewState.hpp>
#include <hyprland/src/desktop/view/window/Window.hpp>
#include <hyprland/src/desktop/view/window/X11Backend.hpp>
#include <hyprland/src/devices/IKeyboard.hpp>
#include <hyprland/src/helpers/defer/Promise.hpp>
#include <hyprland/src/helpers/time/Time.hpp>
#include <hyprland/src/ipc/s1/S1.hpp>
#include <hyprland/src/managers/SeatManager.hpp>
#include <hyprland/src/protocols/LayerShell.hpp>
#include <hyprland/src/protocols/core/Compositor.hpp>
#include <hyprland/src/protocols/core/DataDevice.hpp>
#include <hyprland/src/protocols/core/Seat.hpp>
#include <hyprland/src/managers/SessionLockManager.hpp>
#include <hyprland/src/managers/XWaylandManager.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopManager.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopTimer.hpp>
#include <hyprland/src/managers/fullscreen/FullscreenController.hpp>
#include <hyprland/src/managers/input/InputManager.hpp>
#include <hyprland/src/output/Monitor.hpp>
#include <hyprland/src/pointer/PointerManager.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/state/MonitorState.hpp>
#include <hyprland/src/xwayland/XSurface.hpp>

#include <xcb/xcb.h>
#include <xkbcommon/xkbcommon.h>

#include <algorithm>
#include <bit>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <deque>
#include <dlfcn.h>
#include <fcntl.h>
#include <fstream>
#include <linux/sockios.h>
#include <sys/ioctl.h>
#include <format>
#include <functional>
#include <map>
#include <optional>
#include <sstream>
#include <unistd.h>
#include <string>
#include <vector>

// The hash of this source, set by the Makefile, so `deskpilot plugin load` can tell a stale build from the current one.
#ifndef DESKPILOT_SOURCE
#define DESKPILOT_SOURCE "unknown"
#endif
constexpr const char* VERSION = DESKPILOT_SOURCE;

inline HANDLE PHANDLE = nullptr;

namespace {
    using MoveFn     = void (*)(CInputManager*, IPointer::SMotionEvent);
    using ButtonFn   = void (*)(CInputManager*, IPointer::SButtonEvent, SP<IPointer>);
    using ActivateFn = void (*)(Desktop::View::CWindow*, bool);
    using WheelFn    = void (*)(CInputManager*, IPointer::SAxisEvent, SP<IPointer>);
    using KeyFn      = void (*)(CInputManager*, const IKeyboard::SKeyEvent&, SP<IKeyboard>);
    using ModFn      = void (*)(CInputManager*, SP<IKeyboard>);
    using FocusFn    = void (*)(CSeatManager*, SP<CWLSurfaceResource>);
    using ms         = std::chrono::milliseconds;

    // The modifiers a typed character's key needs: held ones, and the locks it needs (the user's, or none when a lock
    // such as Caps Lock would change the character).
    struct SMods {
        xkb_mod_mask_t depressed = 0;
        xkb_mod_mask_t locked    = 0;
    };

    // One key event. A character is named by its code point until its action starts, when it is resolved to the key
    // that produces it on the active keymap. A typed character carries the modifiers it needs: they are set for its
    // press and the user's restored after its release, with no modifier key pressed.
    struct SKeyEvent {
        enum eKind { CODE, KEY_OF, TYPED } kind = CODE;
        uint32_t             code  = 0; // an evdev key code, or the code point until resolved
        bool                 press = false;
        std::optional<SMods> mods;
    };
    using KeyEvents = std::vector<SKeyEvent>;

    // How long an XWayland key step may take to show in the X server before the sequence gives up.
    constexpr auto X_WAIT = ms(500);
    // How often a step that waits on the X server or a client checks it.
    constexpr auto X_POLL = ms(1);
    // How long the window's client may leave earlier input unread before the next keystroke gives up on it.
    constexpr auto CLIENT_WAIT = std::chrono::seconds(5);

    // Gaps between the steps of a pointer sequence. A client that reads input once per frame (a game) still sees each.
    constexpr auto SETTLE  = ms(40);
    constexpr auto HOLD    = ms(60);
    constexpr auto BETWEEN = ms(80);

    // Actions that may wait behind the running one. A caller beyond this is refused rather than left waiting.
    constexpr size_t MAX_QUEUED = 32;
    // How long a request waits for its action before answering with its ticket: under hyprctl's five second read
    // timeout (SO_RCVTIMEO in hyprctl's request()), after which hyprctl gives up on the reply.
    constexpr auto REPLY_WAIT = std::chrono::seconds(4);
    // Results kept for callers that never collect them (a client killed while it waited); the oldest go first.
    constexpr size_t MAX_UNCOLLECTED = 64;
    // Key transactions being staged at once; a client that died mid-stage leaves one behind, and the oldest goes first.
    constexpr size_t MAX_STAGED = 32;

    struct SOp {
        uintptr_t                          window = 0;
        Vector2D                           target;
        uint32_t                           button = 0;
        int                                count  = 0;
        int                                scroll = 0; // wheel notches, positive up; 0 for a click
        Vector2D                           saved;
        Vector2D                           heldMotion;
        bool                               hadFocus = false;
        PHLWINDOWREF                       focusWindow;
        WP<CWLSurfaceResource>             focusSurface;
        std::vector<std::function<void()>> heldInput;
        std::vector<std::function<void()>> steps;
        size_t                             next = 0;
        SP<CEventLoopTimer>                timer;
        uint64_t                           ticket = 0;
        std::string                        error;
    };

    CFunctionHook*     g_moveHook     = nullptr;
    CFunctionHook*     g_buttonHook   = nullptr;
    CFunctionHook*     g_wheelHook    = nullptr;
    CFunctionHook*     g_keyHook      = nullptr;
    CFunctionHook*     g_modHook      = nullptr;
    CFunctionHook*     g_activateHook = nullptr;
    CFunctionHook*     g_focusHook    = nullptr;
    std::optional<SOp> g_op;

    struct SKeyOp {
        enum ePhase { ENTER, AWAIT_FOCUS, KEY, AWAIT_KEY, RESTORE, AWAIT_RESTORE, DONE } phase = ENTER;
        PHLWINDOWREF                       window;
        WP<CWLSurfaceResource>             surface;
        bool                               x11 = false;
        uint32_t                           xid = 0;
        KeyEvents                          events;
        size_t                             next = 0;
        ms                                 hold{0};
        ms                                 gap{0};
        // The stroke in progress: the window client's wl_keyboard resources it goes to, whether they were entered for
        // it, and the surface of the same client that had keyboard focus and is entered again after it.
        std::vector<WP<CWLKeyboardResource>> keyboards;
        bool                               entered = false;
        WP<CWLSurfaceResource>             sibling;
        bool                               holding = false; // the user's input is held until the stroke ends
        // A keyboard focus change Hyprland asked for mid-stroke, applied when the stroke ends.
        std::optional<WP<CWLSurfaceResource>> deferredFocus;
        std::optional<std::chrono::steady_clock::time_point> clientDeadline;
        uint32_t                           restoreXid = 0; // the X window the X focus goes back to, or 0 for none
        std::vector<uint32_t>              down; // keys this sequence holds down in the window
        xkb_state*                         xkb = nullptr;
        xkb_mod_mask_t                     locked = 0; // the user's locks, which a typed character's modifiers return to
        std::chrono::steady_clock::time_point deadline;
        std::vector<std::function<void()>> heldInput;
        SP<CEventLoopTimer>                timer;
        uint64_t                           ticket = 0;
        std::string                        error;
    };
    std::optional<SKeyOp> g_keyOp;
    xcb_connection_t*     g_x = nullptr;

    // ---- the queue ----

    struct SWaiter {
        SP<CPromiseResolver<std::string>> resolver;
        SP<CEventLoopTimer>               timer;
    };
    struct STicket {
        std::optional<std::string> result;
        std::optional<SWaiter>     waiter;
    };
    // Starts a queued action. It returns an error when the action cannot start, and otherwise runs it to completion,
    // which ends in complete() with its ticket.
    using Start = std::function<std::optional<std::string>(uint64_t ticket)>;
    struct SQueued {
        uint64_t ticket = 0;
        Start    start;
    };
    std::map<uint64_t, STicket> g_tickets;
    std::deque<SQueued>         g_queue;
    uint64_t                    g_nextTicket = 1;

    struct SStaged {
        uint64_t  order = 0;
        KeyEvents events;
    };
    std::map<std::string, SStaged> g_staged;
    uint64_t                       g_stageOrder = 0;

    uint32_t now() {
        return Time::millis(Time::steadyNow());
    }

    // During a keystroke the user's mouse is held with their keys: with focus-follows-mouse, motion into another window
    // would move the seat's keyboard focus away from the window being typed into. Replayed when the stroke ends.
    bool holdForStroke(std::function<void()> replay) {
        if (!g_keyOp || !g_keyOp->holding)
            return false;
        g_keyOp->heldInput.emplace_back(std::move(replay));
        return true;
    }

    void hkOnMouseMoved(CInputManager* self, IPointer::SMotionEvent e) {
        if (g_op) {
            g_op->heldMotion += e.delta;
            return;
        }
        if (holdForStroke([self, e] { reinterpret_cast<MoveFn>(g_moveHook->m_original)(self, e); }))
            return;
        reinterpret_cast<MoveFn>(g_moveHook->m_original)(self, e);
    }

    void hkOnMouseButton(CInputManager* self, IPointer::SButtonEvent e, SP<IPointer> mouse) {
        auto replay = [self, e, mouse] { reinterpret_cast<ButtonFn>(g_buttonHook->m_original)(self, e, mouse); };
        if (g_op) {
            g_op->heldInput.emplace_back(replay);
            return;
        }
        if (!holdForStroke(replay))
            replay();
    }

    // A client clicked or typed into while unfocused asks to be activated (an X client sends _NET_ACTIVE_WINDOW), and
    // with misc:focus_on_activate Hyprland focuses it, raises it and warps the pointer to it. A real click would have
    // focused the window first, so the request would be a no-op; here it would take the user's focus. Requests from
    // the target while deskpilot acts on it are dropped. A forced activation comes from a dispatcher, not the client.
    void hkActivate(Desktop::View::CWindow* self, bool force) {
        const bool TARGET = (g_op && reinterpret_cast<uintptr_t>(self) == g_op->window) || (g_keyOp && g_keyOp->window.lock().get() == self);
        if (TARGET && !force)
            return;
        reinterpret_cast<ActivateFn>(g_activateHook->m_original)(self, force);
    }

    void hkOnMouseWheel(CInputManager* self, IPointer::SAxisEvent e, SP<IPointer> pointer) {
        auto replay = [self, e, pointer] { reinterpret_cast<WheelFn>(g_wheelHook->m_original)(self, e, pointer); };
        if (g_op) {
            g_op->heldInput.emplace_back(replay);
            return;
        }
        if (!holdForStroke(replay))
            replay();
    }

    void hkOnKeyboardKey(CInputManager* self, const IKeyboard::SKeyEvent& e, SP<IKeyboard> keyboard) {
        auto replay = [self, e, keyboard] { reinterpret_cast<KeyFn>(g_keyHook->m_original)(self, e, keyboard); };
        if (!holdForStroke(replay))
            replay();
    }

    void hkOnKeyboardMod(CInputManager* self, SP<IKeyboard> keyboard) {
        auto replay = [self, keyboard] { reinterpret_cast<ModFn>(g_modHook->m_original)(self, keyboard); };
        if (!holdForStroke(replay))
            replay();
    }

    // Hyprland refocuses on its own events (a layer surface closing, a window opening) as well as on the user's input,
    // and a focus change sends every wl_keyboard a leave, the window being typed into included. Mid-stroke the change
    // waits for the stroke to end, as the user's own input does. Hyprland's record of the focused window updates at once.
    void hkSetKeyboardFocus(CSeatManager* self, SP<CWLSurfaceResource> surface) {
        if (g_keyOp && g_keyOp->holding) {
            g_keyOp->deferredFocus = surface;
            return;
        }
        reinterpret_cast<FocusFn>(g_focusHook->m_original)(self, surface);
    }

    std::string jsonString(const std::string& s) {
        std::string out = "\"";
        for (char c : s) {
            if (c == '"' || c == '\\')
                out += '\\';
            if (static_cast<unsigned char>(c) < 0x20)
                continue;
            out += c;
        }
        return out + "\"";
    }

    std::string okReply() {
        return R"({"ok":true})";
    }

    std::string errorReply(const std::string& why) {
        return std::format(R"({{"ok":false,"error":{}}})", jsonString(why));
    }

    IPC::Socket1::SResponse refuse(const std::string& why) {
        return IPC::Socket1::SResponse(errorReply(why));
    }

    void dropTimer(const SP<CEventLoopTimer>& timer) {
        if (!timer)
            return;
        timer->cancel();
        g_pEventLoopManager->removeTimer(timer);
    }

    void startNext();

    // An action has finished: its caller gets the reply now if it is waiting, or from `wait` later.
    void complete(uint64_t ticket, const std::string& reply) {
        auto it = g_tickets.find(ticket);
        if (it == g_tickets.end())
            return;
        if (it->second.waiter) {
            auto waiter = std::move(*it->second.waiter);
            g_tickets.erase(it);
            dropTimer(waiter.timer);
            waiter.resolver->resolve(reply);
            return;
        }
        it->second.result = reply;
        size_t uncollected = std::ranges::count_if(g_tickets, [](const auto& entry) { return entry.second.result.has_value(); });
        for (auto old = g_tickets.begin(); uncollected > MAX_UNCOLLECTED && old != g_tickets.end();) {
            if (old->second.result) {
                old = g_tickets.erase(old);
                --uncollected;
            } else
                ++old;
        }
    }

    // The reply to a request for the action with this ticket: its result if it has finished, else a promise kept
    // when it finishes or after REPLY_WAIT with the ticket to wait on again.
    IPC::Socket1::SResponse awaitTicket(uint64_t ticket) {
        auto it = g_tickets.find(ticket);
        if (it == g_tickets.end())
            return refuse(std::format("no deskpilot action {}: its result was collected already or dropped", ticket));
        if (it->second.result) {
            auto reply = std::move(*it->second.result);
            g_tickets.erase(it);
            return IPC::Socket1::SResponse(reply);
        }
        if (it->second.waiter)
            return refuse(std::format("another caller is already waiting on deskpilot action {}", ticket));

        SWaiter waiter;
        auto    promise = CPromise<std::string>::make([&waiter](SP<CPromiseResolver<std::string>> resolver) { waiter.resolver = resolver; });
        waiter.timer    = makeShared<CEventLoopTimer>(
            REPLY_WAIT,
            [ticket](SP<CEventLoopTimer>, void*) {
                auto found = g_tickets.find(ticket);
                if (found == g_tickets.end() || !found->second.waiter)
                    return;
                auto waiting = std::move(*found->second.waiter);
                found->second.waiter.reset();
                dropTimer(waiting.timer);
                waiting.resolver->resolve(std::format(R"({{"ok":true,"pending":{}}})", ticket));
            },
            nullptr);
        g_pEventLoopManager->addTimer(waiter.timer);
        it->second.waiter = std::move(waiter);
        return IPC::Socket1::SResponse(promise);
    }

    IPC::Socket1::SResponse enqueue(Start start) {
        if (g_queue.size() >= MAX_QUEUED)
            return refuse(std::format("the deskpilot queue is full: {} actions are waiting behind the running one, try again when they finish", g_queue.size()));
        const auto TICKET = g_nextTicket++;
        g_tickets[TICKET] = {};
        g_queue.push_back({TICKET, std::move(start)});
        startNext();
        return awaitTicket(TICKET);
    }

    void startNext() {
        while (!g_op && !g_keyOp && !g_queue.empty()) {
            auto queued = std::move(g_queue.front());
            g_queue.pop_front();
            if (const auto ERROR = queued.start(queued.ticket))
                complete(queued.ticket, errorReply(*ERROR));
        }
    }

    // ---- pointer ----

    std::string describe(const PHLWINDOW& window) {
        return std::format("window 0x{:x} ({})", reinterpret_cast<uintptr_t>(window.get()), window->metadata().title());
    }

    PHLWINDOW windowUnderPointer() {
        const auto SURFACE = g_pSeatManager->m_state.pointerFocus.lock();
        if (!SURFACE)
            return nullptr;
        const auto HLSURFACE = Desktop::View::CWLSurface::fromResource(SURFACE);
        return HLSURFACE ? Desktop::View::CWindow::fromView(HLSURFACE->view()) : nullptr;
    }

    // What a pointer at `pos` lands on, asked of Hyprland's hit tester in the order mouseMoveUnified looks: input
    // method popups, focus-priority windows, layer popups, overlay and top layers (a top layer only above an exclusive
    // fullscreen window if it may be), then windows with the extents processMouseDownNormal uses. Nothing when it is
    // the target, else what is there instead. It only asks, so neither focus moves.
    std::optional<std::string> blockerAt(const Vector2D& pos, uintptr_t target) {
        using namespace Desktop::View;
        const auto AT = std::format("at {},{}", pos.x, pos.y);
        if (g_pSessionLockManager->isSessionLocked())
            return "the session is locked";
        if (g_pInputManager->m_relay.popupFromCoords(pos))
            return "an input method popup is on top " + AT;
        if (const auto W = Desktop::viewState()->hitTest().windowAt(pos, FOCUS_PRIORITY); W && reinterpret_cast<uintptr_t>(W.get()) != target)
            return describe(W) + " is on top " + AT;

        const auto MONITOR = State::monitorState()->query().vec(pos).run();
        if (!MONITOR)
            return "no monitor " + AT;
        Vector2D local;
        PHLLS    layer;
        if (Desktop::viewState()->hitTest().layerPopupSurfaceAt(pos, MONITOR, &local, &layer))
            return std::format("a popup of layer surface '{}' is on top {}", layer ? layer->m_namespace : "", AT);
        const auto WORKSPACE = MONITOR->m_activeSpecialWorkspace ? MONITOR->m_activeSpecialWorkspace : MONITOR->m_activeWorkspace;
        const bool EXCLUSIVE_FULLSCREEN =
            (WORKSPACE && Fullscreen::controller()->hasFullscreen(WORKSPACE) && Fullscreen::controller()->getFullscreenModes(WORKSPACE).internal == Fullscreen::FSMODE_FULLSCREEN) ||
            (Fullscreen::controller()->hasFullscreen(MONITOR) && Fullscreen::controller()->getFullscreenModes(MONITOR).internal == Fullscreen::FSMODE_FULLSCREEN);
        for (const auto LEVEL : {ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, ZWLR_LAYER_SHELL_V1_LAYER_TOP}) {
            layer = nullptr;
            if (!Desktop::viewState()->hitTest().layerSurfaceAt(pos, &MONITOR->m_layerSurfaceLayers[LEVEL], &local, &layer))
                continue;
            if (LEVEL == ZWLR_LAYER_SHELL_V1_LAYER_TOP && EXCLUSIVE_FULLSCREEN && layer && !(layer->m_flags & LAYER_FLAG_ABOVE_FULLSCREEN))
                continue;
            return std::format("layer surface '{}' is on top {}", layer ? layer->m_namespace : "", AT);
        }

        const auto WINDOW = Desktop::viewState()->hitTest().windowAt(pos, ALLOW_FLOATING | RESERVED_EXTENTS | INPUT_EXTENTS);
        if (WINDOW && reinterpret_cast<uintptr_t>(WINDOW.get()) == target)
            return std::nullopt;
        return WINDOW ? describe(WINDOW) + " is on top " + AT : "no window " + AT;
    }

    // The target must be what is at the point, fresh from the hit tester, and still hold the seat's pointer focus
    // (where a press goes) before anything is sent to it.
    bool targetUnderPointer() {
        if (const auto BLOCKER = blockerAt(g_op->target, g_op->window)) {
            g_op->error = *BLOCKER;
            return false;
        }
        const auto UNDER = windowUnderPointer();
        if (UNDER && reinterpret_cast<uintptr_t>(UNDER.get()) == g_op->window)
            return true;
        g_op->error = std::format("the pointer focus is on {}, not the target, at {},{}", UNDER ? describe(UNDER) : "no window", g_op->target.x, g_op->target.y);
        return false;
    }

    // Pointer focus to the surface under the pointer, with an enter at the pointer's position, and nothing else:
    // simulateMouseMovement would also run focus-follows-mouse and move keyboard focus. Leaving first makes the enter
    // carry the new position even when the pointer was already on that surface, since setPointerFocus skips a
    // surface it already has and refocusPointer sends no motion.
    void enterUnderPointer() {
        g_pSeatManager->setPointerFocus(nullptr, {});
        g_pInputManager->refocusPointer();
        // Xwayland applies pointer events per frame: without one, the press arrives before the motion.
        g_pSeatManager->sendPointerFrame();
    }

    void finish() {
        auto op = std::move(*g_op);
        g_op.reset();
        dropTimer(op.timer);

        Pointer::mgr()->warpTo(op.saved + op.heldMotion);
        enterUnderPointer();
        // Keyboard focus goes back to the window, layer surface or nothing that had it. One that closed meanwhile
        // leaves focus where Hyprland put it.
        const auto WINDOW  = op.focusWindow.lock();
        const auto SURFACE = op.focusSurface.lock();
        const auto STATE   = Desktop::focusState();
        if (WINDOW && (WINDOW != STATE->window() || (SURFACE && SURFACE != STATE->surface())))
            STATE->fullWindowFocus(WINDOW, Desktop::FOCUS_REASON_UNKNOWN, SURFACE);
        else if (!WINDOW && (SURFACE || !op.hadFocus))
            STATE->rawSurfaceFocus(SURFACE);
        // Hyprland's own rules decide whether the cursor shows (cursor:invisible, the inactive timeout and so on).
        g_pHyprRenderer->ensureCursorRenderingMode();

        for (auto& replay : op.heldInput)
            replay();

        complete(op.ticket,
                 op.error.empty() ? std::format(R"({{"ok":true,"x":{},"y":{}}})", op.target.x, op.target.y) : errorReply(op.error));
        startNext();
    }

    void runNext(SP<CEventLoopTimer>, void*) {
        if (!g_op)
            return;
        if (!g_op->error.empty() || g_op->next >= g_op->steps.size()) {
            finish();
            return;
        }
        g_op->steps[g_op->next++]();
        if (g_op)
            g_op->timer->updateTimeout(g_op->next >= g_op->steps.size() ? SETTLE : (g_op->scroll ? BETWEEN / 2 : HOLD));
    }

    // Each step runs on its own timer tick; a step that fails sets the error and the next tick restores the user.
    void buildSteps(SOp& op) {
        op.steps.push_back([] {
            Pointer::mgr()->warpTo(g_op->target);
            enterUnderPointer();
            targetUnderPointer();
        });
        if (op.scroll) {
            for (int i = 0; i < std::abs(op.scroll); ++i) {
                op.steps.push_back([] {
                    if (!targetUnderPointer())
                        return;
                    // One wheel notch as onMouseWheel sends it with a scroll factor of 1: delta 15, one discrete step,
                    // value120 of 120. Negative is up in Wayland.
                    const int SIGN = g_op->scroll > 0 ? -1 : 1;
                    g_pSeatManager->sendPointerAxis(now(), WL_POINTER_AXIS_VERTICAL_SCROLL, 15.0 * SIGN, SIGN, 120 * SIGN, WL_POINTER_AXIS_SOURCE_WHEEL,
                                                    WL_POINTER_AXIS_RELATIVE_DIRECTION_IDENTICAL);
                    g_pSeatManager->sendPointerFrame();
                });
            }
            return;
        }
        for (int i = 0; i < op.count; ++i) {
            for (auto state : {WL_POINTER_BUTTON_STATE_PRESSED, WL_POINTER_BUTTON_STATE_RELEASED}) {
                op.steps.push_back([state] {
                    if (state == WL_POINTER_BUTTON_STATE_PRESSED && !targetUnderPointer())
                        return;
                    // The seat call processMouseDownNormal ends with, without its refocus and raise before it.
                    g_pSeatManager->sendPointerButton(now(), g_op->button, state);
                    g_pSeatManager->sendPointerFrame();
                });
            }
        }
    }

    // Starts a click or scroll; the user's pointer and focus are saved now, when it runs, not when it was queued.
    std::optional<std::string> startPointer(SOp op, uint64_t ticket) {
        op.ticket       = ticket;
        op.saved        = g_pInputManager->getMouseCoordsInternal();
        op.focusWindow  = Desktop::focusState()->window();
        op.focusSurface = Desktop::focusState()->surface();
        op.hadFocus     = op.focusWindow || op.focusSurface;
        buildSteps(op);
        g_op.emplace(std::move(op));

        g_pHyprRenderer->setCursorHidden(true);
        g_op->timer = makeShared<CEventLoopTimer>(ms(1), runNext, nullptr);
        g_pEventLoopManager->addTimer(g_op->timer);
        return std::nullopt;
    }

    std::optional<double> number(const std::string& s) {
        double v = 0;
        auto [end, ec] = std::from_chars(s.data(), s.data() + s.size(), v);
        if (ec != std::errc{} || end != s.data() + s.size())
            return std::nullopt;
        return v;
    }

    std::optional<uintptr_t> address(std::string s) {
        if (s.starts_with("0x"))
            s = s.substr(2);
        uintptr_t v = 0;
        auto [end, ec] = std::from_chars(s.data(), s.data() + s.size(), v, 16);
        if (ec != std::errc{} || end != s.data() + s.size() || v == 0)
            return std::nullopt;
        return v;
    }

    // This build's own file, which `hyprctl plugin unload` needs.
    std::string ownPath() {
        Dl_info info;
        return dladdr(reinterpret_cast<void*>(&ownPath), &info) && info.dli_fname ? info.dli_fname : "";
    }

    // ---- keys ----

    // CX11Backend keeps its X surface private, and with it the window's X id. Explicit instantiation may name a
    // private member, so this takes a pointer to it; the hash check in PLUGIN_INIT guarantees these are the running
    // Hyprland's own headers.
    template <auto MEMBER, typename TAG>
    struct SAccess {
        friend constexpr auto member(TAG) {
            return MEMBER;
        }
    };
    struct SX11Surface {
        friend constexpr auto member(SX11Surface);
    };
    template struct SAccess<&Desktop::View::CX11Backend::m_xwaylandSurface, SX11Surface>;

    uint32_t xidOf(PHLWINDOW window) {
        const auto& BACKEND = static_cast<const Desktop::View::CX11Backend&>(window->backend());
        const auto  SURFACE = (BACKEND.*member(SX11Surface{})).lock();
        return SURFACE ? SURFACE->m_xID : 0;
    }

    bool xOpen() {
        if (g_x && !xcb_connection_has_error(g_x))
            return true;
        if (g_x)
            xcb_disconnect(g_x);
        g_x = xcb_connect(std::getenv("DISPLAY"), nullptr); // Hyprland exports its Xwayland display to itself
        return !xcb_connection_has_error(g_x);
    }

    uint32_t xFocus() {
        auto*          reply = xcb_get_input_focus_reply(g_x, xcb_get_input_focus(g_x), nullptr);
        const uint32_t FOCUS = reply ? reply->focus : UINT32_MAX;
        free(reply);
        return FOCUS;
    }

    bool xKeyDown(uint32_t code) {
        auto* reply = xcb_query_keymap_reply(g_x, xcb_query_keymap(g_x), nullptr);
        if (!reply)
            return false;
        const uint32_t KEYCODE = code + 8; // evdev to X keycode
        const bool     DOWN    = KEYCODE < 256 && (reply->keys[KEYCODE / 8] & (1u << (KEYCODE % 8)));
        free(reply);
        return DOWN;
    }

    // A fresh xkb state on the keyboard's keymap, keeping the user's locks (Caps Lock) and layout but none of the
    // modifiers they physically hold.
    xkb_state* scratchState(const SP<IKeyboard>& keyboard) {
        auto* xkb = xkb_state_new(keyboard->m_xkbKeymap);
        if (xkb)
            xkb_state_update_mask(xkb, 0, 0, keyboard->m_modifiersState.locked, 0, 0, keyboard->m_modifiersState.group);
        return xkb;
    }

    // The seat's wl_keyboard resources owned by one client. CWLSeatProtocol keeps them private (see SX11Surface).
    struct SSeatKeyboards {
        friend constexpr auto member(SSeatKeyboards);
    };
    template struct SAccess<&CWLSeatProtocol::m_keyboards, SSeatKeyboards>;

    std::vector<WP<CWLKeyboardResource>> keyboardsOf(wl_client* client) {
        std::vector<WP<CWLKeyboardResource>> out;
        for (const auto& keyboard : (*PROTO::seat).*member(SSeatKeyboards{})) {
            if (keyboard && keyboard->m_owner && keyboard->m_owner->client() == client)
                out.emplace_back(keyboard);
        }
        return out;
    }

    // ---- paste ----

    // CWLDataDeviceProtocol keeps the lookup of a client's data device and the sending of a selection to one device
    // private: Hyprland itself only ever offers the seat's selection to the focused client.
    struct SDeviceForClient {
        friend constexpr auto member(SDeviceForClient);
    };
    template struct SAccess<&CWLDataDeviceProtocol::dataDeviceForClient, SDeviceForClient>;
    struct SSendSelection {
        friend constexpr auto member(SSendSelection);
    };
    template struct SAccess<&CWLDataDeviceProtocol::sendSelectionToDevice, SSendSelection>;

    class CPasteSource;
    struct SPaste {
        SP<CPasteSource>       source;
        WP<IDataDevice>        device;
        WP<CWLSurfaceResource> surface;
        SP<CEventLoopTimer>    timer;
    };
    std::optional<SPaste> g_paste;
    void                  restorePaste();

    // Text a paste offers as one window client's clipboard. It is never the seat's selection, so the user's clipboard
    // and every other client's stay as they are. A read is written as the client's pipe takes it, so a long text never
    // blocks the compositor.
    class CPasteSource : public IDataSource {
      public:
        explicit CPasteSource(std::string text) : m_text(std::move(text)) {}
        ~CPasteSource() override {
            for (auto* write : m_writes) {
                wl_event_source_remove(write->event);
                delete write;
            }
        }

        std::vector<std::string> mimes() override {
            return {"text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "TEXT", "STRING"};
        }
        void send(const std::string&, Hyprutils::OS::CFileDescriptor fd) override {
            auto* write = new SWrite{.source = this, .fd = std::move(fd)};
            fcntl(write->fd.get(), F_SETFL, fcntl(write->fd.get(), F_GETFL) | O_NONBLOCK);
            write->event = wl_event_loop_add_fd(g_pCompositor->m_wlEventLoop, write->fd.get(), WL_EVENT_WRITABLE, onWritable, write);
            m_writes.push_back(write);
        }
        void accepted(const std::string&) override {}
        void cancelled() override {}
        void error(uint32_t, const std::string&) override {}

      private:
        struct SWrite {
            CPasteSource*                  source = nullptr;
            Hyprutils::OS::CFileDescriptor fd;
            size_t                         written = 0;
            wl_event_source*               event   = nullptr;
        };

        static int onWritable(int, uint32_t mask, void* data) {
            auto*       write = static_cast<SWrite*>(data);
            const auto& TEXT  = write->source->m_text;
            while (!(mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR)) && write->written < TEXT.size()) {
                const auto N = ::write(write->fd.get(), TEXT.data() + write->written, TEXT.size() - write->written);
                if (N < 0 && errno == EINTR)
                    continue;
                if (N < 0 && errno == EAGAIN)
                    return 0;
                if (N <= 0)
                    break;
                write->written += N;
            }
            // Read once: the window has its text, so it gets the user's clipboard back (or none, when unfocused).
            // Restored here rather than later, so no callback into this plugin is left queued if it is unloaded.
            auto* source = write->source;
            std::erase(source->m_writes, write);
            wl_event_source_remove(write->event);
            delete write;
            if (g_paste && g_paste->source.get() == source)
                restorePaste();
            return 0;
        }

        std::string          m_text;
        std::vector<SWrite*> m_writes;
    };

    // How long a window may take to read a pasted text before its clipboard is given back anyway.
    constexpr auto PASTE_WAIT = std::chrono::seconds(10);

    // The window's client sees the clipboard it would have without the paste: the user's, if it has keyboard focus,
    // and none otherwise, since Hyprland offers the clipboard only to the focused client.
    void restorePaste() {
        if (!g_paste)
            return;
        auto paste = std::move(*g_paste);
        g_paste.reset();
        dropTimer(paste.timer);
        const auto DEVICE = paste.device.lock();
        if (!DEVICE)
            return;
        const auto FOCUS   = g_pSeatManager->m_state.keyboardFocusResource.lock();
        const auto SURFACE = paste.surface.lock();
        const bool FOCUSED = FOCUS && SURFACE && FOCUS->client() == SURFACE->client();
        ((*PROTO::data).*member(SSendSelection{}))(DEVICE, FOCUSED ? g_pSeatManager->m_selection.currentSelection.lock() : nullptr);
    }

    // Offers `text` as the clipboard of `window`'s client alone, until it has read it or PASTE_WAIT passes.
    std::optional<std::string> offerPaste(const PHLWINDOW& window, std::string text) {
        if (!window || !window->mapped())
            return "the window closed before its text was offered";
        const auto SURFACE = window->wlSurface()->resource();
        const auto DEVICE  = SURFACE ? ((*PROTO::data).*member(SDeviceForClient{}))(SURFACE->client()) : nullptr;
        if (!DEVICE || DEVICE->type() != DATA_SOURCE_TYPE_WAYLAND)
            return "the window's client has no Wayland clipboard to paste from";
        restorePaste();
        g_paste.emplace(SPaste{.source = makeShared<CPasteSource>(std::move(text)), .device = DEVICE, .surface = SURFACE});
        g_paste->timer = makeShared<CEventLoopTimer>(PASTE_WAIT, [](SP<CEventLoopTimer>, void*) { restorePaste(); }, nullptr);
        g_pEventLoopManager->addTimer(g_paste->timer);
        ((*PROTO::data).*member(SSendSelection{}))(DEVICE, g_paste->source);
        return std::nullopt;
    }

    template <typename FN>
    void eachKeyboard(SKeyOp& op, FN&& fn) {
        for (const auto& weak : op.keyboards) {
            if (const auto KEYBOARD = weak.lock())
                fn(KEYBOARD);
        }
    }

    void sendMods(SKeyOp& op) {
        const auto DEPRESSED = xkb_state_serialize_mods(op.xkb, XKB_STATE_MODS_DEPRESSED), LATCHED = xkb_state_serialize_mods(op.xkb, XKB_STATE_MODS_LATCHED),
                   LOCKED = xkb_state_serialize_mods(op.xkb, XKB_STATE_MODS_LOCKED), GROUP = xkb_state_serialize_layout(op.xkb, XKB_STATE_LAYOUT_EFFECTIVE);
        eachKeyboard(op, [&](const auto& keyboard) { keyboard->sendMods(DEPRESSED, LATCHED, LOCKED, GROUP); });
    }

    // The modifiers the user's keyboard really has, as the seat would send them.
    void sendRealMods(SKeyOp& op) {
        const auto KEYBOARD = g_pSeatManager->m_keyboard.lock();
        if (!KEYBOARD)
            return;
        const auto& MODS = KEYBOARD->m_modifiersState;
        eachKeyboard(op, [&](const auto& keyboard) { keyboard->sendMods(MODS.depressed, MODS.latched, MODS.locked, MODS.group); });
    }

    // The modifiers a typed character needs, in the user's layout, as if their keys were held.
    void setMods(SKeyOp& op, SMods mods) {
        xkb_state_update_mask(op.xkb, mods.depressed, 0, mods.locked, 0, 0, xkb_state_serialize_layout(op.xkb, XKB_STATE_LAYOUT_LOCKED));
        sendMods(op);
    }

    // One key to the window's keyboards, then the modifiers it leads to (wl_keyboard: modifiers follow the key that
    // changed them).
    void sendKey(SKeyOp& op, uint32_t code, bool press) {
        const auto TIME = now();
        eachKeyboard(op, [&](const auto& keyboard) { keyboard->sendKey(TIME, code, press ? WL_KEYBOARD_KEY_STATE_PRESSED : WL_KEYBOARD_KEY_STATE_RELEASED); });
        xkb_state_update_key(op.xkb, code + 8, press ? XKB_KEY_DOWN : XKB_KEY_UP);
        sendMods(op);
        if (press)
            op.down.push_back(code);
        else
            std::erase(op.down, code);
    }

    void releaseAll(SKeyOp& op) {
        while (!op.down.empty())
            sendKey(op, op.down.back(), false);
    }

    void finishKeys() {
        auto op = std::move(*g_keyOp);
        g_keyOp.reset();
        dropTimer(op.timer);
        if (op.xkb)
            xkb_state_unref(op.xkb);
        for (auto& replay : op.heldInput)
            replay();
        complete(op.ticket, op.error.empty() ? okReply() : errorReply(op.error));
        startNext();
    }

    // The stroke is over: a focus change Hyprland made meanwhile takes effect, the user's held input is replayed, and
    // the next stroke follows after the gap.
    std::optional<ms> endStroke(SKeyOp& op) {
        op.holding = false;
        if (const auto DEFERRED = std::exchange(op.deferredFocus, std::nullopt))
            reinterpret_cast<FocusFn>(g_focusHook->m_original)(g_pSeatManager.get(), DEFERRED->lock());
        auto held = std::move(op.heldInput);
        op.heldInput.clear();
        for (auto& replay : held)
            replay();
        if (!op.error.empty() || op.next >= op.events.size()) {
            op.phase = SKeyOp::DONE;
            return std::nullopt;
        }
        op.phase = SKeyOp::ENTER;
        return op.gap;
    }

    // Keys this stroke holds are released and its keyboards left, or given back the user's modifiers if the window
    // had keyboard focus all along; a sibling surface of the same client is entered again, and the X focus goes back
    // to the X window Hyprland has focused.
    std::optional<ms> restoreStroke(SKeyOp& op) {
        releaseAll(op);
        if (!op.entered) {
            sendRealMods(op);
            return endStroke(op);
        }
        eachKeyboard(op, [](const auto& keyboard) { keyboard->sendLeave(); });
        if (const auto SIBLING = op.sibling.lock()) {
            wl_array keys;
            wl_array_init(&keys);
            const auto& PRESSED = g_pInputManager->getKeysFromAllKBs();
            if (!PRESSED.empty()) {
                if (auto* data = wl_array_add(&keys, PRESSED.size() * sizeof(uint32_t)))
                    std::ranges::copy(PRESSED, static_cast<uint32_t*>(data));
            }
            eachKeyboard(op, [&](const auto& keyboard) { keyboard->sendEnter(SIBLING, &keys); });
            wl_array_release(&keys);
            sendRealMods(op);
        }
        if (!op.x11)
            return endStroke(op);
        const auto FOCUSED = Desktop::focusState()->window();
        op.restoreXid      = FOCUSED && FOCUSED->backend().isX11() ? xidOf(FOCUSED) : 0;
        if (op.restoreXid)
            g_pXWaylandManager->activateSurface(FOCUSED->wlSurface()->resource(), true);
        else
            g_pXWaylandManager->activateSurface(op.surface.lock(), false);
        op.phase    = SKeyOp::AWAIT_RESTORE;
        op.deadline = std::chrono::steady_clock::now() + X_WAIT;
        return X_POLL;
    }

    // After a key: a press is held before the next event; a release that leaves no key down ends the stroke.
    std::optional<ms> afterKey(SKeyOp& op) {
        const bool PRESS = op.events[op.next].press;
        op.next++;
        const bool LAST = op.next >= op.events.size();
        if (PRESS) {
            op.phase = LAST ? SKeyOp::RESTORE : SKeyOp::KEY;
            return op.hold;
        }
        op.phase = op.down.empty() || LAST ? SKeyOp::RESTORE : SKeyOp::KEY;
        return std::nullopt;
    }

    // One step of a key sequence: how long to wait before the next, or nothing to go on in this event-loop turn.
    std::optional<ms> keyStep(SKeyOp& op) {
        const bool EXPIRED = std::chrono::steady_clock::now() > op.deadline;
        switch (op.phase) {
            case SKeyOp::ENTER: {
                const auto WINDOW = op.window.lock();
                if (!WINDOW || !WINDOW->mapped()) {
                    op.error = "the window closed mid-sequence";
                    op.phase = SKeyOp::DONE;
                    return std::nullopt;
                }
                const auto SURFACE = WINDOW->wlSurface()->resource();
                // Backpressure: the next stroke waits until the window's client has read everything sent to it, so a
                // fast sequence never fills the client's buffer, which would make Hyprland disconnect it.
                if (auto* client = SURFACE->client()) {
                    wl_client_flush(client);
                    int unread = 0;
                    if (ioctl(wl_client_get_fd(client), SIOCOUTQ, &unread) == 0 && unread > 0) {
                        const auto NOW = std::chrono::steady_clock::now();
                        if (!op.clientDeadline)
                            op.clientDeadline = NOW + CLIENT_WAIT;
                        if (NOW < *op.clientDeadline)
                            return X_POLL;
                        op.error = "the window's client stopped reading its input";
                        op.phase = SKeyOp::DONE;
                        return std::nullopt;
                    }
                }
                op.clientDeadline.reset();
                // The window's own client gets enter, keys and leave on its wl_keyboard resources; the seat's keyboard
                // focus, and with it the user's client, is never touched. Only a surface of the same client that has
                // keyboard focus is left and entered again, since one wl_keyboard has one focused surface.
                const auto FOCUS = g_pSeatManager->m_state.keyboardFocus.lock();
                op.surface       = SURFACE;
                op.keyboards     = keyboardsOf(SURFACE->client());
                op.entered       = FOCUS != SURFACE;
                op.sibling       = op.entered && FOCUS && FOCUS->client() == SURFACE->client() ? FOCUS : nullptr;
                if (op.keyboards.empty()) {
                    op.error = "the window's client has no keyboard to send keys to";
                    op.phase = SKeyOp::DONE;
                    return std::nullopt;
                }
                op.holding = true;
                if (op.entered) {
                    if (op.x11) {
                        op.xid = xidOf(WINDOW);
                        if (!op.xid || !xOpen()) {
                            op.error = "cannot reach the X server for this XWayland window";
                            return endStroke(op);
                        }
                    }
                    wl_array keys;
                    wl_array_init(&keys);
                    eachKeyboard(op, [&](const auto& keyboard) {
                        keyboard->sendLeave();
                        keyboard->sendEnter(SURFACE, &keys);
                    });
                    wl_array_release(&keys);
                    if (op.x11) {
                        g_pXWaylandManager->activateSurface(SURFACE, true);
                        op.phase    = SKeyOp::AWAIT_FOCUS;
                        op.deadline = std::chrono::steady_clock::now() + X_WAIT;
                        return X_POLL;
                    }
                }
                sendMods(op);
                op.phase = SKeyOp::KEY;
                return std::nullopt;
            }
            case SKeyOp::AWAIT_FOCUS:
                if (xFocus() == op.xid) {
                    sendMods(op);
                    op.phase = SKeyOp::KEY;
                    return std::nullopt;
                }
                if (EXPIRED) {
                    op.error = "the X window did not take the X input focus";
                    return restoreStroke(op);
                }
                return X_POLL;
            case SKeyOp::KEY: {
                if (!op.window.lock() || !op.surface.lock()) {
                    op.error = "the window closed mid-sequence";
                    return restoreStroke(op);
                }
                // Xwayland hands keys to the X focus, which Hyprland moves when it focuses another X window.
                if (op.x11 && op.entered && xFocus() != op.xid) {
                    op.error = "another X window took the X input focus mid-sequence";
                    return restoreStroke(op);
                }
                const auto EVENT = op.events[op.next];
                if (EVENT.mods && EVENT.press)
                    setMods(op, *EVENT.mods);
                sendKey(op, EVENT.code, EVENT.press);
                if (EVENT.mods && !EVENT.press)
                    setMods(op, {.locked = op.locked});
                if (!(op.x11 && op.entered))
                    return afterKey(op);
                op.phase    = SKeyOp::AWAIT_KEY;
                op.deadline = std::chrono::steady_clock::now() + X_WAIT;
                return X_POLL;
            }
            case SKeyOp::AWAIT_KEY:
                if (xKeyDown(op.events[op.next].code) == op.events[op.next].press)
                    return afterKey(op);
                if (EXPIRED) {
                    op.error = std::format("key {} did not reach the X server", op.events[op.next].code);
                    return restoreStroke(op);
                }
                return X_POLL;
            case SKeyOp::RESTORE: return restoreStroke(op);
            case SKeyOp::AWAIT_RESTORE:
                if (op.restoreXid ? xFocus() == op.restoreXid : xFocus() != op.xid)
                    return endStroke(op);
                if (EXPIRED) {
                    op.error = op.error.empty() ? "the X input focus did not return to the window that had it" : op.error;
                    return endStroke(op);
                }
                return X_POLL;
            case SKeyOp::DONE: break;
        }
        return std::nullopt;
    }

    // Runs steps until one has to wait: with no hold, a Wayland stroke goes out in one event-loop turn.
    void keyTick(SP<CEventLoopTimer>, void*) {
        while (g_keyOp) {
            if (g_keyOp->phase == SKeyOp::DONE) {
                finishKeys();
                return;
            }
            const auto WAIT = keyStep(*g_keyOp);
            if (WAIT && WAIT->count() > 0) {
                g_keyOp->timer->updateTimeout(*WAIT);
                return;
            }
        }
    }

    xkb_keysym_t keysymOf(uint32_t codePoint) {
        if (codePoint == '\n')
            return XKB_KEY_Return;
        if (codePoint == '\t')
            return XKB_KEY_Tab;
        return xkb_utf32_to_keysym(codePoint);
    }

    // The key that gives `keysym` on `keymap` under locks `locked` and the user's layout, with the fewest modifiers, as
    // its evdev code and those modifiers. With `bare`, only a key that gives it with no modifiers: the key a chord such
    // as ctrl+a names. Lock modifiers are never chosen, since holding one is not how a key is typed.
    std::optional<std::pair<uint32_t, xkb_mod_mask_t>> keyUnder(xkb_keymap* keymap, xkb_keysym_t keysym, xkb_mod_mask_t locked, xkb_layout_index_t group, bool bare) {
        auto* state = xkb_state_new(keymap);
        if (!state)
            return std::nullopt;
        xkb_mod_mask_t lockMods = 0;
        for (const char* name : {XKB_MOD_NAME_CAPS, XKB_MOD_NAME_NUM}) {
            if (const auto INDEX = xkb_keymap_mod_get_index(keymap, name); INDEX != XKB_MOD_INVALID)
                lockMods |= 1u << INDEX;
        }
        std::optional<std::pair<uint32_t, xkb_mod_mask_t>> best;
        // X keycodes stop at 255, and the first eight are not keys.
        const auto LAST = std::min<xkb_keycode_t>(xkb_keymap_max_keycode(keymap), 255);
        for (xkb_keycode_t keycode = std::max<xkb_keycode_t>(xkb_keymap_min_keycode(keymap), 9); keycode <= LAST; ++keycode) {
            xkb_state_update_mask(state, 0, 0, locked, 0, 0, group);
            const auto LAYOUT = xkb_state_key_get_layout(state, keycode);
            if (LAYOUT == XKB_LAYOUT_INVALID)
                continue;
            for (xkb_level_index_t level = 0; level < xkb_keymap_num_levels_for_key(keymap, keycode, LAYOUT); ++level) {
                xkb_mod_mask_t masks[16];
                const auto     COUNT = xkb_keymap_key_get_mods_for_level(keymap, keycode, LAYOUT, level, masks, 16);
                for (size_t i = 0; i < COUNT; ++i) {
                    if ((masks[i] & lockMods) || (bare && masks[i]) || (best && std::popcount(masks[i]) >= std::popcount(best->second)))
                        continue;
                    xkb_state_update_mask(state, masks[i], 0, locked, 0, 0, group);
                    if (xkb_state_key_get_one_sym(state, keycode) == keysym)
                        best = {{keycode - 8, masks[i]}};
                }
            }
        }
        xkb_state_unref(state);
        return best;
    }

    // The key and modifiers that type `keysym`: under the user's locks if any key gives it there, else with the locks
    // off for its keystroke, as Caps Lock makes French é a capital and its shifted level a digit. A chord's key (`bare`)
    // is the one that gives it with no modifiers or locks.
    std::optional<std::pair<uint32_t, SMods>> keyFor(xkb_keymap* keymap, xkb_keysym_t keysym, xkb_mod_mask_t locked, xkb_layout_index_t group, bool bare) {
        if (keysym == XKB_KEY_NoSymbol)
            return std::nullopt;
        for (const xkb_mod_mask_t LOCKED : {bare ? 0 : locked, 0u}) {
            if (const auto KEY = keyUnder(keymap, keysym, LOCKED, group, bare))
                return std::pair{KEY->first, SMods{.depressed = KEY->second, .locked = LOCKED}};
        }
        return std::nullopt;
    }

    // Characters become the keys that type them on the keymap clients have now, the active keyboard's, so the text
    // arrives as written whatever the user's layout and Caps Lock. A character no key gives is refused, not skipped.
    std::optional<std::string> resolveKeys(KeyEvents& events, const SP<IKeyboard>& keyboard) {
        std::map<std::pair<uint32_t, bool>, std::pair<uint32_t, SMods>> found;
        for (auto& event : events) {
            if (event.kind == SKeyEvent::CODE)
                continue;
            const bool BARE = event.kind == SKeyEvent::KEY_OF;
            auto       it   = found.find({event.code, BARE});
            if (it == found.end()) {
                const auto KEY = keyFor(keyboard->m_xkbKeymap, keysymOf(event.code), keyboard->m_modifiersState.locked, keyboard->m_modifiersState.group, BARE);
                if (!KEY)
                    return std::format("no key on the active keymap gives U+{:04X}{}", event.code, BARE ? " without modifiers" : "");
                it = found.emplace(std::pair{event.code, BARE}, *KEY).first;
            }
            event.code = it->second.first;
            event.kind = SKeyEvent::CODE;
            if (!BARE)
                event.mods = it->second.second;
        }
        return std::nullopt;
    }

    std::optional<std::string> startKeys(SKeyOp op, uint64_t ticket) {
        const auto WINDOW = op.window.lock();
        if (!WINDOW || !WINDOW->mapped())
            return "the window closed before its keys were sent";
        const auto KEYBOARD = g_pSeatManager->m_keyboard.lock();
        if (!KEYBOARD || !KEYBOARD->m_xkbKeymap)
            return "no keyboard";
        if (const auto ERROR = resolveKeys(op.events, KEYBOARD))
            return ERROR;
        op.ticket = ticket;
        op.x11    = WINDOW->backend().isX11();
        op.xkb    = scratchState(KEYBOARD);
        op.locked = KEYBOARD->m_modifiersState.locked;
        g_keyOp.emplace(std::move(op));
        g_keyOp->timer = makeShared<CEventLoopTimer>(ms(1), keyTick, nullptr);
        g_pEventLoopManager->addTimer(g_keyOp->timer);
        return std::nullopt;
    }

    std::optional<uint32_t> codePoint(std::string_view hex) {
        uint32_t value = 0;
        const auto [END, ERROR] = std::from_chars(hex.data(), hex.data() + hex.size(), value, 16);
        if (hex.empty() || ERROR != std::errc{} || END != hex.data() + hex.size() || value == 0 || value > 0x10FFFF)
            return std::nullopt;
        return value;
    }

    // Key event words from `first` on, appended to `events`: `+code` and `-code` press and release an evdev key code,
    // `pHEX` and `rHEX` the key that gives character U+HEX with no modifiers, and `tHEX` types U+HEX as one keystroke. A
    // word that starts with `-` and is not a number would be a hyprctl flag, so characters take letters.
    std::optional<std::string> parseKeys(const std::vector<std::string>& args, size_t first, KeyEvents& events) {
        for (size_t i = first; i < args.size(); ++i) {
            const std::string_view WORD = args[i];
            const auto             BAD  = "bad key event '" + args[i] + "'";
            if (WORD[0] == 't') {
                const auto CHAR = codePoint(WORD.substr(1));
                if (!CHAR)
                    return BAD;
                events.push_back({.kind = SKeyEvent::TYPED, .code = *CHAR, .press = true});
                events.push_back({.kind = SKeyEvent::TYPED, .code = *CHAR, .press = false});
                continue;
            }
            if (WORD[0] == 'p' || WORD[0] == 'r') {
                const auto CHAR = codePoint(WORD.substr(1));
                if (!CHAR)
                    return BAD;
                events.push_back({.kind = SKeyEvent::KEY_OF, .code = *CHAR, .press = WORD[0] == 'p'});
                continue;
            }
            if (WORD[0] != '+' && WORD[0] != '-')
                return BAD;
            const bool PRESS = WORD[0] == '+';
            const auto CODE = number(std::string(WORD.substr(1)));
            if (!CODE || *CODE < 1 || *CODE > 247)
                return BAD;
            events.push_back({.code = static_cast<uint32_t>(*CODE), .press = PRESS});
        }
        return std::nullopt;
    }

    IPC::Socket1::SResponse stageCommand(const std::vector<std::string>& args) {
        if (args.size() < 4)
            return refuse("usage: deskpilot stage <txn> <key event>...");
        auto it = g_staged.find(args[2]);
        if (it == g_staged.end()) {
            if (g_staged.size() >= MAX_STAGED)
                g_staged.erase(std::ranges::min_element(g_staged, {}, [](const auto& entry) { return entry.second.order; }));
            it = g_staged.emplace(args[2], SStaged{.order = g_stageOrder++}).first;
        }
        if (const auto ERROR = parseKeys(args, 3, it->second.events)) {
            g_staged.erase(it);
            return refuse(*ERROR);
        }
        return IPC::Socket1::SResponse(okReply());
    }

    // How much text one paste may carry, read from the file its request names.
    constexpr size_t MAX_PASTE = 16 << 20;

    // Reads the text a paste request names into `text`: a file the caller wrote, removed once read so it never
    // outlives the request.
    std::optional<std::string> readPaste(const std::string& path, std::string& text) {
        std::ifstream file(path, std::ios::binary);
        if (file) {
            text.resize(MAX_PASTE + 1);
            file.read(text.data(), text.size());
            text.resize(file.gcount());
        }
        const bool READ = file.good() || file.eof();
        std::remove(path.c_str());
        if (!READ)
            return "cannot read the paste text from " + path;
        if (text.size() > MAX_PASTE)
            return std::format("a paste may carry at most {} bytes", MAX_PASTE);
        return std::nullopt;
    }

    // `keys <window> <hold> <gap> <txn|none> <event>...`, or `paste <window> <file> ...` with the same words after the
    // file, which offers the file's text as the window's clipboard just before the keys (its paste chord) are sent.
    IPC::Socket1::SResponse keysCommand(const std::vector<std::string>& args) {
        const bool   PASTE = args[1] == "paste";
        const size_t AT    = PASTE ? 4 : 3; // where <hold-ms> is
        if (args.size() < AT + 3)
            return refuse(PASTE ? "usage: deskpilot paste <window> <file> <hold-ms> <gap-ms> <txn|none> <key event>..." :
                                  "usage: deskpilot keys <window> <hold-ms> <gap-ms> <txn|none> <key event>...");
        std::optional<std::string> text;
        if (PASTE) {
            if (const auto ERROR = readPaste(args[3], text.emplace()))
                return refuse(*ERROR);
        }
        const auto WINDOW = Desktop::viewState()->query().selector("address:" + args[2]).runWindow();
        const auto HOLD = number(args[AT]), GAP = number(args[AT + 1]);
        // The user's keys wait out each hold, so neither may be longer than a key would be held by hand.
        if (!HOLD || !GAP || *HOLD < 0 || *GAP < 0 || *HOLD > 10000 || *GAP > 10000)
            return refuse("hold and gap must be 0 to 10000 milliseconds");

        SKeyOp op;
        if (args[AT + 2] != "none") {
            auto staged = g_staged.find(args[AT + 2]);
            if (staged == g_staged.end())
                return refuse("no staged key transaction " + args[AT + 2]);
            op.events = std::move(staged->second.events);
            g_staged.erase(staged);
        }
        if (const auto ERROR = parseKeys(args, AT + 3, op.events))
            return refuse(*ERROR);
        if (op.events.empty())
            return refuse("no key events");
        if (!WINDOW || !WINDOW->mapped())
            return refuse("no mapped window " + args[2]);
        if (PASTE && WINDOW->backend().isX11())
            return refuse("an XWayland window reads the clipboard every X client shares, so a paste would replace the user's clipboard: type the text instead");
        op.window = WINDOW;
        op.hold   = ms(static_cast<int>(*HOLD));
        op.gap    = ms(static_cast<int>(*GAP));
        return enqueue([op = std::move(op), text = std::move(text)](uint64_t ticket) mutable -> std::optional<std::string> {
            if (text) {
                if (const auto ERROR = offerPaste(op.window.lock(), std::move(*text)))
                    return ERROR;
            }
            const auto ERROR = startKeys(std::move(op), ticket);
            if (ERROR && text)
                restorePaste();
            return ERROR;
        });
    }

    IPC::Socket1::SResponse pointerCommand(const std::vector<std::string>& args, const std::string& command) {
        const bool CLICK = args[1] == "click";
        const auto WINDOW = address(args[2]);
        const auto X = number(args[3]), Y = number(args[4]), A = number(args[5]);
        if (!WINDOW || !X || !Y || !A)
            return refuse("bad arguments: " + command);

        SOp op;
        op.window = *WINDOW;
        op.target = {*X, *Y};
        if (CLICK) {
            const auto COUNT = number(args[6]);
            if (*A < 272 || *A > 274 || !COUNT || *COUNT < 1 || *COUNT > 3)
                return refuse("button must be 272-274 and count 1-3");
            op.button = static_cast<uint32_t>(*A);
            op.count  = static_cast<int>(*COUNT);
        } else {
            if (*A == 0 || std::abs(*A) > 50)
                return refuse("dy must be a non-zero number of notches up to 50");
            op.scroll = static_cast<int>(*A);
        }
        return enqueue([op = std::move(op)](uint64_t ticket) mutable { return startPointer(std::move(op), ticket); });
    }

    IPC::Socket1::SResponse onCommand(const IPC::Socket1::SRequest& req) {
        std::istringstream       in(req.command);
        std::vector<std::string> args;
        for (std::string word; in >> word;)
            args.push_back(word);
        // args[0] is "deskpilot".
        const std::string VERB = args.size() >= 2 ? args[1] : "";
        if (VERB == "status")
            return IPC::Socket1::SResponse(std::format(R"({{"ok":true,"version":{},"path":{},"busy":{},"queued":{}}})", jsonString(VERSION), jsonString(ownPath()),
                                                       g_op || g_keyOp ? "true" : "false", g_queue.size()));
        if (VERB == "wait") {
            const auto TICKET = args.size() == 3 ? number(args[2]) : std::nullopt;
            if (!TICKET || *TICKET < 1)
                return refuse("usage: deskpilot wait <ticket>");
            return awaitTicket(static_cast<uint64_t>(*TICKET));
        }
        if (VERB == "stage")
            return stageCommand(args);
        if (VERB == "keys" || VERB == "paste")
            return keysCommand(args);
        if ((VERB == "click" && args.size() == 7) || (VERB == "scroll" && args.size() == 6))
            return pointerCommand(args, req.command);
        return refuse("usage: deskpilot click <window> <x> <y> <button> <count> | scroll <window> <x> <y> <dy> | keys <window> <hold-ms> <gap-ms> <txn|none> "
                      "<key event>... | paste <window> <file> <hold-ms> <gap-ms> <txn|none> <key event>... | stage <txn> <key event>... | wait <ticket> | status; a key "
                      "event is +code, -code, pHEX, rHEX or tHEX");
    }
}

// `owner::name(` in the demangled signature, so `CWindow::activate` does not also match `activateGroupMembers`.
static CFunctionHook* hook(HANDLE handle, const std::string& owner, const std::string& name, void* replacement) {
    for (const auto& fn : HyprlandAPI::findFunctionsByName(handle, name)) {
        if (fn.demangled.contains(owner + "::" + name + "(")) {
            const auto HOOK = HyprlandAPI::createFunctionHook(handle, fn.address, replacement);
            if (HOOK && HOOK->hook())
                return HOOK;
            break;
        }
    }
    throw std::runtime_error("deskpilot: could not hook " + owner + "::" + name);
}

APICALL EXPORT std::string PLUGIN_API_VERSION() {
    return HYPRLAND_API_VERSION;
}

APICALL EXPORT PLUGIN_DESCRIPTION_INFO PLUGIN_INIT(HANDLE handle) {
    PHANDLE = handle;
    // Hyprland's loader only checks the API string; a build against other headers would misread its objects.
    if (std::string(__hyprland_api_get_hash()) != __hyprland_api_get_client_hash()) {
        HyprlandAPI::addNotification(PHANDLE, "[deskpilot] built for another Hyprland version: run `deskpilot plugin load`", CHyprColor{1.0, 0.2, 0.2, 1.0}, 5000);
        throw std::runtime_error("deskpilot: built for another Hyprland version, rebuild it with `deskpilot plugin build`");
    }

    g_moveHook     = hook(handle, "CInputManager", "onMouseMoved", reinterpret_cast<void*>(&hkOnMouseMoved));
    g_buttonHook   = hook(handle, "CInputManager", "onMouseButton", reinterpret_cast<void*>(&hkOnMouseButton));
    g_wheelHook    = hook(handle, "CInputManager", "onMouseWheel", reinterpret_cast<void*>(&hkOnMouseWheel));
    g_keyHook      = hook(handle, "CInputManager", "onKeyboardKey", reinterpret_cast<void*>(&hkOnKeyboardKey));
    g_modHook      = hook(handle, "CInputManager", "onKeyboardMod", reinterpret_cast<void*>(&hkOnKeyboardMod));
    g_activateHook = hook(handle, "CWindow", "activate", reinterpret_cast<void*>(&hkActivate));
    g_focusHook    = hook(handle, "CSeatManager", "setKeyboardFocus", reinterpret_cast<void*>(&hkSetKeyboardFocus));

    HyprlandAPI::registerHyprCtlCommand(handle, IPC::Socket1::SCommand{.name = "deskpilot", .match = IPC::Socket1::COMMAND_MATCH_PREFIX, .handler = onCommand});
    return {"deskpilot", "Clicks, scrolls and keys for agents that leave the user's pointer and focus in place", "cjber", VERSION};
}

// The running action is ended and the user's pointer and focus put back; everything queued or waiting is answered.
APICALL EXPORT void PLUGIN_EXIT() {
    const auto UNLOADED = errorReply("the deskpilot plugin was unloaded");
    for (auto& queued : g_queue)
        complete(queued.ticket, UNLOADED);
    g_queue.clear();
    if (g_op) {
        g_op->error = "the deskpilot plugin was unloaded";
        finish();
    }
    if (g_keyOp) {
        if (g_keyOp->holding)
            restoreStroke(*g_keyOp);
        g_keyOp->error = "the deskpilot plugin was unloaded";
        finishKeys();
    }
    for (auto& [ticket, entry] : g_tickets) {
        if (!entry.waiter)
            continue;
        dropTimer(entry.waiter->timer);
        entry.waiter->resolver->resolve(UNLOADED);
    }
    g_tickets.clear();
    restorePaste();
    if (g_x)
        xcb_disconnect(g_x);
    g_x = nullptr;
}
