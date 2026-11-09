// deskpilot Hyprland plugin: clicks and scrolls that leave the user's pointer where it is.
//
// `hyprctl deskpilot click <window> <x> <y> <button> <count>` and `hyprctl deskpilot scroll <window> <x> <y> <dy>`
// run one input sequence on the user's own seat, so every client receives them, XWayland and Wine included:
//
//   1. The user's mouse input is held through hooks on CInputManager's onMouseMoved, onMouseButton and
//      onMouseWheel, which see the device as well as the event: a replayed release must name the device that
//      pressed, or Hyprland drops it and the button stays held. Nothing the user does can move the pointer mid-click.
//   2. The cursor image is hidden, the pointer warps to the point, and the window under it must be <window>;
//      anything else on top refuses before any press is sent.
//   3. Presses and releases are spaced by event-loop timers, so the client sees them as separate events. Before each
//      press or wheel notch the target must still be under the pointer, so a window that closes mid-sequence never
//      passes its click on to the one behind it. Releases always go, so no button is left down.
//   4. The pointer goes back to where the user left it plus the motion they made meanwhile, keyboard focus goes
//      back to the surface that had it, the cursor's visibility is restored and the held input is replayed.
//
// `hyprctl deskpilot keys <window> <+code|-code>...` presses and releases evdev key codes in <window> without focusing
// it. Only the seat's keyboard focus moves to the window for the keystroke and comes straight back; Hyprland's own
// window focus never changes. Modifiers come from an xkb state on the keyboard's own keymap, fed with these keys.
//
//   - A Wayland window gets enter, the keys and leave in one event-loop turn: one client socket keeps them in order,
//     and nothing the user types can land in between.
//   - An XWayland window also needs the X input focus, which the window manager sets over its own X connection while
//     the keys travel over Wayland, and the X server does not order the two. So each step waits on what the X server
//     reports through deskpilot's own X connection: the focus is the window before the first key, each key is down
//     or up before the next, and the focus is back before the user's keyboard is let go. The user's keys are held
//     meanwhile and replayed. A step that does not happen in time ends the sequence with an error.
//
// The reply is JSON, sent when the sequence has finished. `hyprctl deskpilot status` reports the plugin's version.

#include <hyprland/src/plugins/PluginAPI.hpp>

#include <hyprland/src/desktop/state/FocusState.hpp>
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
#include <hyprland/src/managers/XWaylandManager.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopManager.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopTimer.hpp>
#include <hyprland/src/managers/input/InputManager.hpp>
#include <hyprland/src/pointer/PointerManager.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/xwayland/XSurface.hpp>

#include <xcb/xcb.h>
#include <xkbcommon/xkbcommon.h>

#include <charconv>
#include <chrono>
#include <cstdlib>
#include <dlfcn.h>
#include <format>
#include <functional>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

// The hash of this source, set by the Makefile, so `deskpilot plugin load` can tell a stale build from the current one.
#ifndef DESKPILOT_SOURCE
#define DESKPILOT_SOURCE "unknown"
#endif
constexpr const char* VERSION = DESKPILOT_SOURCE;

inline HANDLE PHANDLE = nullptr;

namespace {
    using MoveFn   = void (*)(CInputManager*, IPointer::SMotionEvent);
    using ButtonFn = void (*)(CInputManager*, IPointer::SButtonEvent, SP<IPointer>);
    using WheelFn  = void (*)(CInputManager*, IPointer::SAxisEvent, SP<IPointer>);
    using KeyFn    = void (*)(CInputManager*, const IKeyboard::SKeyEvent&, SP<IKeyboard>);
    using ModFn    = void (*)(CInputManager*, SP<IKeyboard>);

    // How long an XWayland key step may take to show in the X server before the sequence gives up.
    constexpr auto X_WAIT = std::chrono::milliseconds(500);

    // Gaps between the steps of a sequence. A client that reads input once per frame (a game) still sees each step.
    constexpr auto SETTLE  = std::chrono::milliseconds(40);
    constexpr auto HOLD    = std::chrono::milliseconds(60);
    constexpr auto BETWEEN = std::chrono::milliseconds(80);

    struct SOp {
        uintptr_t                            window = 0;
        Vector2D                             target;
        uint32_t                             button = 0;
        int                                  count  = 0;
        int                                  scroll = 0; // wheel notches, positive up; 0 for a click
        Vector2D                             saved;
        Vector2D                             heldMotion;
        bool                                 hadFocus = false;
        PHLWINDOWREF                         focusWindow;
        WP<CWLSurfaceResource>               focusSurface;
        std::vector<std::function<void()>>   heldInput;
        std::vector<std::function<void()>>   steps;
        size_t                               next = 0;
        SP<CEventLoopTimer>                  timer;
        SP<CPromiseResolver<std::string>>    reply;
        std::string                          error;
    };

    CFunctionHook*       g_moveHook   = nullptr;
    CFunctionHook*       g_buttonHook = nullptr;
    CFunctionHook*       g_wheelHook  = nullptr;
    CFunctionHook*       g_keyHook    = nullptr;
    CFunctionHook*       g_modHook    = nullptr;
    std::optional<SOp>   g_op;
    bool                 g_injecting = false;

    struct SKeyOp {
        enum ePhase { AWAIT_FOCUS, KEY, AWAIT_KEY, AWAIT_RESTORE } phase = AWAIT_FOCUS;
        PHLWINDOWREF                             window;
        WP<CWLSurfaceResource>                   surface;
        uint32_t                                 xid = 0;
        WP<CWLSurfaceResource>                   lastSurface;
        PHLWINDOWREF                             lastWindow;
        uint32_t                                 lastXFocus = 0;
        std::vector<std::pair<uint32_t, bool>>   events;
        size_t                                   next = 0;
        std::vector<uint32_t>                    down; // keys this sequence holds down in the window
        xkb_state*                               xkb = nullptr;
        std::chrono::steady_clock::time_point    deadline;
        std::vector<std::function<void()>>       heldInput;
        SP<CEventLoopTimer>                      timer;
        SP<CPromiseResolver<std::string>>        reply;
        std::string                              error;
    };
    std::optional<SKeyOp> g_keyOp;
    xcb_connection_t*     g_x = nullptr;

    uint32_t now() {
        return Time::millis(Time::steadyNow());
    }

    void hkOnMouseMoved(CInputManager* self, IPointer::SMotionEvent e) {
        if (g_op) {
            g_op->heldMotion += e.delta;
            return;
        }
        reinterpret_cast<MoveFn>(g_moveHook->m_original)(self, e);
    }

    void hkOnMouseButton(CInputManager* self, IPointer::SButtonEvent e, SP<IPointer> mouse) {
        if (g_op && !g_injecting) {
            g_op->heldInput.emplace_back([self, e, mouse] { reinterpret_cast<ButtonFn>(g_buttonHook->m_original)(self, e, mouse); });
            return;
        }
        reinterpret_cast<ButtonFn>(g_buttonHook->m_original)(self, e, mouse);
    }

    void hkOnMouseWheel(CInputManager* self, IPointer::SAxisEvent e, SP<IPointer> pointer) {
        if (g_op && !g_injecting) {
            g_op->heldInput.emplace_back([self, e, pointer] { reinterpret_cast<WheelFn>(g_wheelHook->m_original)(self, e, pointer); });
            return;
        }
        reinterpret_cast<WheelFn>(g_wheelHook->m_original)(self, e, pointer);
    }

    void hkOnKeyboardKey(CInputManager* self, const IKeyboard::SKeyEvent& e, SP<IKeyboard> keyboard) {
        if (g_keyOp) {
            g_keyOp->heldInput.emplace_back([self, e, keyboard] { reinterpret_cast<KeyFn>(g_keyHook->m_original)(self, e, keyboard); });
            return;
        }
        reinterpret_cast<KeyFn>(g_keyHook->m_original)(self, e, keyboard);
    }

    void hkOnKeyboardMod(CInputManager* self, SP<IKeyboard> keyboard) {
        if (g_keyOp) {
            g_keyOp->heldInput.emplace_back([self, keyboard] { reinterpret_cast<ModFn>(g_modHook->m_original)(self, keyboard); });
            return;
        }
        reinterpret_cast<ModFn>(g_modHook->m_original)(self, keyboard);
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

    PHLWINDOW windowUnderPointer() {
        const auto SURFACE = g_pSeatManager->m_state.pointerFocus.lock();
        if (!SURFACE)
            return nullptr;
        const auto HLSURFACE = Desktop::View::CWLSurface::fromResource(SURFACE);
        return HLSURFACE ? Desktop::View::CWindow::fromView(HLSURFACE->view()) : nullptr;
    }

    void inject(const std::function<void()>& fn) {
        g_injecting = true;
        fn();
        g_injecting = false;
    }

    void finish() {
        auto op = std::move(*g_op);
        g_op.reset();
        if (op.timer) {
            op.timer->cancel();
            g_pEventLoopManager->removeTimer(op.timer);
        }

        Pointer::mgr()->warpTo(op.saved + op.heldMotion);
        g_pInputManager->simulateMouseMovement();
        g_pSeatManager->sendPointerFrame();
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

        const auto POS = op.target;
        if (op.error.empty())
            op.reply->resolve(std::format(R"({{"ok":true,"x":{},"y":{}}})", POS.x, POS.y));
        else
            op.reply->resolve(std::format(R"({{"ok":false,"error":{}}})", jsonString(op.error)));
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

    // The target must be the window under the pointer before anything is sent to it.
    bool targetUnderPointer() {
        const auto UNDER = windowUnderPointer();
        if (UNDER && reinterpret_cast<uintptr_t>(UNDER.get()) == g_op->window)
            return true;
        g_op->error = UNDER ? std::format("window 0x{:x} ({}) is on top at {},{}", reinterpret_cast<uintptr_t>(UNDER.get()), UNDER->metadata().title(),
                                          g_op->target.x, g_op->target.y) :
                              std::format("no window under {},{}", g_op->target.x, g_op->target.y);
        return false;
    }

    // Each step runs on its own timer tick; a step that fails sets the error and the next tick restores the user.
    void buildSteps(SOp& op) {
        op.steps.push_back([] {
            inject([] {
                Pointer::mgr()->warpTo(g_op->target);
                g_pInputManager->simulateMouseMovement();
                // Xwayland applies pointer events per frame: without one, the press arrives before the motion.
                g_pSeatManager->sendPointerFrame();
            });
            targetUnderPointer();
        });
        if (op.scroll) {
            for (int i = 0; i < std::abs(op.scroll); ++i) {
                op.steps.push_back([] {
                    // A wheel notch is delta 15 with discrete 120 (value120); negative is up in Wayland.
                    const int SIGN = g_op->scroll > 0 ? -1 : 1;
                    if (!targetUnderPointer())
                        return;
                    inject([SIGN] {
                        g_pInputManager->onMouseWheel(IPointer::SAxisEvent{.timeMs = now(), .delta = 15.0 * SIGN, .deltaDiscrete = 120 * SIGN});
                        g_pSeatManager->sendPointerFrame();
                    });
                });
            }
            return;
        }
        for (int i = 0; i < op.count; ++i) {
            for (auto state : {WL_POINTER_BUTTON_STATE_PRESSED, WL_POINTER_BUTTON_STATE_RELEASED}) {
                op.steps.push_back([state] {
                    if (state == WL_POINTER_BUTTON_STATE_PRESSED && !targetUnderPointer())
                        return;
                    inject([state] {
                        g_pInputManager->onMouseButton(IPointer::SButtonEvent{.timeMs = now(), .button = g_op->button, .state = state}, nullptr);
                        g_pSeatManager->sendPointerFrame();
                    });
                });
            }
        }
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

    IPC::Socket1::SResponse refuse(const std::string& why) {
        return IPC::Socket1::SResponse(std::format(R"({{"ok":false,"error":{}}})", jsonString(why)));
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

    void sendMods(xkb_state* xkb) {
        g_pSeatManager->sendKeyboardMods(xkb_state_serialize_mods(xkb, XKB_STATE_MODS_DEPRESSED), xkb_state_serialize_mods(xkb, XKB_STATE_MODS_LATCHED),
                                         xkb_state_serialize_mods(xkb, XKB_STATE_MODS_LOCKED), xkb_state_serialize_layout(xkb, XKB_STATE_LAYOUT_EFFECTIVE));
    }

    // One key to the surface with keyboard focus, then the modifiers it leads to (wl_keyboard: modifiers follow the
    // key that changed them).
    void sendKey(xkb_state* xkb, std::vector<uint32_t>& down, uint32_t code, bool press) {
        g_pSeatManager->sendKeyboardKey(now(), code, press ? WL_KEYBOARD_KEY_STATE_PRESSED : WL_KEYBOARD_KEY_STATE_RELEASED);
        xkb_state_update_key(xkb, code + 8, press ? XKB_KEY_DOWN : XKB_KEY_UP);
        sendMods(xkb);
        if (press)
            down.push_back(code);
        else
            std::erase(down, code);
    }

    void releaseAll(xkb_state* xkb, std::vector<uint32_t>& down) {
        while (!down.empty())
            sendKey(xkb, down, down.back(), false);
    }

    std::string okReply() {
        return R"({"ok":true})";
    }

    std::string errorReply(const std::string& why) {
        return std::format(R"({{"ok":false,"error":{}}})", jsonString(why));
    }

    // A Wayland window, or one that already has keyboard focus: the whole sequence in this event-loop turn.
    std::string keysNow(PHLWINDOW window, const std::vector<std::pair<uint32_t, bool>>& events, const SP<IKeyboard>& keyboard) {
        const auto SURFACE = window->wlSurface()->resource();
        const auto LAST    = g_pSeatManager->m_state.keyboardFocus.lock();
        const bool MOVE    = SURFACE != LAST;
        if (MOVE) {
            g_pSeatManager->setKeyboardFocus(SURFACE);
            if (g_pSeatManager->m_state.keyboardFocus.lock() != SURFACE) {
                g_pSeatManager->setKeyboardFocus(LAST);
                return errorReply("the window cannot take keyboard focus now (a lock screen or a grab holds it)");
            }
        }
        auto*                 xkb = scratchState(keyboard);
        std::vector<uint32_t> down;
        sendMods(xkb);
        for (const auto& [code, press] : events)
            sendKey(xkb, down, code, press);
        releaseAll(xkb, down);
        xkb_state_unref(xkb);
        if (MOVE)
            g_pSeatManager->setKeyboardFocus(LAST);
        else
            g_pSeatManager->sendKeyboardMods(keyboard->m_modifiersState.depressed, keyboard->m_modifiersState.latched, keyboard->m_modifiersState.locked,
                                             keyboard->m_modifiersState.group);
        return okReply();
    }

    void finishKeys() {
        auto op = std::move(*g_keyOp);
        g_keyOp.reset();
        if (op.timer) {
            op.timer->cancel();
            g_pEventLoopManager->removeTimer(op.timer);
        }
        if (op.xkb)
            xkb_state_unref(op.xkb);
        for (auto& replay : op.heldInput)
            replay();
        op.reply->resolve(op.error.empty() ? okReply() : errorReply(op.error));
    }

    // Keyboard focus back to the surface that had it, and the X focus back to the X window that had it.
    void restoreKeys(SKeyOp& op) {
        if (g_pSeatManager->m_state.keyboardFocus.lock() == op.surface.lock())
            releaseAll(op.xkb, op.down);
        g_pSeatManager->setKeyboardFocus(op.lastSurface.lock());
        const auto LASTWINDOW = op.lastWindow.lock();
        if (LASTWINDOW && LASTWINDOW->backend().isX11())
            g_pXWaylandManager->activateSurface(LASTWINDOW->wlSurface()->resource(), true);
        else if (LASTWINDOW)
            g_pXWaylandManager->activateSurface(op.surface.lock(), false);
        op.phase    = SKeyOp::AWAIT_RESTORE;
        op.deadline = std::chrono::steady_clock::now() + X_WAIT;
    }

    void keyTick(SP<CEventLoopTimer>, void*) {
        if (!g_keyOp)
            return;
        auto&      op      = *g_keyOp;
        const bool EXPIRED = std::chrono::steady_clock::now() > op.deadline;
        switch (op.phase) {
            case SKeyOp::AWAIT_FOCUS:
                if (xFocus() == op.xid) {
                    sendMods(op.xkb);
                    op.phase = SKeyOp::KEY;
                } else if (EXPIRED) {
                    op.error = "the X window did not take the X input focus";
                    restoreKeys(op);
                }
                break;
            case SKeyOp::KEY:
                if (op.next == op.events.size() || !op.window.lock() || g_pSeatManager->m_state.keyboardFocus.lock() != op.surface.lock()) {
                    if (op.next != op.events.size())
                        op.error = "the window closed or lost keyboard focus mid-sequence";
                    restoreKeys(op);
                    break;
                }
                sendKey(op.xkb, op.down, op.events[op.next].first, op.events[op.next].second);
                op.phase    = SKeyOp::AWAIT_KEY;
                op.deadline = std::chrono::steady_clock::now() + X_WAIT;
                break;
            case SKeyOp::AWAIT_KEY:
                if (xKeyDown(op.events[op.next].first) == op.events[op.next].second) {
                    op.next++;
                    op.phase = SKeyOp::KEY;
                } else if (EXPIRED) {
                    op.error = std::format("key {} did not reach the X server", op.events[op.next].first);
                    restoreKeys(op);
                }
                break;
            case SKeyOp::AWAIT_RESTORE:
                if (!op.lastWindow.lock() || xFocus() == op.lastXFocus) {
                    finishKeys();
                    return;
                }
                if (EXPIRED) {
                    op.error = op.error.empty() ? "the X input focus did not return to the window that had it" : op.error;
                    finishKeys();
                    return;
                }
                break;
        }
        if (g_keyOp)
            g_keyOp->timer->updateTimeout(std::chrono::milliseconds(1));
    }

    IPC::Socket1::SResponse keysCommand(const std::vector<std::string>& args, const std::string& command) {
        if (args.size() < 4)
            return refuse("usage: deskpilot keys <window> <+code|-code>...");
        const auto WINDOW = Desktop::viewState()->query().selector("address:" + args[2]).runWindow();
        if (!WINDOW || !WINDOW->mapped())
            return refuse("no mapped window " + args[2]);
        const auto KEYBOARD = g_pSeatManager->m_keyboard.lock();
        if (!KEYBOARD || !KEYBOARD->m_xkbKeymap)
            return refuse("no keyboard");

        std::vector<std::pair<uint32_t, bool>> events;
        for (size_t i = 3; i < args.size(); ++i) {
            const auto CODE = args[i].size() > 1 ? number(args[i].substr(1)) : std::nullopt;
            if ((args[i][0] != '+' && args[i][0] != '-') || !CODE || *CODE < 1 || *CODE > 247)
                return refuse("bad key event '" + args[i] + "' in: " + command);
            events.emplace_back(static_cast<uint32_t>(*CODE), args[i][0] == '+');
        }

        const auto SURFACE = WINDOW->wlSurface()->resource();
        if (!WINDOW->backend().isX11() || g_pSeatManager->m_state.keyboardFocus.lock() == SURFACE)
            return IPC::Socket1::SResponse(keysNow(WINDOW, events, KEYBOARD));

        const auto XID = xidOf(WINDOW);
        if (!XID || !xOpen())
            return refuse("cannot reach the X server for this XWayland window");

        SKeyOp op;
        op.window      = WINDOW;
        op.surface     = SURFACE;
        op.xid         = XID;
        op.lastSurface = g_pSeatManager->m_state.keyboardFocus;
        op.lastWindow  = Desktop::focusState()->window();
        op.lastXFocus  = xFocus();
        op.events      = std::move(events);
        op.xkb         = scratchState(KEYBOARD);
        op.deadline    = std::chrono::steady_clock::now() + X_WAIT;

        g_pSeatManager->setKeyboardFocus(SURFACE);
        if (g_pSeatManager->m_state.keyboardFocus.lock() != SURFACE) {
            g_pSeatManager->setKeyboardFocus(op.lastSurface.lock());
            xkb_state_unref(op.xkb);
            return refuse("the window cannot take keyboard focus now (a lock screen or a grab holds it)");
        }
        g_pXWaylandManager->activateSurface(SURFACE, true);

        auto promise = CPromise<std::string>::make([&op](SP<CPromiseResolver<std::string>> resolver) { op.reply = resolver; });
        g_keyOp.emplace(std::move(op));
        g_keyOp->timer = makeShared<CEventLoopTimer>(std::chrono::milliseconds(1), keyTick, nullptr);
        g_pEventLoopManager->addTimer(g_keyOp->timer);
        return IPC::Socket1::SResponse(promise);
    }

    IPC::Socket1::SResponse onCommand(const IPC::Socket1::SRequest& req) {
        std::istringstream       in(req.command);
        std::vector<std::string> args;
        for (std::string word; in >> word;)
            args.push_back(word);
        // args[0] is "deskpilot".
        if (args.size() >= 2 && args[1] == "status")
            return IPC::Socket1::SResponse(std::format(R"({{"ok":true,"version":{},"path":{},"busy":{}}})", jsonString(VERSION), jsonString(ownPath()),
                                                       g_op || g_keyOp ? "true" : "false"));
        if (g_op || g_keyOp)
            return refuse("busy: another deskpilot action is running");
        if (args.size() >= 2 && args[1] == "keys")
            return keysCommand(args, req.command);

        const bool CLICK  = args.size() == 7 && args[1] == "click";
        const bool SCROLL = args.size() == 6 && args[1] == "scroll";
        if (!CLICK && !SCROLL)
            return refuse("usage: deskpilot click <window> <x> <y> <button> <count> | scroll <window> <x> <y> <dy> | keys <window> <+code|-code>... | status");

        const auto WINDOW = address(args[2]);
        const auto X = number(args[3]), Y = number(args[4]), A = number(args[5]);
        if (!WINDOW || !X || !Y || !A)
            return refuse("bad arguments: " + req.command);

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
        op.saved         = g_pInputManager->getMouseCoordsInternal();
        op.focusWindow   = Desktop::focusState()->window();
        op.focusSurface  = Desktop::focusState()->surface();
        op.hadFocus      = op.focusWindow || op.focusSurface;
        buildSteps(op);

        // make() calls its function at once, so the resolver is ours before the sequence starts.
        auto promise = CPromise<std::string>::make([&op](SP<CPromiseResolver<std::string>> resolver) { op.reply = resolver; });
        g_op.emplace(std::move(op));

        g_pHyprRenderer->setCursorHidden(true);
        g_op->timer = makeShared<CEventLoopTimer>(std::chrono::milliseconds(1), runNext, nullptr);
        g_pEventLoopManager->addTimer(g_op->timer);
        return IPC::Socket1::SResponse(promise);
    }
}

static CFunctionHook* hook(HANDLE handle, const std::string& name, void* replacement) {
    for (const auto& fn : HyprlandAPI::findFunctionsByName(handle, name)) {
        if (fn.demangled.contains("CInputManager::" + name)) {
            const auto HOOK = HyprlandAPI::createFunctionHook(handle, fn.address, replacement);
            if (HOOK && HOOK->hook())
                return HOOK;
            break;
        }
    }
    throw std::runtime_error("deskpilot: could not hook CInputManager::" + name);
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

    g_moveHook   = hook(handle, "onMouseMoved", reinterpret_cast<void*>(&hkOnMouseMoved));
    g_buttonHook = hook(handle, "onMouseButton", reinterpret_cast<void*>(&hkOnMouseButton));
    g_wheelHook  = hook(handle, "onMouseWheel", reinterpret_cast<void*>(&hkOnMouseWheel));
    g_keyHook    = hook(handle, "onKeyboardKey", reinterpret_cast<void*>(&hkOnKeyboardKey));
    g_modHook    = hook(handle, "onKeyboardMod", reinterpret_cast<void*>(&hkOnKeyboardMod));

    HyprlandAPI::registerHyprCtlCommand(handle, IPC::Socket1::SCommand{.name = "deskpilot", .match = IPC::Socket1::COMMAND_MATCH_PREFIX, .handler = onCommand});
    return {"deskpilot", "Clicks, scrolls and keys for agents that leave the user's pointer and focus in place", "cjber", VERSION};
}

APICALL EXPORT void PLUGIN_EXIT() {
    if (g_op)
        finish();
    if (g_keyOp) {
        restoreKeys(*g_keyOp);
        finishKeys();
    }
    if (g_x)
        xcb_disconnect(g_x);
}
