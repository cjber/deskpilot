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
// The reply is JSON, sent when the sequence has finished. `hyprctl deskpilot status` reports the plugin's version.

#include <hyprland/src/plugins/PluginAPI.hpp>

#include <hyprland/src/desktop/state/FocusState.hpp>
#include <hyprland/src/desktop/view/WLSurface.hpp>
#include <hyprland/src/desktop/view/window/Window.hpp>
#include <hyprland/src/helpers/defer/Promise.hpp>
#include <hyprland/src/helpers/time/Time.hpp>
#include <hyprland/src/ipc/s1/S1.hpp>
#include <hyprland/src/managers/SeatManager.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopManager.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopTimer.hpp>
#include <hyprland/src/managers/input/InputManager.hpp>
#include <hyprland/src/pointer/PointerManager.hpp>
#include <hyprland/src/render/Renderer.hpp>

#include <charconv>
#include <format>
#include <functional>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

constexpr const char* VERSION = "1";

inline HANDLE PHANDLE = nullptr;

namespace {
    using MoveFn   = void (*)(CInputManager*, IPointer::SMotionEvent);
    using ButtonFn = void (*)(CInputManager*, IPointer::SButtonEvent, SP<IPointer>);
    using WheelFn  = void (*)(CInputManager*, IPointer::SAxisEvent, SP<IPointer>);

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
    std::optional<SOp>   g_op;
    bool                 g_injecting = false;

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

    IPC::Socket1::SResponse refuse(const std::string& why) {
        return IPC::Socket1::SResponse(std::format(R"({{"ok":false,"error":{}}})", jsonString(why)));
    }

    IPC::Socket1::SResponse onCommand(const IPC::Socket1::SRequest& req) {
        std::istringstream       in(req.command);
        std::vector<std::string> args;
        for (std::string word; in >> word;)
            args.push_back(word);
        // args[0] is "deskpilot".
        if (args.size() >= 2 && args[1] == "status")
            return IPC::Socket1::SResponse(std::format(R"({{"ok":true,"version":{},"busy":{}}})", jsonString(VERSION), g_op ? "true" : "false"));

        const bool CLICK  = args.size() == 7 && args[1] == "click";
        const bool SCROLL = args.size() == 6 && args[1] == "scroll";
        if (!CLICK && !SCROLL)
            return refuse("usage: deskpilot click <window> <x> <y> <button> <count> | scroll <window> <x> <y> <dy> | status");
        if (g_op)
            return refuse("busy: another deskpilot action is running");

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
    if (std::string(__hyprland_api_get_hash()) != __hyprland_api_get_client_hash())
        throw std::runtime_error("deskpilot: built for another Hyprland version, rebuild it with `deskpilot plugin build`");

    g_moveHook   = hook(handle, "onMouseMoved", reinterpret_cast<void*>(&hkOnMouseMoved));
    g_buttonHook = hook(handle, "onMouseButton", reinterpret_cast<void*>(&hkOnMouseButton));
    g_wheelHook  = hook(handle, "onMouseWheel", reinterpret_cast<void*>(&hkOnMouseWheel));

    HyprlandAPI::registerHyprCtlCommand(handle, IPC::Socket1::SCommand{.name = "deskpilot", .match = IPC::Socket1::COMMAND_MATCH_PREFIX, .handler = onCommand});
    return {"deskpilot", "Clicks and scrolls for agents that leave the user's pointer in place", "cjber", VERSION};
}

APICALL EXPORT void PLUGIN_EXIT() {
    if (g_op)
        finish();
}
