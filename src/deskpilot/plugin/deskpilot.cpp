// deskpilot Hyprland plugin: pointer and key input for agents that leaves the user's pointer and focus where they are.
//
// `hyprctl deskpilot path <window> <shown|plain|hidden> <step>...` runs one bounded pointer sequence on <window>. A step
// is mX,Y to move to global X,Y (the first step enters there), pBUTTON or rBUTTON to press or release, wNOTCHES to turn
// the wheel, dMS to wait, or n to answer the caller at once while the rest goes on. A path must release every button it
// presses. Clicks, scrolls, drags and hovers are all paths. `hyprctl deskpilot end <ticket>` ends a running one early.
//
// A window whose app bound the agent seat (see "agent seat") takes every path through that seat's pointer, in any mode,
// and nothing of the user's is held. Otherwise SHOWN and PLAIN run on the user's own seat, so every client receives
// them, XWayland and Wine included:
//
//   1. The user's pointer input is held through hooks on CInputManager's mouse and tablet handlers (onMouseMoved,
//      onMouseWarp, onMouseButton, onMouseWheel, onTabletAxis, onTabletProximity, onTabletTip and onTabletButton),
//      which see the device as well as the event: a replayed release must name the device that pressed, or Hyprland
//      drops it and the button stays held. Nothing the user does can move the pointer mid-action.
//   2. The cursor image is hidden, the pointer warps to the point, and the window under it must be <window>;
//      anything else on top refuses before any press is sent. Only the seat's pointer focus moves: Hyprland's
//      focus-follows-mouse and click-to-focus never run, so keyboard focus stays on the user's window and the
//      target is not raised.
//   3. Presses, releases and wheel notches go straight to the seat, as the end of Hyprland's own handlers does, so
//      compositor mouse binds and scroll factors never see them. They are spaced by event-loop timers, so the client
//      sees them as separate events. Before each press or wheel notch Hyprland's hit tester must still find <window>
//      at the point and the seat's pointer focus must still be on it, so a window, layer surface or popup that
//      appears mid-sequence never has the click passed on to it. Releases always go, so no button is left down.
//      While a button is down the surface it pressed keeps the pointer focus, as for the user's own held buttons; a
//      drag and drop the client starts is moved and dropped the way Hyprland's own listeners do it, without emitting
//      input events other listeners would act on.
//   4. The pointer goes back to where the user left it, keyboard focus goes back to the surface that had it if
//      anything moved it, the cursor's visibility is restored, and the held input is replayed in the order it came
//      through Hyprland's own handlers, so the user's motion keeps its acceleration and reaches pointer-locked games.
//
// SHOWN first glides the agent cursor, a marker the compositor draws, to the first point, then moves it with the action
// and shows each press (see agentcursor). Each agent named by `agent=NAME` after the verb has a cursor of its own.
// HIDDEN drives the window client's own pointer instead and touches neither the seat's pointer nor the user's input
// unless it holds a button, so the window may be off screen (see the hidden pointer section).
// `hyprctl deskpilot cursor [agent=NAME] <x> <y>` glides an agent cursor alone.
// `hyprctl deskpilot agent <name> [owner=PID]` places every window an agent launched on its hidden workspace, and
// `agent <name> stop` closes them (see agents).
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
// The seat's selection never changes, so the user's clipboard and every other client's stay as they are. After
// PASTE_WAIT, or at the next paste, the client gets the clipboard it would otherwise have: the user's if it has
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
#include <hyprland/src/desktop/rule/Engine.hpp>
#include <hyprland/src/desktop/rule/windowRule/WindowRule.hpp>
#include <hyprland/src/desktop/state/FocusState.hpp>
#include <hyprland/src/desktop/state/ViewHitTester.hpp>
#include <hyprland/src/desktop/view/LayerSurface.hpp>
#include <hyprland/src/desktop/view/Popup.hpp>
#include <hyprland/src/desktop/view/WLSurface.hpp>
#include <hyprland/src/desktop/state/ViewQuery.hpp>
#include <hyprland/src/desktop/state/ViewState.hpp>
#include <hyprland/src/desktop/view/window/Window.hpp>
#include <hyprland/src/desktop/view/window/X11Backend.hpp>
#include <hyprland/src/devices/IKeyboard.hpp>
#include <hyprland/src/devices/Tablet.hpp>
#include <hyprland/src/helpers/defer/Promise.hpp>
#include <hyprland/src/helpers/time/Time.hpp>
#include <hyprland/src/ipc/s1/S1.hpp>
#include <hyprland/src/managers/SeatManager.hpp>
#include <hyprland/src/managers/screenshare/ScreenshareManager.hpp>
#include <hyprland/src/protocols/CursorShape.hpp>
#include <hyprland/src/protocols/LayerShell.hpp>
#include <hyprland/src/protocols/PointerConstraints.hpp>
#include <hyprland/src/protocols/PointerGestures.hpp>
#include <hyprland/src/protocols/RelativePointer.hpp>
#include <hyprland/src/protocols/XDGShell.hpp>
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
#include <hyprland/src/render/Texture.hpp>
#include <hyprland/src/render/pass/RectPassElement.hpp>
#include <hyprland/src/render/pass/TexPassElement.hpp>
#include <hyprland/src/event/EventBus.hpp>
#include <hyprland/src/state/MonitorState.hpp>
#include <hyprland/src/xwayland/XSurface.hpp>

#include "seat.hpp"

#include <cairo/cairo.h>
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
#include <filesystem>
#include <csignal>
#include <fstream>
#include <linux/sockios.h>
#include <sys/ioctl.h>
#include <format>
#include <functional>
#include <map>
#include <ranges>
#include <optional>
#include <sstream>
#include <unistd.h>
#include <string>
#include <vector>

// The hash of this source, set by the Makefile, so `deskpilot plugin load` can tell a stale build from the current one.
#ifndef DESKPILOT_SOURCE
#define DESKPILOT_SOURCE "unknown"
#endif
// The agent seat library this build opens (see seat.hpp), set by the Makefile.
#ifndef DESKPILOT_SEAT
#define DESKPILOT_SEAT "deskpilot-seat.so"
#endif
constexpr const char* VERSION = DESKPILOT_SOURCE;

inline HANDLE PHANDLE = nullptr;

namespace {
    using ActivateFn = void (*)(Desktop::View::CWindow*, bool);
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

    // How long a path waits after moving the pointer: about a frame, so a client that reads input once a frame still
    // sees the pointer go through each point.
    constexpr auto MOVE_GAP = ms(8);
    // A path's bounds, since the user's own pointer input is held while it runs.
    constexpr int  MAX_MOVES   = 500;
    constexpr int  MAX_PRESSES = 20;
    constexpr int  MAX_NOTCHES = 50;
    constexpr auto MAX_DWELL   = ms(10000);

    // One step of a pointer action and how long to wait after it before the next.
    struct SStep {
        std::function<void()> run;
        ms                    wait{0};
    };

    // What an action shows. SHOWN and PLAIN act on a window on screen, SHOWN with the agent cursor gliding there first;
    // HIDDEN acts on a window wherever it is and shows nothing. How the input gets there is SOp::direct.
    enum class eMode : uint8_t {
        SHOWN,
        PLAIN,
        HIDDEN,
    };

    struct SOp {
        uintptr_t                          window = 0;
        eMode                              mode   = eMode::SHOWN;
        std::string                        agent;  // whose agent cursor shows the action, "" for an unnamed agent
        Vector2D                           start;  // the first point, where the agent cursor glides before the action
        Vector2D                           target; // where the pointer is: the latest point of the action
        Vector2D                           saved;
        bool                               hadFocus = false;
        PHLWINDOWREF                       focusWindow;
        WP<CWLSurfaceResource>             focusSurface;
        std::vector<std::function<void()>> heldInput;
        std::vector<SStep>                 steps;
        size_t                             next = 0;
        std::vector<uint32_t>              down; // buttons this action holds down, released when it ends however it ends
        bool                               answered = false; // a path's `n` step has answered its caller already
        // The input goes to the window client's own pointer (see "hidden pointer" below), leaving the user's pointer,
        // focus and input alone. Otherwise the seat's pointer is moved onto the window and the user's input is held:
        // for an XWayland window, which reads the one X pointer, and for a window of the app the user's pointer is on.
        bool                               direct = false;
        // The window's client bound the agent seat, so the input goes to its agent pointer (see "agent seat").
        bool                               agentSeat = false;
        // A direct action: the surface its pointer is on and the pointers entered there, the client's own wl_pointer
        // resources or its agent pointers.
        WP<CWLSurfaceResource>             surface;
        Vector2D                           local;
        std::vector<WP<CWLPointerResource>> pointers;
        std::vector<WP<CWlPointer>>        agents;
        CHyprSignalListener                focusChange;
        SP<CEventLoopTimer>                timer;
        uint64_t                           ticket = 0;
        std::string                        error;
        // What kind of failure `error` is, a word a program reads: covered (another window or layer is over the point),
        // gone (the window or surface went away), unavailable (the plugin was unloaded) or refused (anything else).
        std::string                        code = "refused";
    };

    CFunctionHook*     g_moveHook     = nullptr;
    CFunctionHook*     g_warpHook     = nullptr;
    CFunctionHook*     g_buttonHook   = nullptr;
    CFunctionHook*     g_wheelHook    = nullptr;
    CFunctionHook*     g_tabletAxis   = nullptr;
    CFunctionHook*     g_tabletNear   = nullptr;
    CFunctionHook*     g_tabletTip    = nullptr;
    CFunctionHook*     g_tabletButton = nullptr;
    CFunctionHook*     g_keyHook      = nullptr;
    CFunctionHook*     g_modHook      = nullptr;
    CFunctionHook*     g_activateHook = nullptr;
    CFunctionHook*     g_focusHook    = nullptr;
    std::optional<SOp> g_op;
    bool               g_gliding = false; // a SHOWN action waits for the agent cursor to land

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
        bool                               inStroke = false;
        // The user's input is held until the stroke ends: only when their keys could reach the stroke's keyboards,
        // since their keyboard focus is in the window's app, or the window is an XWayland one, which X focus reaches.
        bool                               holding = false;
        // The window stays entered between strokes when nothing is held, so the next stroke needs no enter. A leave and
        // enter around each keystroke makes an app such as Chrome redraw itself unfocused and focused every time,
        // which flickers its title bar. It is left when the sequence ends, or when the user focuses its app.
        bool                               lingering = false;
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
        std::string                        code = "refused"; // see SOp::code
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

    // The user's pointer input during an action: each event is held while a pointer action moves the seat's pointer
    // or a keystroke holds the user's input, and replayed after it in the order it came, through Hyprland's
    // own handler, so acceleration, relative motion for pointer-locked games and absolute devices work as they would.
    // A held event is replayed with the time it is replayed at, so it never reaches a client dated before the
    // agent's own events that went ahead of it.
    template <CFunctionHook*& HOOK, typename EVENT, typename... REST>
    void holdPointer(CInputManager* self, EVENT event, REST... rest) {
        auto run    = [self, rest...](EVENT e) { reinterpret_cast<void (*)(CInputManager*, EVENT, REST...)>(HOOK->m_original)(self, e, rest...); };
        auto replay = [run, event]() mutable {
            event.timeMs = now();
            run(event);
        };
        if (g_op && !g_op->direct) {
            g_op->heldInput.emplace_back(replay);
            return;
        }
        if (!holdForStroke(replay))
            run(event);
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

    // A client asks for an xdg-activation token (or a pointer warp) with the serial of an input event, and Hyprland
    // grants it if the seat recorded that serial. A client such as Chrome asks after every click and activates itself
    // once its own reply comes back, after the action has ended, so dropping its activation mid-action is not enough.
    // Input deskpilot sends carries serials Hyprland never records: the client handles the event as usual, but its
    // serial earns no activation or warp. Drags and moves are unaffected, since they check the separate record of
    // button serials. The user's own input replayed after an action is theirs and keeps recorded serials.
    using NextSerialFn         = uint32_t (*)(CSeatManager*, SP<CWLSeatResource>, bool);
    CFunctionHook* g_serialHook = nullptr;
    bool           g_agentInput = false;

    // Marks the input sent in a scope as deskpilot's (or, with false, as the user's), restoring the outer scope's.
    struct SInputOwner {
        bool outer;
        explicit SInputOwner(bool agent = true) : outer(std::exchange(g_agentInput, agent)) {}
        ~SInputOwner() { g_agentInput = outer; }
        SInputOwner(const SInputOwner&)            = delete;
        SInputOwner& operator=(const SInputOwner&) = delete;
    };

    // Whether the latest input event each client got was deskpilot's. A client keeps its entry after it disconnects; a
    // new client at its address overwrites it with its first event.
    std::map<wl_client*, bool> g_agentLast;

    uint32_t hkNextSerial(CSeatManager* self, SP<CWLSeatResource> seat, bool enter) {
        if (seat)
            g_agentLast[seat->client()] = g_agentInput;
        if (g_agentInput && seat)
            return wl_display_next_serial(g_pCompositor->m_wlDisplay);
        return reinterpret_cast<NextSerialFn>(g_serialHook->m_original)(self, seat, enter);
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
    //
    // Each change also sends every client's keyboard a leave. Mid-stroke, a change between other apps' surfaces goes
    // ahead without the stroke's keyboards getting it (g_shieldKeys); one into the window's own app waits, since that
    // app's keyboard is in use.
    bool g_shieldKeys = false;

    void endLinger(SKeyOp& op);

    void hkSetKeyboardFocus(CSeatManager* self, SP<CWLSurfaceResource> surface) {
        // Hyprland enters the surface it focuses itself, so a window still entered between strokes is left first.
        if (g_keyOp && g_keyOp->lingering) {
            const auto TARGET = g_keyOp->surface.lock();
            if (!TARGET || (surface && surface->client() == TARGET->client()))
                endLinger(*g_keyOp);
        }
        if (g_keyOp && g_keyOp->inStroke) {
            const auto TARGET = g_keyOp->surface.lock();
            if (g_keyOp->holding || (surface && TARGET && surface->client() == TARGET->client())) {
                g_keyOp->deferredFocus = surface;
                return;
            }
            g_shieldKeys = true;
        }
        reinterpret_cast<FocusFn>(g_focusHook->m_original)(self, surface);
        g_shieldKeys = false;
    }

    using KeyboardLeaveFn = void (*)(CWLKeyboardResource*);
    CFunctionHook* g_keyboardLeaveHook = nullptr;

    void hkKeyboardSendLeave(CWLKeyboardResource* self) {
        if (g_shieldKeys && g_keyOp && std::ranges::any_of(g_keyOp->keyboards, [self](const auto& keyboard) { return keyboard.get() == self; }))
            return;
        reinterpret_cast<KeyboardLeaveFn>(g_keyboardLeaveHook->m_original)(self);
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

    // `code` is the kind of failure for a program to branch on; see SOp::code. A request the plugin turns down before it
    // runs is refused, and busy when only the queue is full.
    std::string errorReply(const std::string& why, const std::string& code = "refused") {
        return std::format(R"({{"ok":false,"error":{},"code":{}}})", jsonString(why), jsonString(code));
    }

    IPC::Socket1::SResponse refuse(const std::string& why, const std::string& code = "refused") {
        return IPC::Socket1::SResponse(errorReply(why, code));
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
            return refuse(std::format("the deskpilot queue is full: {} actions are waiting behind the running one, try again when they finish", g_queue.size()), "busy");
        const auto TICKET = g_nextTicket++;
        g_tickets[TICKET] = {};
        g_queue.push_back({TICKET, std::move(start)});
        startNext();
        return awaitTicket(TICKET);
    }

    void startNext() {
        while (!g_op && !g_keyOp && !g_gliding && !g_queue.empty()) {
            auto queued = std::move(g_queue.front());
            g_queue.pop_front();
            if (const auto ERROR = queued.start(queued.ticket))
                complete(queued.ticket, errorReply(*ERROR));
        }
    }

    // ---- agent cursor ----
    //
    // A cursor the compositor draws where deskpilot acts, so whoever watches the screen sees what the agent does while
    // the user's own pointer stays where they left it: a flight-path marker (a ring with wings and a fin, as on a
    // head-up display) centred on the point. Each agent (DESKPILOT_AGENT) has its own, in a colour of its own with its
    // name on a tag beside it; any number can show at once. They are drawn at Hyprland's last render stage, above every
    // window and layer, and window captures (grim -T) never include them. Before a visible pointer action an agent's
    // cursor glides to the first point along an arc, fast in the middle and slow at the ends, while the user's input
    // still runs; the action starts once it lands, as lock-on brackets close in around it. It then follows each move
    // of the action, shrinks while a button is down, and sends a ring out at each press. It fades out after IDLE_HIDE
    // with nothing to show, and is forgotten once faded.
    namespace agentcursor {
        constexpr double RING        = 5.5;  // the marker's ring radius in logical pixels
        constexpr double WING        = 13;   // from the centre to a wing tip
        constexpr double FIN         = 10.5; // from the centre to the fin's tip
        constexpr double PEAK_SPEED  = 900;  // logical pixels a second at the middle of a glide, as cua-driver's
        constexpr auto   MIN_GLIDE   = ms(120);
        constexpr auto   MAX_GLIDE   = ms(700);
        constexpr double ARC         = 0.2; // how far a glide bows out to the side, as a fraction of its length
        constexpr auto   PULSE       = ms(380);
        constexpr double PULSE_SIZE  = 30;
        constexpr double PRESSED     = 0.85; // the marker's scale while a button is down
        constexpr auto   LOCK        = ms(160);
        constexpr double LOCK_FROM   = 30; // the brackets' half-width as the cursor lands, closing to LOCK_TO
        constexpr double LOCK_TO     = 15;
        constexpr double BRACKET     = 6;   // the length of a bracket's arm
        constexpr double TAG_GAP     = LOCK_TO + 6; // from the centre to the tag's left edge, clear of the brackets
        constexpr double TAG_FONT    = 10;
        constexpr double TAG_HEIGHT  = 18;
        constexpr size_t NAME_MAX    = 32;
        constexpr auto   IDLE_HIDE   = std::chrono::seconds(8);
        constexpr auto   FADE        = ms(400);
        constexpr auto   FRAME       = ms(8);
        // The cursor of an unnamed agent: the colour cua-driver gives its default cursor, and no tag.
        const CHyprColor DEFAULT{94 / 255.0, 192 / 255.0, 232 / 255.0, 1.0};
        constexpr double DEFAULT_HUE = 197 / 360.0; // DEFAULT's hue, which named cursors keep apart from

        struct SGlide {
            Vector2D              from, bend, to;
            Time::steady_tp       start;
            double                seconds = 0;
            std::function<void()> then;
        };

        struct STexture {
            SP<Render::ITexture> texture;
            double               scale = 0;
        };

        struct SCursor {
            CHyprColor           color;
            double               hue = DEFAULT_HUE;
            bool                 placed = false; // it has a position to glide from
            Vector2D             pos;
            bool                 pressed = false;
            Time::steady_tp      active; // the last time it moved or pressed
            Time::steady_tp      landed; // when its last glide ended, for the brackets closing in
            std::optional<SGlide> glide;
            std::vector<std::pair<Vector2D, Time::steady_tp>> pulses;
            STexture             marker, tag;
            double               tagWidth = 0; // logical pixels; 0 for no tag
            CBox                 drawn;        // the global box last drawn, damaged again when it moves or goes
        };

        struct SState {
            std::map<std::string, SCursor> cursors; // by agent name, "" for an unnamed agent
            SP<CEventLoopTimer>            timer;
            CHyprSignalListener            render;
        };
        std::unique_ptr<SState> g_state;

        double seconds(Time::steady_tp since) {
            return std::chrono::duration<double>(Time::steadyNow() - since).count();
        }

        double secondsOf(auto duration) {
            return std::chrono::duration<double>(duration).count();
        }

        // Whether `name` may name an agent: it is shown on the tag and travels as one word of a request.
        bool validName(const std::string& name) {
            return name.size() <= NAME_MAX && std::ranges::all_of(name, [](char c) { return std::isalnum(static_cast<unsigned char>(c)) || c == '-' || c == '_' || c == '.'; });
        }

        // A colour at one lightness and saturation, which reads on light and dark backgrounds alike.
        CHyprColor hueColor(double hue) {
            const double L = 0.6, S = 0.8, C = (1 - std::abs(2 * L - 1)) * S;
            const auto   channel = [&](double n) {
                const double K = std::fmod(n + hue * 12, 12);
                return L - C / 2 * std::max(-1.0, std::min({K - 3, 9 - K, 1.0}));
            };
            return CHyprColor{channel(0), channel(8), channel(4), 1.0};
        }

        double hueDistance(double a, double b) {
            const auto D = std::abs(a - b);
            return std::min(D, 1 - D);
        }

        // A new cursor's hue: from its name's FNV-1a hash, so an agent alone on screen keeps its colour from run to
        // run; stepped on by the golden angle while it is within MIN_HUE_GAP of a cursor already showing, so cursors
        // on screen together stay apart. Past MAX_HUES cursors no hue is that far from all of them, and it takes the
        // last step.
        constexpr double MIN_HUE_GAP = 0.08;
        constexpr int    MAX_HUES    = 12;
        double hueOf(const std::string& name) {
            uint32_t hash = 2166136261u;
            for (const unsigned char c : name)
                hash = (hash ^ c) * 16777619u;
            double hue = (hash % 3600) / 3600.0;
            for (int step = 0; step < MAX_HUES; ++step) {
                const bool CLEAR = std::ranges::all_of(g_state->cursors, [&](const auto& entry) { return entry.first == name || hueDistance(entry.second.hue, hue) >= MIN_HUE_GAP; });
                if (CLEAR)
                    break;
                hue = std::fmod(hue + 0.381966, 1.0);
            }
            return hue;
        }

        void tagFont(cairo_t* cr, double scale) {
            cairo_select_font_face(cr, "monospace", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
            cairo_set_font_size(cr, TAG_FONT * scale);
        }

        // The tag's width in logical pixels: the name, a colour dot before it and padding.
        double tagWidthOf(const std::string& name) {
            if (name.empty())
                return 0;
            auto* surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, 1, 1);
            auto* cr      = cairo_create(surface);
            tagFont(cr, 1);
            cairo_text_extents_t extents;
            cairo_text_extents(cr, name.c_str(), &extents);
            cairo_destroy(cr);
            cairo_surface_destroy(surface);
            return std::ceil(extents.x_advance) + 26;
        }

        SCursor& cursorOf(const std::string& name) {
            auto [it, made] = g_state->cursors.try_emplace(name);
            if (made) {
                if (!name.empty()) {
                    it->second.hue = hueOf(name);
                    it->second.color = hueColor(it->second.hue);
                } else
                    it->second.color = DEFAULT;
                it->second.tagWidth = tagWidthOf(name);
            }
            return it->second;
        }

        // A held button keeps its cursor in view however long it is held.
        double alpha(const SCursor& cursor) {
            if (cursor.pressed)
                return 1;
            const auto IDLE = seconds(cursor.active) - secondsOf(IDLE_HIDE);
            return std::clamp(1.0 - IDLE / secondsOf(FADE), 0.0, 1.0);
        }

        // How far the brackets have closed in: 0 in flight, to 1 a moment after landing.
        double lock(const SCursor& cursor) {
            if (cursor.glide)
                return 0;
            const auto T = std::clamp(seconds(cursor.landed) / secondsOf(LOCK), 0.0, 1.0);
            return 1 - std::pow(1 - T, 3);
        }

        SP<Render::ITexture> upload(cairo_surface_t* surface, cairo_t* cr, STexture& cache, double scale) {
            cairo_surface_flush(surface);
            cache.texture = g_pHyprRenderer->createTexture(surface);
            cache.scale   = scale;
            cairo_destroy(cr);
            cairo_surface_destroy(surface);
            return cache.texture;
        }

        // The marker for one monitor scale, centred in its texture: a soft shadow, a white edge so it shows on any
        // background, and the agent's colour.
        SP<Render::ITexture> marker(SCursor& cursor, double scale) {
            if (cursor.marker.texture && cursor.marker.scale == scale)
                return cursor.marker.texture;
            const int  PX      = std::ceil((2 * WING + 12) * scale);
            auto*      SURFACE = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, PX, PX);
            auto*      CR      = cairo_create(SURFACE);
            const auto SHAPE   = [&] {
                cairo_new_path(CR);
                cairo_arc(CR, 0, 0, RING, 0, 2 * M_PI);
                cairo_move_to(CR, -RING, 0);
                cairo_line_to(CR, -WING, 0);
                cairo_move_to(CR, RING, 0);
                cairo_line_to(CR, WING, 0);
                cairo_move_to(CR, 0, -RING);
                cairo_line_to(CR, 0, -FIN);
            };
            cairo_translate(CR, PX / 2.0, PX / 2.0);
            cairo_scale(CR, scale, scale);
            cairo_set_line_cap(CR, CAIRO_LINE_CAP_ROUND);
            // A shadow as widening, fading strokes a little below: a blur's look without a blur pass.
            cairo_save(CR);
            cairo_translate(CR, 0, 1.2);
            for (const auto [width, a] : {std::pair{9.0, 0.06}, {7.0, 0.1}, {5.5, 0.16}}) {
                SHAPE();
                cairo_set_line_width(CR, width);
                cairo_set_source_rgba(CR, 0, 0, 0, a);
                cairo_stroke(CR);
            }
            cairo_restore(CR);
            SHAPE();
            cairo_set_line_width(CR, 4.4);
            cairo_set_source_rgba(CR, 1, 1, 1, 1);
            cairo_stroke(CR);
            SHAPE();
            cairo_set_line_width(CR, 2.2);
            cairo_set_source_rgba(CR, cursor.color.r, cursor.color.g, cursor.color.b, 1);
            cairo_stroke(CR);
            cairo_arc(CR, 0, 0, 1.4, 0, 2 * M_PI);
            cairo_fill(CR);
            return upload(SURFACE, CR, cursor.marker, scale);
        }

        // The name tag for one monitor scale: a dark pill edged in the agent's colour, a dot of it, and the name in
        // monospace, legible on any background and in any hue.
        SP<Render::ITexture> tag(SCursor& cursor, const std::string& name, double scale) {
            if (cursor.tag.texture && cursor.tag.scale == scale)
                return cursor.tag.texture;
            const int  W       = std::ceil(cursor.tagWidth * scale) + 2, H = std::ceil(TAG_HEIGHT * scale) + 2;
            auto*      SURFACE = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, W, H);
            auto*      CR      = cairo_create(SURFACE);
            const auto R       = TAG_HEIGHT / 2;
            cairo_translate(CR, 1, 1);
            cairo_scale(CR, scale, scale);
            cairo_new_sub_path(CR);
            cairo_arc(CR, cursor.tagWidth - R, R, R, -M_PI / 2, M_PI / 2);
            cairo_arc(CR, R, R, R, M_PI / 2, 3 * M_PI / 2);
            cairo_close_path(CR);
            cairo_set_source_rgba(CR, 0.08, 0.09, 0.11, 0.86);
            cairo_fill_preserve(CR);
            cairo_set_source_rgba(CR, cursor.color.r, cursor.color.g, cursor.color.b, 0.9);
            cairo_set_line_width(CR, 1.2);
            cairo_stroke(CR);
            cairo_arc(CR, 10, R, 3.2, 0, 2 * M_PI);
            cairo_fill(CR);
            cairo_scale(CR, 1 / scale, 1 / scale);
            tagFont(CR, scale);
            cairo_set_source_rgba(CR, 0.95, 0.96, 0.98, 1);
            cairo_move_to(CR, 18 * scale, 12.6 * scale);
            cairo_show_text(CR, name.c_str());
            return upload(SURFACE, CR, cursor.tag, scale);
        }

        CBox unite(const CBox& a, const CBox& b) {
            const auto X1 = std::min(a.x, b.x), Y1 = std::min(a.y, b.y);
            return CBox{X1, Y1, std::max(a.x + a.w, b.x + b.w) - X1, std::max(a.y + a.h, b.y + b.h) - Y1};
        }

        // Everything a cursor draws now, in global logical coordinates: the marker, brackets, tag, and every press ring,
        // which stays where it was pressed while the cursor moves on.
        CBox extent(const SCursor& cursor) {
            const double REACH = std::max({PULSE_SIZE, LOCK_FROM + 1, WING + 6});
            const auto   around = [&](Vector2D at) { return CBox{at - Vector2D{REACH, REACH}, Vector2D{2 * REACH, 2 * REACH}}; };
            CBox         box    = around(cursor.pos);
            if (cursor.tagWidth > 0)
                box = unite(box, CBox{cursor.pos + Vector2D{TAG_GAP, -TAG_HEIGHT / 2}, Vector2D{cursor.tagWidth, TAG_HEIGHT}});
            for (const auto& [at, start] : cursor.pulses)
                box = unite(box, around(at));
            return box.expand(4);
        }

        void damage(SCursor& cursor) {
            if (!cursor.drawn.empty())
                g_pHyprRenderer->damageBox(cursor.drawn);
            cursor.drawn = cursor.placed && alpha(cursor) > 0 ? extent(cursor) : CBox{};
            if (!cursor.drawn.empty())
                g_pHyprRenderer->damageBox(cursor.drawn);
        }

        void rect(Render::CRenderContext& ctx, const PHLMONITOR& monitor, const CBox& global, const CHyprColor& color, double round) {
            CRectPassElement::SRectData data;
            data.box   = CBox{global.pos() - monitor->m_position, global.size()}.scale(monitor->m_scale).round();
            data.round = std::round(round * monitor->m_scale);
            data.color = color;
            g_pHyprRenderer->addPassElement(ctx, makeUnique<CRectPassElement>(data));
        }

        void drawOne(Render::CRenderContext& ctx, const PHLMONITOR& monitor, const std::string& name, SCursor& cursor) {
            const auto A = alpha(cursor);
            if (!cursor.placed || A <= 0 || CBox{monitor->m_position, monitor->m_size}.intersection(extent(cursor)).empty())
                return;
            const auto& COLOR = cursor.color;
            for (const auto& [at, start] : cursor.pulses) {
                const auto T      = std::clamp(seconds(start) / secondsOf(PULSE), 0.0, 1.0);
                const auto RADIUS = PULSE_SIZE * (0.3 + 0.7 * (1 - std::pow(1 - T, 3)));
                rect(ctx, monitor, CBox{at - Vector2D{RADIUS, RADIUS}, {2 * RADIUS, 2 * RADIUS}}, COLOR.modifyA(static_cast<float>(0.3 * (1 - T) * A)), RADIUS);
            }
            // Lock-on brackets: four corners that close in from LOCK_FROM to LOCK_TO as the cursor lands.
            const auto L    = lock(cursor);
            const auto HALF = LOCK_FROM + (LOCK_TO - LOCK_FROM) * L;
            const auto BA   = COLOR.modifyA(static_cast<float>((0.35 + 0.65 * L) * A));
            constexpr double T = 1.8;
            for (const int SX : {-1, 1}) {
                for (const int SY : {-1, 1}) {
                    const Vector2D CORNER = cursor.pos + Vector2D{SX * HALF, SY * HALF};
                    const double   HX     = SX < 0 ? CORNER.x - T / 2 : CORNER.x - BRACKET + T / 2;
                    const double   VY     = SY < 0 ? CORNER.y - T / 2 : CORNER.y - BRACKET + T / 2;
                    rect(ctx, monitor, CBox{HX, CORNER.y - T / 2, BRACKET, T}, BA, T / 2);
                    rect(ctx, monitor, CBox{CORNER.x - T / 2, VY, T, BRACKET}, BA, T / 2);
                }
            }
            const auto                   SCALE = cursor.pressed ? PRESSED : 1.0;
            CTexPassElement::SRenderData data;
            data.tex        = marker(cursor, monitor->m_scale);
            const auto SIZE = data.tex->m_size / monitor->m_scale * SCALE;
            data.box        = CBox{cursor.pos - monitor->m_position - SIZE / 2, SIZE}.scale(monitor->m_scale).round();
            data.a          = A;
            g_pHyprRenderer->addPassElement(ctx, makeUnique<CTexPassElement>(std::move(data)));
            if (cursor.tagWidth > 0) {
                CTexPassElement::SRenderData label;
                label.tex = tag(cursor, name, monitor->m_scale);
                label.box = CBox{cursor.pos + Vector2D{TAG_GAP, -TAG_HEIGHT / 2} - monitor->m_position - Vector2D{1, 1} / monitor->m_scale, label.tex->m_size / monitor->m_scale}
                                .scale(monitor->m_scale)
                                .round();
                label.a = A;
                g_pHyprRenderer->addPassElement(ctx, makeUnique<CTexPassElement>(std::move(label)));
            }
        }

        void draw(Render::CRenderContext& ctx, const PHLMONITOR& monitor) {
            for (auto& [name, cursor] : g_state->cursors)
                drawOne(ctx, monitor, name, cursor);
        }

        // A point on the glide's arc (a quadratic Bezier through a bend to one side), at an eased fraction of the way.
        Vector2D along(const SGlide& glide, double t) {
            const auto E = t < 0.5 ? 4 * t * t * t : 1 - std::pow(-2 * t + 2, 3) / 2;
            return glide.from * ((1 - E) * (1 - E)) + glide.bend * (2 * (1 - E) * E) + glide.to * (E * E);
        }

        // One animation frame for every cursor; the timer runs only while something moves, closes in, pulses or
        // fades. A cursor that has faded out is forgotten.
        void tick(SP<CEventLoopTimer>, void*) {
            if (!g_state)
                return;
            std::vector<std::function<void()>> landed;
            bool                               moving = false;
            auto                               untilFade = ms::max();
            for (auto it = g_state->cursors.begin(); it != g_state->cursors.end();) {
                auto& cursor = it->second;
                if (cursor.glide) {
                    const auto T  = std::min(seconds(cursor.glide->start) / cursor.glide->seconds, 1.0);
                    cursor.pos    = along(*cursor.glide, T);
                    cursor.active = Time::steadyNow();
                    if (T >= 1) {
                        landed.push_back(std::move(cursor.glide->then));
                        cursor.glide.reset();
                        cursor.landed = Time::steadyNow();
                    }
                }
                std::erase_if(cursor.pulses, [](const auto& pulse) { return seconds(pulse.second) >= secondsOf(PULSE); });
                damage(cursor);
                const bool VISIBLE = cursor.placed && alpha(cursor) > 0;
                if (!cursor.glide && cursor.pulses.empty() && !VISIBLE) {
                    it = g_state->cursors.erase(it);
                    continue;
                }
                const auto FADE_IN = std::chrono::duration_cast<ms>(cursor.active + IDLE_HIDE - Time::steadyNow());
                if (cursor.glide || !cursor.pulses.empty() || lock(cursor) < 1 || FADE_IN <= FRAME)
                    moving = true;
                else
                    untilFade = std::min(untilFade, FADE_IN);
                ++it;
            }
            for (const auto& monitor : State::monitorState()->monitors())
                monitor->scheduleFrame();
            // Waiting out the idle time needs no frames: the next one is when the first fade starts.
            if (moving)
                g_state->timer->updateTimeout(FRAME);
            else if (untilFade != ms::max())
                g_state->timer->updateTimeout(untilFade);
            for (auto& then : landed) {
                if (then)
                    then();
            }
        }

        void wake(SCursor& cursor) {
            cursor.active = Time::steadyNow();
            g_state->timer->updateTimeout(ms(1));
        }

        // Glides `name`'s cursor to `to` (global), then runs `then`. With no position yet it appears there at once.
        void glideTo(const std::string& name, const Vector2D& to, std::function<void()> then) {
            auto&      cursor = cursorOf(name);
            const auto FROM   = cursor.placed ? cursor.pos : to;
            const auto DIST   = FROM.distance(to);
            cursor.placed     = true;
            if (DIST < 2) {
                cursor.pos    = to;
                cursor.landed = Time::steadyNow();
                wake(cursor);
                then();
                return;
            }
            // A smooth start and stop at a peak of PEAK_SPEED take about 1.5 times the time at that speed.
            const auto SECONDS = std::clamp(1.5 * DIST / PEAK_SPEED, secondsOf(MIN_GLIDE), secondsOf(MAX_GLIDE));
            const auto DIR     = (to - FROM) / DIST;
            const auto BEND    = (FROM + to) / 2 + Vector2D{-DIR.y, DIR.x} * (DIST * ARC);
            cursor.glide       = SGlide{FROM, BEND, to, Time::steadyNow(), SECONDS, std::move(then)};
            wake(cursor);
        }

        void moveTo(const std::string& name, const Vector2D& to) {
            auto& cursor  = cursorOf(name);
            cursor.placed = true;
            cursor.pos    = to;
            wake(cursor);
        }

        void press(const std::string& name) {
            auto& cursor   = cursorOf(name);
            cursor.pressed = true;
            cursor.pulses.emplace_back(cursor.pos, Time::steadyNow());
            wake(cursor);
        }

        void release(const std::string& name) {
            auto& cursor   = cursorOf(name);
            cursor.pressed = false;
            wake(cursor);
        }

        // A glide still under way when the plugin unloads lands at once, so the action waiting on it still runs.
        void init() {
            g_state         = std::make_unique<SState>();
            g_state->timer  = makeShared<CEventLoopTimer>(std::nullopt, tick, nullptr);
            g_pEventLoopManager->addTimer(g_state->timer);
            g_state->render = Event::bus()->m_events.render.stage.listen([](const Event::SRenderStageEvent& event) {
                if (event.stage == RENDER_LAST_MOMENT && event.context && g_state)
                    draw(event.context->get(), event.monitor);
            });
        }

        void shutdown() {
            if (!g_state)
                return;
            g_state->render.reset();
            dropTimer(g_state->timer);
            std::vector<CBox> drawn;
            for (const auto& [name, cursor] : g_state->cursors)
                drawn.push_back(cursor.drawn);
            g_state.reset();
            for (const auto& box : drawn) {
                if (!box.empty())
                    g_pHyprRenderer->damageBox(box);
            }
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
        const auto VIEW      = HLSURFACE ? HLSURFACE->view() : nullptr;
        if (!VIEW)
            return nullptr;
        // A menu or tooltip the window opened is its own view, and belongs to the window.
        if (const auto POPUP = Desktop::View::CPopup::fromView(VIEW))
            return POPUP->windowOwner();
        return Desktop::View::CWindow::fromView(VIEW);
    }

    // What a pointer at `pos` lands on, asked of Hyprland's hit tester in the order mouseMoveUnified looks: input
    // method popups, focus-priority windows, layer popups, overlay and top layers (a top layer only above an exclusive
    // fullscreen window if it may be), then windows with the extents processMouseDownNormal uses. Nothing when it is
    // the target, else what is there instead. It only asks, so neither focus moves.
    struct SBlocker {
        std::string why;
        std::string code; // covered when something is over the point, else refused (see SOp::code)
    };
    std::optional<SBlocker> blockerAt(const Vector2D& pos, uintptr_t target) {
        using namespace Desktop::View;
        const auto AT = std::format("at {},{}", pos.x, pos.y);
        if (g_pSessionLockManager->isSessionLocked())
            return SBlocker{"the session is locked", "refused"};
        if (g_pInputManager->m_relay.popupFromCoords(pos))
            return SBlocker{"an input method popup is on top " + AT, "covered"};
        if (const auto W = Desktop::viewState()->hitTest().windowAt(pos, FOCUS_PRIORITY); W && reinterpret_cast<uintptr_t>(W.get()) != target)
            return SBlocker{describe(W) + " is on top " + AT, "covered"};

        const auto MONITOR = State::monitorState()->query().vec(pos).run();
        if (!MONITOR)
            return SBlocker{"no monitor " + AT, "refused"};
        Vector2D local;
        PHLLS    layer;
        if (Desktop::viewState()->hitTest().layerPopupSurfaceAt(pos, MONITOR, &local, &layer))
            return SBlocker{std::format("a popup of layer surface '{}' is on top {}", layer ? layer->m_namespace : "", AT), "covered"};
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
            return SBlocker{std::format("layer surface '{}' is on top {}", layer ? layer->m_namespace : "", AT), "covered"};
        }

        const auto WINDOW = Desktop::viewState()->hitTest().windowAt(pos, ALLOW_FLOATING | RESERVED_EXTENTS | INPUT_EXTENTS);
        if (WINDOW && reinterpret_cast<uintptr_t>(WINDOW.get()) == target)
            return std::nullopt;
        if (WINDOW)
            return SBlocker{describe(WINDOW) + " is on top " + AT, "covered"};
        return SBlocker{"no window " + AT, "refused"};
    }

    // Whether the target is what shows at the action's point, fresh from the hit tester. A hidden action acts beneath
    // whatever is on top, so it is always clear; an on-screen one never reaches a window the user sees covered there,
    // whichever way its input goes.
    bool clearAt() {
        if (g_op->mode == eMode::HIDDEN)
            return true;
        if (const auto BLOCKER = blockerAt(g_op->target, g_op->window)) {
            g_op->error = BLOCKER->why;
            g_op->code  = BLOCKER->code;
            return false;
        }
        return true;
    }

    // The target must be clear at the point and still hold the seat's pointer focus (where a press goes) before
    // anything is sent to it.
    bool targetUnderPointer() {
        if (!clearAt())
            return false;
        const auto UNDER = windowUnderPointer();
        if (UNDER && reinterpret_cast<uintptr_t>(UNDER.get()) == g_op->window)
            return true;
        g_op->error = std::format("the pointer focus is on {}, not the target, at {},{}", UNDER ? describe(UNDER) : "no window", g_op->target.x, g_op->target.y);
        g_op->code  = "covered";
        return false;
    }

    // `pos` in the coordinates of `surface`, as mouseMoveUnified computes them: from the surface's global box, in buffer
    // pixels for an XWayland window.
    std::optional<Vector2D> localOn(const SP<CWLSurfaceResource>& surface, const Vector2D& pos) {
        if (!surface)
            return std::nullopt;
        const auto HLSURFACE = Desktop::View::CWLSurface::fromResource(surface);
        if (!HLSURFACE)
            return std::nullopt;
        const auto BOX = HLSURFACE->getSurfaceBoxGlobal();
        if (!BOX)
            return std::nullopt;
        auto       local  = pos - BOX->pos();
        const auto WINDOW = Desktop::View::CWindow::fromView(HLSURFACE->view());
        if (WINDOW && WINDOW->backend().isX11())
            local = WINDOW->backend().surfaceLocalToBuffer(local);
        return local;
    }

    // A motion event at the pointer's position to the surface with pointer focus. Real input moves the pointer to a
    // point before pressing there, and some clients track the pointer from motion alone, not from the enter.
    void sendMotion() {
        const auto LOCAL = localOn(g_pSeatManager->m_state.pointerFocus.lock(), g_pInputManager->getMouseCoordsInternal());
        if (!LOCAL)
            return;
        g_pSeatManager->sendPointerMotion(now(), *LOCAL);
        g_pSeatManager->sendPointerFrame();
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

    // Defined with the private members they need, below.
    void dndMotion(const Vector2D& pos);
    void dndDrop();
    void hiddenEnter(SOp& op, const Vector2D& pos);
    void hiddenMove(SOp& op, const Vector2D& pos);
    bool hiddenReady(SOp& op);
    void hiddenButton(SOp& op, uint32_t button, wl_pointer_button_state state);
    void hiddenWheel(SOp& op, int sign);
    void hiddenLeave(SOp& op);
    void pumpFrames(const PHLWINDOW& window);
    PHLWINDOW windowOf(const SOp& op);
    std::vector<WP<CWlPointer>> agentPointersOf(wl_client* client);

    bool direct() {
        return g_op->direct;
    }

    // The pointer to the first point of an action: entered there, and moved there only once that is the target, so a
    // window covering the point never sees the motion of an action refused at it.
    void enterAt(const Vector2D& pos) {
        g_op->target = pos;
        if (g_op->mode == eMode::SHOWN)
            agentcursor::moveTo(g_op->agent, pos);
        if (direct()) {
            if (clearAt())
                hiddenEnter(*g_op, pos);
            return;
        }
        Pointer::mgr()->warpTo(pos);
        enterUnderPointer();
        if (targetUnderPointer())
            sendMotion();
    }

    // The pointer on to a later point. With a button down the surface it pressed keeps the pointer focus, as Hyprland
    // keeps it for the user's own held buttons, so a drag reaches the surface it started on wherever it goes. With none
    // down the pointer enters what is under it, which must be the target. A drag and drop the window started goes
    // the way Hyprland takes one.
    void moveTo(const Vector2D& pos) {
        g_op->target = pos;
        if (g_op->mode == eMode::SHOWN)
            agentcursor::moveTo(g_op->agent, pos);
        if (direct()) {
            if (!g_op->down.empty() || clearAt())
                hiddenMove(*g_op, pos);
            return;
        }
        Pointer::mgr()->warpTo(pos);
        if (PROTO::data->dndActive()) {
            dndMotion(pos);
            return;
        }
        if (g_op->down.empty()) {
            g_pInputManager->refocusPointer();
            if (!targetUnderPointer())
                return;
        }
        sendMotion();
    }

    // The seat call processMouseDownNormal ends with, without its refocus and raise before it.
    void press(uint32_t button) {
        if (direct() ? !(clearAt() && hiddenReady(*g_op)) : !targetUnderPointer())
            return;
        if (g_op->mode == eMode::SHOWN)
            agentcursor::press(g_op->agent);
        if (direct())
            hiddenButton(*g_op, button, WL_POINTER_BUTTON_STATE_PRESSED);
        else {
            g_pSeatManager->sendPointerButton(now(), button, WL_POINTER_BUTTON_STATE_PRESSED);
            g_pSeatManager->sendPointerFrame();
        }
        g_op->down.push_back(button);
    }

    void replayHeld(std::vector<std::function<void()>>& held) {
        SInputOwner user{false};
        auto        input = std::move(held);
        held.clear();
        for (auto& replay : input)
            replay();
    }

    // A release that ends a drag and drop drops it where the pointer is.
    void release(uint32_t button) {
        const auto IT = std::ranges::find(g_op->down, button);
        if (IT == g_op->down.end())
            return;
        g_op->down.erase(IT);
        if (g_op->mode == eMode::SHOWN && g_op->down.empty())
            agentcursor::release(g_op->agent);
        if (direct()) {
            hiddenButton(*g_op, button, WL_POINTER_BUTTON_STATE_RELEASED);
            return;
        }
        if (PROTO::data->dndActive()) {
            dndDrop();
            return;
        }
        g_pSeatManager->sendPointerButton(now(), button, WL_POINTER_BUTTON_STATE_RELEASED);
        g_pSeatManager->sendPointerFrame();
    }

    // One wheel notch as onMouseWheel sends it with a scroll factor of 1: delta 15, one discrete step, value120 of 120.
    // Negative is up in Wayland.
    void wheel(int sign) {
        if (direct()) {
            if (clearAt() && hiddenReady(*g_op))
                hiddenWheel(*g_op, sign);
            return;
        }
        if (!targetUnderPointer())
            return;
        g_pSeatManager->sendPointerAxis(now(), WL_POINTER_AXIS_VERTICAL_SCROLL, 15.0 * sign, sign, 120 * sign, WL_POINTER_AXIS_SOURCE_WHEEL,
                                        WL_POINTER_AXIS_RELATIVE_DIRECTION_IDENTICAL);
        g_pSeatManager->sendPointerFrame();
    }

    std::string pointAt(const Vector2D& pos) {
        return std::format(R"("x":{},"y":{})", pos.x, pos.y);
    }

    // Where the action's pointer is and how its input got there: the agent seat, the client's own pointer, or the
    // user's seat.
    std::string outcome(const SOp& op) {
        return std::format(R"({},"route":"{}")", pointAt(op.target), op.agentSeat ? "agent" : op.direct ? "client" : "seat");
    }

    // A path's `n` step: the caller is answered now, while the action goes on.
    void answerEarly() {
        if (!g_op->error.empty())
            return;
        complete(g_op->ticket, std::format(R"({{"ok":true,"ticket":{},{}}})", g_op->ticket, outcome(*g_op)));
        g_op->answered = true;
    }

    void finish() {
        SInputOwner agent;
        auto        op = std::move(*g_op);
        g_op.reset();
        dropTimer(op.timer);
        op.focusChange.reset();
        if (op.mode == eMode::SHOWN && !op.down.empty())
            agentcursor::release(op.agent);
        // An action that ends early (an error, `end`, an unload) still lets go of every button it holds. A drag and
        // drop started while it held one is its own, since the user's input is held meanwhile: it is cancelled rather
        // than dropped wherever the pointer happened to be.
        if (!op.down.empty())
            PROTO::data->abortDndIfPresent();

        if (op.direct) {
            // Every button it still holds goes up, and the client's pointer leaves its window.
            for (const auto BUTTON : op.down)
                hiddenButton(op, BUTTON, WL_POINTER_BUTTON_STATE_RELEASED);
            hiddenLeave(op);
            pumpFrames(Desktop::viewState()->query().selector(std::format("address:0x{:x}", op.window)).runWindow());
        } else {
            for (const auto BUTTON : op.down) {
                g_pSeatManager->sendPointerButton(now(), BUTTON, WL_POINTER_BUTTON_STATE_RELEASED);
                g_pSeatManager->sendPointerFrame();
            }

            Pointer::mgr()->warpTo(op.saved);
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
        }

        replayHeld(op.heldInput);

        if (!op.answered)
            complete(op.ticket, op.error.empty() ? std::format(R"({{"ok":true,{}}})", outcome(op)) : errorReply(op.error, op.code));
        startNext();
    }

    void runNext(SP<CEventLoopTimer>, void*) {
        if (!g_op)
            return;
        SInputOwner agent;
        if (!g_op->error.empty() || g_op->next >= g_op->steps.size()) {
            finish();
            return;
        }
        const auto& STEP = g_op->steps[g_op->next++];
        const auto  WAIT = STEP.wait;
        STEP.run();
        if (g_op)
            g_op->timer->updateTimeout(g_op->next >= g_op->steps.size() ? std::max(WAIT, ms(SETTLE)) : std::max(WAIT, ms(1)));
    }

    // Starts a pointer action; the user's pointer and focus are saved now, when it runs, not when it was queued.
    std::optional<std::string> startPointer(SOp op, uint64_t ticket) {
        // The user's drag and drop follows the pointer, so an action's pointer would carry it off and drop it; a hidden
        // action's press could start one of its own, which only the seat's pointer can carry.
        if (PROTO::data->dndActive())
            return "a drag and drop is in progress: try again when it ends";
        op.ticket = ticket;
        // A window whose app bound the agent seat takes its agent pointer. Otherwise a window of the app the user's
        // pointer is on cannot take the app's own pointer: one wl_pointer is on one surface.
        const auto WINDOW = windowOf(op);
        const auto USER   = g_pSeatManager->m_state.pointerFocus.lock();
        const auto MINE   = WINDOW && WINDOW->wlSurface() && WINDOW->wlSurface()->resource() ? WINDOW->wlSurface()->resource()->client() : nullptr;
        const bool NATIVE = WINDOW && !WINDOW->backend().isX11();
        op.agentSeat      = NATIVE && MINE && !agentPointersOf(MINE).empty();
        op.direct         = op.mode == eMode::HIDDEN || op.agentSeat || (NATIVE && (!USER || USER->client() != MINE));
        if (!op.direct) {
            op.saved        = g_pInputManager->getMouseCoordsInternal();
            op.focusWindow  = Desktop::focusState()->window();
            op.focusSurface = Desktop::focusState()->surface();
            op.hadFocus     = op.focusWindow || op.focusSurface;
        }
        g_op.emplace(std::move(op));

        if (!direct())
            g_pHyprRenderer->setCursorHidden(true);
        g_op->timer = makeShared<CEventLoopTimer>(ms(1), runNext, nullptr);
        g_pEventLoopManager->addTimer(g_op->timer);
        return std::nullopt;
    }

    // Runs a queued pointer action. A SHOWN one starts once the agent cursor has glided to its first point; the user's
    // input runs on through the glide, and the queue waits behind it.
    std::optional<std::string> beginPointer(SOp op, uint64_t ticket) {
        if (op.mode != eMode::SHOWN)
            return startPointer(std::move(op), ticket);
        g_gliding         = true;
        const auto START  = op.start;
        const auto AGENT  = op.agent;
        agentcursor::glideTo(AGENT, START, [op = std::move(op), ticket]() mutable {
            g_gliding = false;
            if (const auto ERROR = startPointer(std::move(op), ticket))
                complete(ticket, errorReply(*ERROR));
            startNext();
        });
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

    // ---- agent seat ----
    //
    // A wl_seat named "deskpilot", with a pointer and nothing else, which seat.cpp advertises (see seat.hpp for why it
    // lives in its own library). A client that binds every seat (GTK and Qt do) gets a second pointer, the action's
    // alone: a window of that client takes each pointer action through it, in every mode, so the user's pointer can be
    // anywhere, the same app included, and nothing of theirs is held. Clients that bind one seat (Chromium, Electron,
    // kitty), X clients, and apps started before the plugin first loaded (GTK 4 binds only the seats it finds when it
    // starts) keep the other routes.
    //
    //   - A move, resize, pointer warp or activation that names the agent seat is ignored, as Hyprland finds no record of
    //     it, and its events carry serials Hyprland never records, so they earn nothing, as for hkNextSerial.
    //   - A popup it opens asks for its grab on the client's first seat (GTK 4 always grabs with the default seat), so
    //     the grab is told from the user's by the client's latest input (hkAddOrStartGrab).
    void*             g_agentLibrary = nullptr;
    const SAgentSeat* g_agentSeat    = nullptr;

    uint32_t agentSerial(wl_client* client) {
        g_agentLast[client] = true;
        return wl_display_next_serial(g_pCompositor->m_wlDisplay);
    }

    std::vector<WP<CWlPointer>> agentPointersOf(wl_client* client) {
        return g_agentSeat->pointersOf(client);
    }

    bool isAgentPointer(wl_resource* resource) {
        return g_agentSeat->isPointer(resource);
    }

    // Other protocols take a client's wl_pointer and either ignore which one it is or look up Hyprland's record of it.
    //
    //   - A cursor shape a client sets for its agent pointer would be the user's cursor whenever their pointer is on
    //     that client, since Hyprland takes any shape from the client under the user's pointer. The client names the
    //     enter serial it answers, so a shape for one of the agent pointer's recent enters is dropped.
    //   - A cursor capture session for a pointer Hyprland has no record of would dereference null, so it is refused,
    //     which the protocol reports to the client as a stopped session.
    //   - Hyprland sends the user's touchpad gestures and relative motion to every gesture and relative pointer object
    //     of the client under their pointer, and applies a pointer lock or confinement to the user's pointer, whichever
    //     pointer the client named. So a client asking for one with its agent pointer (GTK makes gesture objects for
    //     each seat) gets an inert object of the seat library's instead.
    constexpr size_t      AGENT_ENTERS = 64;
    std::deque<uint32_t>  g_agentEnters;

    uint32_t agentEnterSerial(wl_client* client) {
        const auto SERIAL = agentSerial(client);
        g_agentEnters.push_back(SERIAL);
        if (g_agentEnters.size() > AGENT_ENTERS)
            g_agentEnters.pop_front();
        return SERIAL;
    }

    using SetShapeFn            = void (*)(CCursorShapeProtocol*, CWpCursorShapeDeviceV1*, uint32_t, wpCursorShapeDeviceV1Shape);
    CFunctionHook* g_shapeHook = nullptr;

    void hkOnSetShape(CCursorShapeProtocol* self, CWpCursorShapeDeviceV1* device, uint32_t serial, wpCursorShapeDeviceV1Shape shape) {
        if (std::ranges::contains(g_agentEnters, serial))
            return;
        reinterpret_cast<SetShapeFn>(g_shapeHook->m_original)(self, device, serial, shape);
    }

    using CursorSessionFn               = UP<Screenshare::CCursorshareSession> (*)(Screenshare::CScreenshareManager*, wl_client*, WP<CWLPointerResource>);
    CFunctionHook* g_cursorSessionHook = nullptr;

    UP<Screenshare::CCursorshareSession> hkNewCursorSession(Screenshare::CScreenshareManager* self, wl_client* client, WP<CWLPointerResource> pointer) {
        if (!pointer)
            return nullptr;
        return reinterpret_cast<CursorSessionFn>(g_cursorSessionHook->m_original)(self, client, pointer);
    }

    // A gesture or relative pointer asked for with an agent pointer (Hyprland ignores which pointer it names).
    template <CFunctionHook*& HOOK, eInert KIND, typename PROTOCOL, typename MANAGER>
    void hkGetForPointer(PROTOCOL* self, MANAGER* manager, uint32_t id, wl_resource* pointer) {
        if (isAgentPointer(pointer))
            return g_agentSeat->inert(KIND, manager->client(), manager->version(), id);
        reinterpret_cast<void (*)(PROTOCOL*, MANAGER*, uint32_t, wl_resource*)>(HOOK->m_original)(self, manager, id, pointer);
    }

    // A pointer lock or confinement asked for with an agent pointer (Hyprland applies it to the user's pointer).
    template <CFunctionHook*& HOOK, eInert KIND>
    void hkConstrain(CPointerConstraintsProtocol* self, CZwpPointerConstraintsV1* manager, uint32_t id, wl_resource* surface, wl_resource* pointer, wl_resource* region,
                     zwpPointerConstraintsV1Lifetime lifetime) {
        if (isAgentPointer(pointer))
            return g_agentSeat->inert(KIND, manager->client(), manager->version(), id);
        reinterpret_cast<decltype(&hkConstrain<HOOK, KIND>)>(HOOK->m_original)(self, manager, id, surface, pointer, region, lifetime);
    }

    CFunctionHook* g_swipeHook    = nullptr;
    CFunctionHook* g_pinchHook    = nullptr;
    CFunctionHook* g_holdHook     = nullptr;
    CFunctionHook* g_relativeHook = nullptr;
    CFunctionHook* g_lockHook     = nullptr;
    CFunctionHook* g_confineHook  = nullptr;

    void startAgentSeat() {
        g_agentLibrary = dlopen(DESKPILOT_SEAT, RTLD_NOW | RTLD_NODELETE);
        const auto START = g_agentLibrary ? reinterpret_cast<decltype(&deskpilot_agent_seat)>(dlsym(g_agentLibrary, "deskpilot_agent_seat")) : nullptr;
        g_agentSeat      = START ? START(g_pCompositor->m_wlDisplay) : nullptr;
        if (!g_agentSeat)
            throw std::runtime_error(std::format("deskpilot: could not open the agent seat {}: {}", DESKPILOT_SEAT, g_agentLibrary ? "no seat" : dlerror()));
    }

    // The seat, and the inert objects clients made for it, stay: its library is never unmapped.
    void stopAgentSeat() {
        g_agentSeat = nullptr;
        if (g_agentLibrary)
            dlclose(g_agentLibrary);
        g_agentLibrary = nullptr;
    }

    // What an action sends a pointer, for the client's own pointers and agent pointers alike.
    void sendEnter(CWLPointerResource& pointer, const SP<CWLSurfaceResource>& surface, const Vector2D& local) {
        pointer.sendEnter(surface, local);
    }
    void sendEnter(CWlPointer& pointer, const SP<CWLSurfaceResource>& surface, const Vector2D& local) {
        pointer.sendEnterRaw(agentEnterSerial(pointer.client()), surface->getResource()->resource(), wl_fixed_from_double(local.x), wl_fixed_from_double(local.y));
    }
    void sendLeave(CWLPointerResource& pointer, const SP<CWLSurfaceResource>&) {
        pointer.sendLeave();
    }
    void sendLeave(CWlPointer& pointer, const SP<CWLSurfaceResource>& surface) {
        if (surface)
            pointer.sendLeaveRaw(agentSerial(pointer.client()), surface->getResource()->resource());
    }
    void sendMotion(CWLPointerResource& pointer, const Vector2D& local) {
        pointer.sendMotion(now(), local);
    }
    void sendMotion(CWlPointer& pointer, const Vector2D& local) {
        pointer.sendMotion(now(), wl_fixed_from_double(local.x), wl_fixed_from_double(local.y));
    }
    void sendButton(CWLPointerResource& pointer, uint32_t button, wl_pointer_button_state state) {
        pointer.sendButton(now(), button, state);
    }
    void sendButton(CWlPointer& pointer, uint32_t button, wl_pointer_button_state state) {
        pointer.sendButton(agentSerial(pointer.client()), now(), button, state);
    }
    void sendFrame(CWLPointerResource& pointer) {
        pointer.sendFrame();
    }
    void sendFrame(CWlPointer& pointer) {
        if (pointer.version() >= 5)
            pointer.sendFrame();
    }
    // What CSeatManager::sendPointerAxis sends one pointer for a wheel notch.
    void sendNotch(CWLPointerResource& pointer, int sign) {
        pointer.sendAxis(now(), WL_POINTER_AXIS_VERTICAL_SCROLL, 15.0 * sign);
        pointer.sendAxisSource(WL_POINTER_AXIS_SOURCE_WHEEL);
        pointer.sendAxisRelativeDirection(WL_POINTER_AXIS_VERTICAL_SCROLL, WL_POINTER_AXIS_RELATIVE_DIRECTION_IDENTICAL);
        if (pointer.version() >= 8)
            pointer.sendAxisValue120(WL_POINTER_AXIS_VERTICAL_SCROLL, 120 * sign);
        else
            pointer.sendAxisDiscrete(WL_POINTER_AXIS_VERTICAL_SCROLL, sign);
    }
    void sendNotch(CWlPointer& pointer, int sign) {
        pointer.sendAxis(now(), WL_POINTER_AXIS_VERTICAL_SCROLL, wl_fixed_from_double(15.0 * sign));
        if (pointer.version() >= 5)
            pointer.sendAxisSource(WL_POINTER_AXIS_SOURCE_WHEEL);
        if (pointer.version() >= 9)
            pointer.sendAxisRelativeDirection(WL_POINTER_AXIS_VERTICAL_SCROLL, WL_POINTER_AXIS_RELATIVE_DIRECTION_IDENTICAL);
        if (pointer.version() >= 8)
            pointer.sendAxisValue120(WL_POINTER_AXIS_VERTICAL_SCROLL, 120 * sign);
        else if (pointer.version() >= 5)
            pointer.sendAxisDiscrete(WL_POINTER_AXIS_VERTICAL_SCROLL, sign);
    }

    // ---- hidden pointer ----
    //
    // A hidden action drives the window client's own pointer: the client's wl_pointer resources are entered on the
    // window's surface at the point and get the motion, buttons and wheel there, while the seat's pointer, its focus,
    // the cursor and keyboard focus stay as they are. Hyprland's hit test for one window finds the surface (its
    // subsurfaces and popups included) wherever the window is, so it need not be on screen.
    //
    //   - A wl_pointer is on one surface at a time, so the client the user's pointer is on is refused: entering the
    //     window would take the pointer from under the user.
    //   - Hyprland sends every client's pointer a leave on each change of the seat's pointer focus. The action's
    //     pointer is spared it while the user's pointer moves between other clients (hkSetPointerFocus), so what the
    //     hover raised stays up.
    //   - An XWayland window is refused: X clients read the pointer from the X server, which has one pointer for all
    //     of them, and only the seat's pointer moves it.
    //   - A drag and drop the press starts would follow the user's pointer, so it is cancelled and the action ends.
    //   - A window off screen gets no frame callbacks, so after input it and its popups get them for PUMP, to draw
    //     what the input changed before a screenshot.

    struct SSeatPointers {
        friend constexpr auto member(SSeatPointers);
    };
    template struct SAccess<&CWLSeatProtocol::m_pointers, SSeatPointers>;

    constexpr auto PUMP = std::chrono::seconds(1);

    PHLWINDOW windowOf(const SOp& op) {
        return Desktop::viewState()->query().selector(std::format("address:0x{:x}", op.window)).runWindow();
    }

    // Runs `fn` on each pointer the action has entered, whichever kind they are.
    template <typename FN>
    void eachPointer(SOp& op, FN&& fn) {
        for (const auto& pointer : op.pointers) {
            if (const auto LIVE = pointer.lock())
                fn(*LIVE);
        }
        for (const auto& pointer : op.agents) {
            if (const auto LIVE = pointer.lock())
                fn(*LIVE);
        }
    }

    void enterPointers(SOp& op) {
        // Also run when the user's pointer moves, outside the action's own steps.
        SInputOwner agent;
        const auto  SURFACE = op.surface.lock();
        if (!SURFACE)
            return;
        eachPointer(op, [&](auto& pointer) {
            sendEnter(pointer, SURFACE, op.local);
            sendFrame(pointer);
        });
    }

    // Whether the action's pointer may send now: still entered on the window, with the user's pointer elsewhere.
    bool hiddenReady(SOp& op) {
        if (!op.error.empty())
            return false;
        const auto SURFACE = op.surface.lock();
        if (!SURFACE) {
            op.error = "the surface under the action's pointer is gone";
            op.code  = "gone";
            return false;
        }
        if (const auto USER = g_pSeatManager->m_state.pointerFocus.lock(); !op.agentSeat && USER && USER->client() == SURFACE->client()) {
            op.error = "the user's pointer is on another window of this app, and hidden input would take it from there";
            return false;
        }
        if (PROTO::data->dndActive()) {
            PROTO::data->abortDndIfPresent();
            op.error = "the press started a drag and drop, which a hidden action cannot carry: it was cancelled";
            return false;
        }
        return true;
    }

    // The window's surface at `pos` and the point on it.
    bool hiddenSurfaceAt(SOp& op, const Vector2D& pos, SP<CWLSurfaceResource>& surface, Vector2D& local) {
        const auto WINDOW = windowOf(op);
        if (!WINDOW || !WINDOW->mapped()) {
            op.error = "the window closed";
            op.code  = "gone";
            return false;
        }
        surface = Desktop::viewState()->hitTest().windowSurfaceAt(pos, WINDOW, local);
        if (!surface) {
            op.error = std::format("no part of {} takes pointer input at {},{}", describe(WINDOW), pos.x, pos.y);
            return false;
        }
        return true;
    }

    void hiddenEnter(SOp& op, const Vector2D& pos) {
        const auto WINDOW = windowOf(op);
        if (WINDOW && WINDOW->backend().isX11()) {
            op.error = "an XWayland window reads the pointer from the X server, which only the user's pointer moves: act on it on screen instead";
            return;
        }
        SP<CWLSurfaceResource> surface;
        if (!hiddenSurfaceAt(op, pos, surface, op.local))
            return;
        op.surface = surface;
        if (!hiddenReady(op))
            return;
        if (op.agentSeat)
            op.agents = agentPointersOf(surface->client());
        else {
            for (const auto& pointer : (*PROTO::seat).*member(SSeatPointers{})) {
                if (pointer && pointer->m_owner && pointer->m_owner->client() == surface->client())
                    op.pointers.emplace_back(pointer);
            }
        }
        if (op.pointers.empty() && op.agents.empty()) {
            op.error = describe(WINDOW) + " has no pointer to send input to";
            return;
        }
        // The agent pointer is the action's alone: the seat's pointer focus never moves it.
        if (!op.agentSeat)
            op.focusChange = g_pSeatManager->m_events.pointerFocusChange.listen([] {
                if (!g_op || !g_op->direct || !g_op->error.empty())
                    return;
                // A drag and drop the press started takes the seat's pointer focus; finish() cancels it.
                if (!g_op->down.empty() && PROTO::data->dndActive()) {
                    g_op->error = "the press started a drag and drop, which a hidden action cannot carry: it was cancelled";
                    return;
                }
                if (hiddenReady(*g_op))
                    enterPointers(*g_op);
            });
        enterPointers(op);
        eachPointer(op, [&](auto& pointer) {
            sendMotion(pointer, op.local);
            sendFrame(pointer);
        });
        pumpFrames(WINDOW);
    }

    // With a button down the pressed surface keeps the pointer, as for the user's own drags.
    void hiddenMove(SOp& op, const Vector2D& pos) {
        if (!hiddenReady(op))
            return;
        if (op.down.empty()) {
            SP<CWLSurfaceResource> surface;
            Vector2D               local;
            if (!hiddenSurfaceAt(op, pos, surface, local))
                return;
            op.local = local;
            if (const auto LEFT = op.surface.lock(); surface != LEFT) {
                eachPointer(op, [&](auto& pointer) { sendLeave(pointer, LEFT); });
                op.surface = surface;
                enterPointers(op);
            }
        } else if (const auto WINDOW = windowOf(op))
            op.local = Desktop::viewState()->hitTest().surfaceLocalAt(pos, WINDOW, op.surface.lock());
        eachPointer(op, [&](auto& pointer) {
            sendMotion(pointer, op.local);
            sendFrame(pointer);
        });
    }

    // Popups that mapped without a grab (hkAddOrStartGrab), by client. A grab closes its popups on a click outside them,
    // and a client waits for that: GTK takes no click on the window under an open menu. Any press outside them, the
    // user's or deskpilot's, closes them here instead.
    std::map<wl_client*, std::vector<WP<CXDGPopupResource>>> g_ungrabbed;

    void closeUngrabbed(const SP<CWLSurfaceResource>& target) {
        std::erase_if(g_ungrabbed, [&](auto& entry) {
            auto& popups = entry.second;
            std::erase_if(popups, [](const auto& popup) { return !popup; });
            if (std::ranges::any_of(popups, [&](const auto& popup) { return popup->m_surface && popup->m_surface->m_surface.lock() == target; }))
                return false;
            for (const auto& popup : popups | std::views::reverse)
                popup->done();
            return true;
        });
    }

    using SeatButtonFn = void (*)(CSeatManager*, uint32_t, uint32_t, wl_pointer_button_state);
    CFunctionHook* g_seatButtonHook = nullptr;

    void hkSendPointerButton(CSeatManager* self, uint32_t timeMs, uint32_t button, wl_pointer_button_state state) {
        if (const auto SURFACE = self->m_state.pointerFocus.lock(); SURFACE && state == WL_POINTER_BUTTON_STATE_PRESSED)
            closeUngrabbed(SURFACE);
        reinterpret_cast<SeatButtonFn>(g_seatButtonHook->m_original)(self, timeMs, button, state);
    }

    void hiddenButton(SOp& op, uint32_t button, wl_pointer_button_state state) {
        if (const auto SURFACE = op.surface.lock(); SURFACE && state == WL_POINTER_BUTTON_STATE_PRESSED)
            closeUngrabbed(SURFACE);
        eachPointer(op, [&](auto& pointer) {
            sendButton(pointer, button, state);
            sendFrame(pointer);
        });
        pumpFrames(windowOf(op));
    }

    void hiddenWheel(SOp& op, int sign) {
        eachPointer(op, [&](auto& pointer) {
            sendNotch(pointer, sign);
            sendFrame(pointer);
        });
        pumpFrames(windowOf(op));
    }

    // Only a pointer still on the action's surface leaves: a client pointer the user's pointer has entered since is
    // theirs.
    void hiddenLeave(SOp& op) {
        const auto SURFACE = op.surface.lock();
        const auto USER    = g_pSeatManager->m_state.pointerFocus.lock();
        if (!SURFACE || (!op.agentSeat && USER && USER->client() == SURFACE->client()))
            return;
        eachPointer(op, [&](auto& pointer) {
            sendLeave(pointer, SURFACE);
            sendFrame(pointer);
        });
    }

    // Windows off screen that get frame callbacks until a time, as a window on screen gets them every frame.
    std::vector<std::pair<PHLWINDOWREF, Time::steady_tp>> g_pumped;
    SP<CEventLoopTimer>                                    g_pumpTimer;

    void pumpTick(SP<CEventLoopTimer>, void*) {
        const auto NOW = Time::steadyNow();
        std::erase_if(g_pumped, [&](const auto& entry) { return !entry.first || entry.second <= NOW; });
        for (const auto& [ref, until] : g_pumped) {
            const auto WINDOW = ref.lock();
            if (!WINDOW->mapped() || g_pHyprRenderer->shouldRenderWindow(WINDOW) || !WINDOW->wlSurface() || !WINDOW->wlSurface()->resource())
                continue;
            const auto FRAME = [&NOW](SP<CWLSurfaceResource> surface, const Vector2D&, void*) { surface->frame(NOW); };
            WINDOW->wlSurface()->resource()->breadthfirst(FRAME, nullptr);
            // A popup (a menu, a tooltip) is a surface tree of its own, and draws only on its own frame callbacks.
            if (const auto HEAD = WINDOW->popupHead()) {
                HEAD->breadthfirst(
                    [&FRAME](WP<Desktop::View::CPopup> popup, void*) {
                        if (popup && popup->mapped() && popup->wlSurface() && popup->wlSurface()->resource())
                            popup->wlSurface()->resource()->breadthfirst(FRAME, nullptr);
                    },
                    nullptr);
            }
        }
        if (!g_pumped.empty())
            g_pumpTimer->updateTimeout(agentcursor::FRAME * 2);
    }

    void pumpFrames(const PHLWINDOW& window) {
        if (!window || !g_pumpTimer)
            return;
        const auto UNTIL = Time::steadyNow() + PUMP;
        auto       it    = std::ranges::find_if(g_pumped, [&](const auto& entry) { return entry.first.lock() == window; });
        if (it == g_pumped.end())
            g_pumped.emplace_back(window, UNTIL);
        else
            it->second = UNTIL;
        g_pumpTimer->updateTimeout(ms(1));
    }

    // A popup's grab confines the user's pointer and keyboard to it until a click outside closes it, and moves their
    // keyboard focus into it. A popup deskpilot's input opened maps without one, as does any popup of a window off
    // screen, which the user could not see holding their input; the window's client still gets its input there.
    using GrabFn = void (*)(CXDGShellProtocol*, SP<CXDGPopupResource>);
    CFunctionHook* g_grabHook = nullptr;

    void hkAddOrStartGrab(CXDGShellProtocol* self, SP<CXDGPopupResource> popup) {
        // A popup is opened by the latest input its client got. The seat its grab names does not say whose that was:
        // GTK 4 names its first seat for a popup the agent seat opened.
        const auto SURFACE = popup && popup->m_surface ? popup->m_surface->m_surface.lock() : nullptr;
        if (SURFACE && g_agentLast[SURFACE->client()]) {
            g_ungrabbed[SURFACE->client()].emplace_back(popup);
            return;
        }
        auto parent = popup ? popup->m_parent.lock() : nullptr;
        while (parent && parent->m_popup && parent->m_popup->m_parent)
            parent = parent->m_popup->m_parent.lock();
        const auto HLSURFACE = parent ? Desktop::View::CWLSurface::fromResource(parent->m_surface.lock()) : nullptr;
        const auto WINDOW    = HLSURFACE ? Desktop::View::CWindow::fromView(HLSURFACE->view()) : nullptr;
        if (WINDOW && !g_pHyprRenderer->shouldRenderWindow(WINDOW)) {
            if (SURFACE)
                g_ungrabbed[SURFACE->client()].emplace_back(popup);
            return;
        }
        reinterpret_cast<GrabFn>(g_grabHook->m_original)(self, popup);
    }

    // Hyprland sends every client's pointer a leave on each change of the seat's pointer focus, wherever it was. A
    // hidden action's pointer is not the seat's: while the user's pointer moves between other clients' surfaces, the
    // leave it would get there is not sent, so a tooltip or hover state the action raised stays up. A move onto a
    // surface of the action's own client still leaves it, and the action is refused there (hiddenReady).
    using PointerFocusFn = void (*)(CSeatManager*, SP<CWLSurfaceResource>, const Vector2D&);
    using LeaveFn = void (*)(CWLPointerResource*);
    CFunctionHook* g_pointerFocusHook = nullptr;
    CFunctionHook* g_leaveHook        = nullptr;
    bool           g_shieldHidden     = false;

    void hkSetPointerFocus(CSeatManager* self, SP<CWLSurfaceResource> surface, const Vector2D& local) {
        const auto OURS = g_op && g_op->direct && g_op->surface.lock();
        g_shieldHidden  = OURS && (!surface || surface->client() != g_op->surface.lock()->client());
        reinterpret_cast<PointerFocusFn>(g_pointerFocusHook->m_original)(self, surface, local);
        g_shieldHidden = false;
    }

    void hkSendLeave(CWLPointerResource* self) {
        if (g_shieldHidden && g_op && std::ranges::any_of(g_op->pointers, [self](const auto& pointer) { return pointer.get() == self; }))
            return;
        reinterpret_cast<LeaveFn>(g_leaveHook->m_original)(self);
    }

    // Hyprland takes a popup of a window off screen as one that cannot take input, and both the popup hit test and a
    // window capture leave out such popups: a tooltip or menu a hidden action opened would neither show in its
    // screenshot nor take its clicks. Being on screen is no part of that here: the seat's own hit test only reaches
    // windows on screen, and such a popup gets no grab (hkAddOrStartGrab), so the user's input still never goes there.
    using PopupFocusFn = bool (*)(const Desktop::View::CPopup*);
    CFunctionHook* g_popupFocusHook = nullptr;

    bool hkPopupFocusAvailable(const Desktop::View::CPopup* self) {
        if (reinterpret_cast<PopupFocusFn>(g_popupFocusHook->m_original)(self))
            return true;
        const auto WINDOW = self->windowOwner();
        return WINDOW && WINDOW->mapped() && WINDOW->acceptsInput() && !g_pHyprRenderer->shouldRenderWindow(WINDOW) && self->wlSurface() &&
            self->wlSurface()->resource();
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
    struct SDropDrag {
        friend constexpr auto member(SDropDrag);
    };
    template struct SAccess<&CWLDataDeviceProtocol::dropDrag, SDropDrag>;
    struct SDnd {
        friend constexpr auto member(SDnd);
    };
    template struct SAccess<&CWLDataDeviceProtocol::m_dnd, SDnd>;

    // A drag and drop started from an action's press follows the action's pointer as it would the user's. Hyprland
    // moves and drops one from its input event bus, which an action's own events never go through, so this does what
    // its listeners do: the surface under the pointer becomes the drop target and gets the motion.
    void dndMotion(const Vector2D& pos) {
        g_pInputManager->refocusPointer();
        const auto DEVICE = ((*PROTO::data).*member(SDnd{})).focusedDevice.lock();
        const auto TARGET = g_pSeatManager->m_state.dndPointerFocus.lock();
        const auto HLSURFACE = TARGET ? Desktop::View::CWLSurface::fromResource(TARGET) : nullptr;
        const auto BOX = HLSURFACE ? HLSURFACE->getSurfaceBoxGlobal() : std::nullopt;
        if (DEVICE && BOX)
            DEVICE->sendMotion(now(), pos - BOX->pos());
    }

    void dndDrop() {
        ((*PROTO::data).*member(SDropDrag{}))();
    }

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
            std::erase(write->source->m_writes, write);
            wl_event_source_remove(write->event);
            delete write;
            return 0;
        }

        std::string          m_text;
        std::vector<SWrite*> m_writes;
    };

    // How long a window keeps the pasted text as its clipboard. A client may read the text as soon as it is offered,
    // as Chrome does to cache it, and paste that copy when the chord arrives; another reads it at the chord. Nothing
    // tells when a client is done with it, and taking it back after the first read left Chrome with an empty
    // clipboard by the time its chord arrived, so the offer lasts this long.
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

    // Offers `text` as the clipboard of `window`'s client alone, until PASTE_WAIT passes or the next paste.
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
        SInputOwner agent;
        endLinger(*g_keyOp);
        auto op = std::move(*g_keyOp);
        g_keyOp.reset();
        dropTimer(op.timer);
        if (op.xkb)
            xkb_state_unref(op.xkb);
        for (auto& replay : op.heldInput)
            replay();
        complete(op.ticket, op.error.empty() ? okReply() : errorReply(op.error, op.code));
        startNext();
    }

    // The stroke is over: a focus change Hyprland made meanwhile takes effect, the user's held input is replayed, and
    // the next stroke follows after the gap.
    std::optional<ms> endStroke(SKeyOp& op) {
        op.holding  = false;
        op.inStroke = false;
        // A window off screen draws what the keys changed only with frame callbacks.
        pumpFrames(op.window.lock());
        // The deferred focus change and the held input are Hyprland's and the user's, not the stroke's.
        SInputOwner user{false};
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

    // A window still entered between strokes is left, so its app sees itself unfocused again.
    void endLinger(SKeyOp& op) {
        if (!std::exchange(op.lingering, false))
            return;
        SInputOwner agent;
        eachKeyboard(op, [](const auto& keyboard) { keyboard->sendLeave(); });
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
        if (!op.holding && op.error.empty() && op.next < op.events.size()) {
            sendMods(op);
            op.lingering = true;
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
                    op.code  = "gone";
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
                if (op.lingering) {
                    if (op.surface.lock() == SURFACE && !(FOCUS && FOCUS->client() == SURFACE->client())) {
                        op.inStroke = true;
                        sendMods(op);
                        op.phase = SKeyOp::KEY;
                        return std::nullopt;
                    }
                    endLinger(op);
                }
                op.surface       = SURFACE;
                op.keyboards     = keyboardsOf(SURFACE->client());
                op.entered       = FOCUS != SURFACE;
                op.sibling       = op.entered && FOCUS && FOCUS->client() == SURFACE->client() ? FOCUS : nullptr;
                if (op.keyboards.empty()) {
                    op.error = "the window's client has no keyboard to send keys to";
                    op.phase = SKeyOp::DONE;
                    return std::nullopt;
                }
                op.inStroke = true;
                op.holding  = !op.entered || op.sibling || op.x11;
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
                    op.code  = "gone";
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
        SInputOwner agent;
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

    // `path [agent=NAME] <window> <shown|plain|hidden> <step>...`, where NAME is whose agent cursor shows it. A step is
    // mX,Y to move the pointer to global X,Y (the first step, where it enters the window), pB or rB to press or release
    // button B (272-274), wN to turn the wheel N notches (negative is up), dMS to wait MS milliseconds, or n to answer
    // the request there while the action goes on, with its ticket for `end`.
    constexpr auto PATH_USAGE = "usage: deskpilot path [agent=NAME] <window> <shown|plain|hidden> <step>...; a step is mX,Y, pBUTTON, rBUTTON, wNOTCHES, dMS or n";

    IPC::Socket1::SResponse pathCommand(const std::vector<std::string>& args, const std::string& agent) {
        if (args.size() < 5)
            return refuse(PATH_USAGE);
        const auto WINDOW = address(args[2]);
        if (!WINDOW)
            return refuse("bad window " + args[2]);
        SOp op;
        op.window = *WINDOW;
        op.agent  = agent;
        if (args[3] == "shown")
            op.mode = eMode::SHOWN;
        else if (args[3] == "plain")
            op.mode = eMode::PLAIN;
        else if (args[3] == "hidden")
            op.mode = eMode::HIDDEN;
        else
            return refuse(PATH_USAGE);
        std::vector<uint32_t> down;
        int                   moves = 0, presses = 0, notches = 0;
        ms                    dwell{0};
        bool                  answers = false;
        for (size_t i = 4; i < args.size(); ++i) {
            const std::string& WORD = args[i];
            const auto         BAD  = "bad path step '" + WORD + "'";
            const std::string  REST = WORD.substr(1);
            if (WORD[0] == 'm') {
                const auto COMMA = REST.find(',');
                const auto X     = COMMA == std::string::npos ? std::nullopt : number(REST.substr(0, COMMA));
                const auto Y     = COMMA == std::string::npos ? std::nullopt : number(REST.substr(COMMA + 1));
                if (!X || !Y)
                    return refuse(BAD);
                const Vector2D POINT{*X, *Y};
                if (moves++ == 0) {
                    op.start = op.target = POINT;
                    op.steps.push_back({[POINT] { enterAt(POINT); }, MOVE_GAP});
                } else
                    op.steps.push_back({[POINT] { moveTo(POINT); }, MOVE_GAP});
                continue;
            }
            if (moves == 0)
                return refuse("a path starts with a move, mX,Y");
            if (WORD[0] == 'p' || WORD[0] == 'r') {
                const auto CODE = number(REST);
                if (!CODE || *CODE < 272 || *CODE > 274)
                    return refuse(BAD + ": the button must be 272-274");
                const auto BUTTON = static_cast<uint32_t>(*CODE);
                const auto IT     = std::ranges::find(down, BUTTON);
                if (WORD[0] == 'p') {
                    if (IT != down.end())
                        return refuse(BAD + ": that button is down already");
                    down.push_back(BUTTON);
                    ++presses;
                    op.steps.push_back({[BUTTON] { press(BUTTON); }, HOLD});
                } else {
                    if (IT == down.end())
                        return refuse(BAD + ": that button is not down");
                    down.erase(IT);
                    op.steps.push_back({[BUTTON] { release(BUTTON); }, HOLD});
                }
                continue;
            }
            if (WORD[0] == 'w') {
                const auto N = number(REST);
                if (!N || *N == 0 || *N != std::trunc(*N))
                    return refuse(BAD + ": turn the wheel a non-zero whole number of notches");
                const int SIGN = *N > 0 ? -1 : 1;
                for (int n = 0; n < std::abs(static_cast<int>(*N)); ++n)
                    op.steps.push_back({[SIGN] { wheel(SIGN); }, BETWEEN / 2});
                notches += std::abs(static_cast<int>(*N));
                continue;
            }
            if (WORD[0] == 'd') {
                const auto MS = number(REST);
                if (!MS || *MS < 0 || *MS > MAX_DWELL.count())
                    return refuse(BAD);
                dwell += ms(static_cast<int>(*MS));
                op.steps.push_back({[] {}, ms(static_cast<int>(*MS))});
                continue;
            }
            if (WORD == "n" && !answers) {
                answers = true;
                op.steps.push_back({[] { answerEarly(); }, ms(1)});
                continue;
            }
            return refuse(WORD == "n" ? "a path answers once" : BAD);
        }
        if (!down.empty())
            return refuse("the path leaves a button down: release every button it presses");
        if (moves > MAX_MOVES || presses > MAX_PRESSES || notches > MAX_NOTCHES || dwell > MAX_DWELL)
            return refuse(std::format("a path may have at most {} moves, {} presses, {} wheel notches and {}ms of waits, since the user's pointer may wait for it",
                                      MAX_MOVES, MAX_PRESSES, MAX_NOTCHES, MAX_DWELL.count()));
        return enqueue([op = std::move(op)](uint64_t ticket) mutable { return beginPointer(std::move(op), ticket); });
    }

    // `cursor [agent=NAME] <x> <y>`: NAME's agent cursor glides to global X,Y, in turn with the actions, answered once
    // it lands.
    // The apps an agent launched (`deskpilot launch`). Each agent NAME has a window rule that puts every window of a
    // process whose environment carries HL_EXEC_RULE_TOKEN=deskpilot-agent-NAME (set by `launch`, inherited by the app's
    // children) on the hidden special:agent-NAME workspace, silently and without taking focus. Hyprland's own exec
    // rules are not used: they place only the first window and expire a minute after launch, so a later dialog would
    // open on the user's workspace. A config reload clears every rule, so the plugin registers them again after it.
    namespace agents {
        // An agent with no owner process is stopped after this long without a request naming it.
        constexpr auto IDLE = std::chrono::minutes(30);
        // How long a stopped agent's apps have to close their windows before their processes are sent SIGTERM.
        constexpr auto GRACE = std::chrono::seconds(5);
        constexpr auto CHECK = std::chrono::seconds(30);

        struct SAgent {
            SP<Desktop::Rule::CWindowRule> rule;
            // The process whose exit stops the agent; 0 stops it after IDLE instead.
            pid_t           owner = 0;
            Time::steady_tp used;
            // Set once stopping: SIGTERM is due then, and the rule stays until it is sent.
            SP<CEventLoopTimer> stopping;
        };

        std::map<std::string, SAgent> g_agents;
        SP<CEventLoopTimer>           g_reaper;
        CHyprSignalListener           g_reloaded;

        std::string token(const std::string& name) {
            return "deskpilot-agent-" + name;
        }

        std::string workspace(const std::string& name) {
            return "special:agent-" + name;
        }

        SP<Desktop::Rule::CWindowRule> makeRule(const std::string& name) {
            auto rule = Desktop::Rule::CWindowRule::buildFromExecString(
                std::format("workspace {} silent;no_initial_focus 1;suppress_event activate activatefocus fullscreen maximize", workspace(name)));
            if (!rule)
                return nullptr;
            // The match is a full regex match; a name's only regex character is '.'.
            std::string pattern;
            for (const char C : token(name))
                pattern += C == '.' ? std::string("\\.") : std::string(1, C);
            (*rule)->registerMatch(Desktop::Rule::RULE_PROP_EXEC_TOKEN, pattern);
            return *rule;
        }

        // Every process of this user whose environment carries the agent's token.
        std::vector<pid_t> processes(const std::string& name) {
            const std::string     ENTRY = std::format("{}={}", Desktop::Rule::EXEC_RULE_ENV_NAME, token(name));
            std::vector<pid_t>    found;
            std::error_code       error;
            for (const auto& dir : std::filesystem::directory_iterator("/proc", error)) {
                const auto PID = number(dir.path().filename().string());
                if (!PID || *PID <= 1 || *PID == getpid())
                    continue;
                std::ifstream file(dir.path() / "environ", std::ios::binary);
                for (std::string entry; std::getline(file, entry, '\0');) {
                    if (entry == ENTRY) {
                        found.push_back(static_cast<pid_t>(*PID));
                        break;
                    }
                }
            }
            return found;
        }

        void forget(const std::string& name) {
            const auto IT = g_agents.find(name);
            if (IT == g_agents.end())
                return;
            dropTimer(IT->second.stopping);
            if (IT->second.rule)
                Desktop::Rule::ruleEngine()->unregisterRule(IT->second.rule);
            g_agents.erase(IT);
        }

        // Asks each of the agent's windows to close, as the user closing it would, and sends SIGTERM to its processes
        // left after GRACE. The rule stays until then, so a window opened meanwhile still lands out of view.
        void stop(const std::string& name) {
            auto& agent = g_agents.at(name);
            if (agent.stopping)
                return;
            const auto WORKSPACE = workspace(name);
            for (const auto& window : Desktop::windowState()->windows()) {
                if (window->mapped() && window->m_workspace && window->m_workspace->addressableName() == WORKSPACE)
                    window->sendClose();
            }
            agent.stopping = makeShared<CEventLoopTimer>(
                GRACE,
                [name](SP<CEventLoopTimer>, void*) {
                    for (const auto PID : processes(name))
                        kill(PID, SIGTERM);
                    forget(name);
                },
                nullptr);
            g_pEventLoopManager->addTimer(agent.stopping);
        }

        void reap(SP<CEventLoopTimer> self, void*) {
            const auto          NOW = Time::steadyNow();
            std::vector<std::string> due;
            for (const auto& [name, agent] : g_agents) {
                const bool GONE = agent.owner > 0 ? kill(agent.owner, 0) == -1 && errno == ESRCH : NOW - agent.used > IDLE;
                if (GONE && !agent.stopping)
                    due.push_back(name);
            }
            for (const auto& name : due)
                stop(name);
            self->updateTimeout(CHECK);
        }

        void init() {
            g_reaper = makeShared<CEventLoopTimer>(CHECK, reap, nullptr);
            g_pEventLoopManager->addTimer(g_reaper);
            g_reloaded = Event::bus()->m_events.config.reloaded.listen([] {
                for (auto& [name, agent] : g_agents) {
                    if (agent.rule)
                        Desktop::Rule::ruleEngine()->registerRule(SP<Desktop::Rule::IRule>(agent.rule));
                }
            });
        }

        // The rules are Hyprland's, but a rule left registered would keep placing windows with no plugin to stop them.
        void shutdown() {
            g_reloaded.reset();
            dropTimer(g_reaper);
            g_reaper.reset();
            for (auto& [name, agent] : g_agents) {
                dropTimer(agent.stopping);
                if (agent.rule)
                    Desktop::Rule::ruleEngine()->unregisterRule(agent.rule);
            }
            g_agents.clear();
        }

        // `agent NAME [owner=PID]` registers NAME's rule if it has none and marks it in use; an owner, once given, is
        // kept. `agent NAME stop` stops it. Both answer at once.
        IPC::Socket1::SResponse command(const std::vector<std::string>& args) {
            constexpr auto USAGE = "usage: deskpilot agent <name> [owner=PID] | agent <name> stop";
            if (args.size() < 3 || args.size() > 4)
                return refuse(USAGE);
            const std::string& NAME = args[2];
            if (!agentcursor::validName(NAME))
                return refuse(std::format("bad agent name '{}': use up to {} letters, digits, '-', '_' or '.'", NAME, agentcursor::NAME_MAX));
            const auto IT = g_agents.find(NAME);
            // An agent the plugin no longer knows, as after it was reloaded, is stopped all the same.
            if (args.size() == 4 && args[3] == "stop") {
                g_agents[NAME];
                stop(NAME);
                return IPC::Socket1::SResponse(okReply());
            }
            pid_t owner = 0;
            if (args.size() == 4) {
                const auto PID = args[3].starts_with("owner=") ? number(args[3].substr(6)) : std::nullopt;
                if (!PID || *PID <= 1)
                    return refuse(USAGE);
                owner = static_cast<pid_t>(*PID);
            }
            if (IT != g_agents.end() && IT->second.stopping)
                return refuse(std::format("agent {} is stopping; try again in {} seconds", NAME, GRACE.count()), "busy");
            auto& agent = g_agents[NAME];
            if (!agent.rule) {
                agent.rule = makeRule(NAME);
                if (!agent.rule) {
                    g_agents.erase(NAME);
                    return refuse("could not build the window rule for agent " + NAME);
                }
                Desktop::Rule::ruleEngine()->registerRule(SP<Desktop::Rule::IRule>(agent.rule));
            }
            if (owner)
                agent.owner = owner;
            agent.used = Time::steadyNow();
            return IPC::Socket1::SResponse(std::format(R"({{"ok":true,"workspace":{},"token":{}}})", jsonString(workspace(NAME)), jsonString(token(NAME))));
        }
    }

    IPC::Socket1::SResponse cursorCommand(const std::vector<std::string>& args, const std::string& agent) {
        const auto X = args.size() == 4 ? number(args[2]) : std::nullopt, Y = args.size() == 4 ? number(args[3]) : std::nullopt;
        if (!X || !Y)
            return refuse("usage: deskpilot cursor [agent=NAME] <x> <y>");
        const Vector2D POINT{*X, *Y};
        return enqueue([POINT, agent](uint64_t ticket) -> std::optional<std::string> {
            g_gliding = true;
            agentcursor::glideTo(agent, POINT, [ticket, POINT] {
                g_gliding = false;
                complete(ticket, std::format(R"({{"ok":true,{}}})", pointAt(POINT)));
                startNext();
            });
            return std::nullopt;
        });
    }

    IPC::Socket1::SResponse onCommand(const IPC::Socket1::SRequest& req) {
        std::istringstream       in(req.command);
        std::vector<std::string> args;
        for (std::string word; in >> word;)
            args.push_back(word);
        // args[0] is "deskpilot".
        const std::string VERB = args.size() >= 2 ? args[1] : "";
        // `agent=NAME` right after the verb names whose agent cursor a path or cursor request moves; no other verb takes it.
        std::string agent;
        if ((VERB == "path" || VERB == "cursor") && args.size() >= 3 && args[2].starts_with("agent=")) {
            agent = args[2].substr(6);
            if (agent.empty() || !agentcursor::validName(agent))
                return refuse(std::format("bad agent name '{}': use up to {} letters, digits, '-', '_' or '.'", agent, agentcursor::NAME_MAX));
            args.erase(args.begin() + 2);
        }
        if (VERB == "status")
            return IPC::Socket1::SResponse(std::format(R"({{"ok":true,"version":{},"path":{},"busy":{},"queued":{}}})", jsonString(VERSION), jsonString(ownPath()),
                                                       g_op || g_keyOp || g_gliding ? "true" : "false", g_queue.size()));
        if (VERB == "wait") {
            const auto TICKET = args.size() == 3 ? number(args[2]) : std::nullopt;
            if (!TICKET || *TICKET < 1)
                return refuse("usage: deskpilot wait <ticket>");
            return awaitTicket(static_cast<uint64_t>(*TICKET));
        }
        if (VERB == "stage")
            return stageCommand(args);
        if (VERB == "path")
            return pathCommand(args, agent);
        // Ends the path with this ticket now, letting go of what it holds; a path that has ended already is fine.
        if (VERB == "end") {
            const auto TICKET = args.size() == 3 ? number(args[2]) : std::nullopt;
            if (!TICKET || *TICKET < 1)
                return refuse("usage: deskpilot end <ticket>");
            if (g_op && g_op->ticket == static_cast<uint64_t>(*TICKET)) {
                g_op->next = g_op->steps.size();
                g_op->timer->updateTimeout(ms(1));
            }
            return IPC::Socket1::SResponse(okReply());
        }
        if (VERB == "keys" || VERB == "paste")
            return keysCommand(args);
        if (VERB == "cursor")
            return cursorCommand(args, agent);
        if (VERB == "agent")
            return agents::command(args);
        return refuse("usage: deskpilot path [agent=NAME] <window> <shown|plain|hidden> <step>... | cursor [agent=NAME] <x> <y> | keys <window> <hold-ms> <gap-ms> <txn|none> <key event>... | "
                      "paste <window> <file> <hold-ms> <gap-ms> <txn|none> <key event>... | stage <txn> <key event>... | agent <name> [owner=PID|stop] | end <ticket> | wait <ticket> | status; a key "
                      "event is +code, -code, pHEX, rHEX or tHEX, and a path step mX,Y, pBUTTON, rBUTTON, wNOTCHES, dMS or n");
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

    g_moveHook     = hook(handle, "CInputManager", "onMouseMoved", reinterpret_cast<void*>(&holdPointer<g_moveHook, IPointer::SMotionEvent>));
    g_warpHook     = hook(handle, "CInputManager", "onMouseWarp", reinterpret_cast<void*>(&holdPointer<g_warpHook, IPointer::SMotionAbsoluteEvent>));
    g_buttonHook   = hook(handle, "CInputManager", "onMouseButton", reinterpret_cast<void*>(&holdPointer<g_buttonHook, IPointer::SButtonEvent, SP<IPointer>>));
    g_wheelHook    = hook(handle, "CInputManager", "onMouseWheel", reinterpret_cast<void*>(&holdPointer<g_wheelHook, IPointer::SAxisEvent, SP<IPointer>>));
    g_tabletAxis   = hook(handle, "CInputManager", "onTabletAxis", reinterpret_cast<void*>(&holdPointer<g_tabletAxis, CTablet::SAxisEvent>));
    g_tabletNear   = hook(handle, "CInputManager", "onTabletProximity", reinterpret_cast<void*>(&holdPointer<g_tabletNear, CTablet::SProximityEvent>));
    g_tabletTip    = hook(handle, "CInputManager", "onTabletTip", reinterpret_cast<void*>(&holdPointer<g_tabletTip, CTablet::STipEvent>));
    g_tabletButton = hook(handle, "CInputManager", "onTabletButton", reinterpret_cast<void*>(&holdPointer<g_tabletButton, CTablet::SButtonEvent>));
    g_keyHook      = hook(handle, "CInputManager", "onKeyboardKey", reinterpret_cast<void*>(&hkOnKeyboardKey));
    g_modHook      = hook(handle, "CInputManager", "onKeyboardMod", reinterpret_cast<void*>(&hkOnKeyboardMod));
    g_activateHook = hook(handle, "CWindow", "activate", reinterpret_cast<void*>(&hkActivate));
    g_focusHook    = hook(handle, "CSeatManager", "setKeyboardFocus", reinterpret_cast<void*>(&hkSetKeyboardFocus));
    g_serialHook   = hook(handle, "CSeatManager", "nextSerial", reinterpret_cast<void*>(&hkNextSerial));
    g_grabHook     = hook(handle, "CXDGShellProtocol", "addOrStartGrab", reinterpret_cast<void*>(&hkAddOrStartGrab));
    g_pointerFocusHook = hook(handle, "CSeatManager", "setPointerFocus", reinterpret_cast<void*>(&hkSetPointerFocus));
    g_leaveHook        = hook(handle, "CWLPointerResource", "sendLeave", reinterpret_cast<void*>(&hkSendLeave));
    g_popupFocusHook   = hook(handle, "CPopup", "focusAvailable", reinterpret_cast<void*>(&hkPopupFocusAvailable));
    g_keyboardLeaveHook = hook(handle, "CWLKeyboardResource", "sendLeave", reinterpret_cast<void*>(&hkKeyboardSendLeave));
    g_seatButtonHook    = hook(handle, "CSeatManager", "sendPointerButton", reinterpret_cast<void*>(&hkSendPointerButton));
    g_shapeHook         = hook(handle, "CCursorShapeProtocol", "onSetShape", reinterpret_cast<void*>(&hkOnSetShape));
    g_cursorSessionHook = hook(handle, "CScreenshareManager", "newCursorSession", reinterpret_cast<void*>(&hkNewCursorSession));
    g_swipeHook         = hook(handle, "CPointerGesturesProtocol", "onGetSwipeGesture", reinterpret_cast<void*>(&hkGetForPointer<g_swipeHook, eInert::SWIPE, CPointerGesturesProtocol, CZwpPointerGesturesV1>));
    g_pinchHook         = hook(handle, "CPointerGesturesProtocol", "onGetPinchGesture", reinterpret_cast<void*>(&hkGetForPointer<g_pinchHook, eInert::PINCH, CPointerGesturesProtocol, CZwpPointerGesturesV1>));
    g_holdHook          = hook(handle, "CPointerGesturesProtocol", "onGetHoldGesture", reinterpret_cast<void*>(&hkGetForPointer<g_holdHook, eInert::HOLD, CPointerGesturesProtocol, CZwpPointerGesturesV1>));
    g_relativeHook      = hook(handle, "CRelativePointerProtocol", "onGetRelativePointer", reinterpret_cast<void*>(&hkGetForPointer<g_relativeHook, eInert::RELATIVE, CRelativePointerProtocol, CZwpRelativePointerManagerV1>));
    g_lockHook          = hook(handle, "CPointerConstraintsProtocol", "onLockPointer", reinterpret_cast<void*>(&hkConstrain<g_lockHook, eInert::LOCKED>));
    g_confineHook       = hook(handle, "CPointerConstraintsProtocol", "onConfinePointer", reinterpret_cast<void*>(&hkConstrain<g_confineHook, eInert::CONFINED>));

    agentcursor::init();
    startAgentSeat();
    agents::init();
    g_pumpTimer = makeShared<CEventLoopTimer>(std::nullopt, pumpTick, nullptr);
    g_pEventLoopManager->addTimer(g_pumpTimer);

    HyprlandAPI::registerHyprCtlCommand(handle, IPC::Socket1::SCommand{.name = "deskpilot", .match = IPC::Socket1::COMMAND_MATCH_PREFIX, .handler = onCommand});
    return {"deskpilot", "Clicks, scrolls and keys for agents that leave the user's pointer and focus in place", "cjber", VERSION};
}

// The running action is ended and the user's pointer and focus put back; everything queued or waiting is answered.
APICALL EXPORT void PLUGIN_EXIT() {
    const auto UNLOADED = errorReply("the deskpilot plugin was unloaded", "unavailable");
    for (auto& queued : g_queue)
        complete(queued.ticket, UNLOADED);
    g_queue.clear();
    if (g_op) {
        g_op->error = "the deskpilot plugin was unloaded";
        g_op->code  = "unavailable";
        finish();
    }
    if (g_keyOp) {
        if (g_keyOp->holding)
            restoreStroke(*g_keyOp);
        g_keyOp->error = "the deskpilot plugin was unloaded";
        g_keyOp->code  = "unavailable";
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
    stopAgentSeat();
    agents::shutdown();
    agentcursor::shutdown();
    dropTimer(g_pumpTimer);
    g_pumped.clear();
    if (g_x)
        xcb_disconnect(g_x);
    g_x = nullptr;
}
