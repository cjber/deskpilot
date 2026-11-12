#include "xdg-shell-client-protocol.h"
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <wayland-client.h>
static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct wl_seat *seat;
static struct wl_keyboard *keyboard;
static struct xdg_wm_base *base;
static int configured;
static struct wl_pointer *pointer;
static uint32_t click_serial;
static struct xdg_surface *main_xdg;
static struct wl_surface *popup_surface;
static int popup_started;
static FILE *events;
static void keymap(void *d, struct wl_keyboard *k, uint32_t f, int32_t fd,
                   uint32_t s) {
  close(fd);
}
static void enter(void *d, struct wl_keyboard *k, uint32_t serial,
                  struct wl_surface *s, struct wl_array *a) {
  fprintf(events, "ENTER\n");
  fflush(events);
}
static void leave(void *d, struct wl_keyboard *k, uint32_t serial,
                  struct wl_surface *s) {
  fprintf(events, "LEAVE\n");
  fflush(events);
}
static void key(void *d, struct wl_keyboard *k, uint32_t serial, uint32_t time,
                uint32_t code, uint32_t state) {
  if (state && getenv("PROBE_USER_KEY"))
    click_serial = serial;
  fprintf(events, "KEY %u %u\n", code, state);
  fflush(events);
}
static void mods(void *d, struct wl_keyboard *k, uint32_t a, uint32_t b,
                 uint32_t c, uint32_t e, uint32_t f) {}
static void repeat(void *d, struct wl_keyboard *k, int32_t rate,
                   int32_t delay) {}
static const struct wl_keyboard_listener keys = {keymap, enter, leave,
                                                 key,    mods,  repeat};
static void pointer_enter(void *d, struct wl_pointer *p, uint32_t serial,
                          struct wl_surface *s, wl_fixed_t x, wl_fixed_t y) {}
static void pointer_leave(void *d, struct wl_pointer *p, uint32_t serial,
                          struct wl_surface *s) {}
static void motion(void *d, struct wl_pointer *p, uint32_t t, wl_fixed_t x,
                   wl_fixed_t y) {}
static void button(void *d, struct wl_pointer *p, uint32_t serial, uint32_t t,
                   uint32_t code, uint32_t state) {
  if (state) {
    click_serial = serial;
    fprintf(events, "CLICK %u\n", serial);
    fflush(events);
  }
}
static void axis(void *d, struct wl_pointer *p, uint32_t t, uint32_t a,
                 wl_fixed_t v) {}
static void frame(void *d, struct wl_pointer *p) {}
static void axis_source(void *d, struct wl_pointer *p, uint32_t s) {}
static void axis_stop(void *d, struct wl_pointer *p, uint32_t t, uint32_t a) {}
static void axis_discrete(void *d, struct wl_pointer *p, uint32_t a,
                          int32_t v) {}
static const struct wl_pointer_listener pointers = {
    pointer_enter, pointer_leave, motion,    button,       axis,
    frame,         axis_source,   axis_stop, axis_discrete};
static void caps(void *d, struct wl_seat *s, uint32_t c) {
  if (!pointer && (c & WL_SEAT_CAPABILITY_POINTER)) {
    pointer = wl_seat_get_pointer(s);
    wl_pointer_add_listener(pointer, &pointers, NULL);
  }
  if (!keyboard && (c & WL_SEAT_CAPABILITY_KEYBOARD)) {
    keyboard = wl_seat_get_keyboard(s);
    wl_keyboard_add_listener(keyboard, &keys, NULL);
  }
}
static void seat_name(void *d, struct wl_seat *s, const char *n) {}
static const struct wl_seat_listener seats = {caps, seat_name};
static void global(void *d, struct wl_registry *r, uint32_t n, const char *i,
                   uint32_t v) {
  if (!strcmp(i, "wl_compositor"))
    compositor = wl_registry_bind(r, n, &wl_compositor_interface, 4);
  else if (!strcmp(i, "wl_shm"))
    shm = wl_registry_bind(r, n, &wl_shm_interface, 1);
  else if (!strcmp(i, "wl_seat") && !seat) {
    seat = wl_registry_bind(r, n, &wl_seat_interface, 7);
    wl_seat_add_listener(seat, &seats, NULL);
  } else if (!strcmp(i, "xdg_wm_base"))
    base = wl_registry_bind(r, n, &xdg_wm_base_interface, 1);
}
static void removed(void *d, struct wl_registry *r, uint32_t n) {}
static const struct wl_registry_listener registry = {global, removed};
static void ping(void *d, struct xdg_wm_base *b, uint32_t serial) {
  xdg_wm_base_pong(b, serial);
}
static void draw(struct wl_surface *s, int width, int height) {
  int size = width * height * 4;
  int fd = syscall(SYS_memfd_create, "popup-probe", 0);
  ftruncate(fd, size);
  uint32_t *p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  for (int i = 0; i < width * height; i++)
    p[i] = 0xff224466;
  struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
  struct wl_buffer *buffer = wl_shm_pool_create_buffer(
      pool, 0, width, height, width * 4, WL_SHM_FORMAT_XRGB8888);
  wl_surface_attach(s, buffer, 0, 0);
  wl_surface_damage_buffer(s, 0, 0, width, height);
  wl_surface_commit(s);
  munmap(p, size);
  close(fd);
  wl_shm_pool_destroy(pool);
}
static void popup_configure(void *d, struct xdg_surface *s, uint32_t serial) {
  xdg_surface_ack_configure(s, serial);
  draw(popup_surface, 120, 100);
  fprintf(events, "POPUP\n");
  fflush(events);
}
static const struct xdg_surface_listener popup_surfaces = {popup_configure};
static void open_popup(void) {
  popup_started = 1;
  popup_surface = wl_compositor_create_surface(compositor);
  struct xdg_surface *x = xdg_wm_base_get_xdg_surface(base, popup_surface);
  xdg_surface_add_listener(x, &popup_surfaces, NULL);
  struct xdg_positioner *p = xdg_wm_base_create_positioner(base);
  xdg_positioner_set_size(p, 120, 100);
  xdg_positioner_set_anchor_rect(p, 100, 50, 1, 1);
  xdg_positioner_set_anchor(p, XDG_POSITIONER_ANCHOR_TOP_LEFT);
  xdg_positioner_set_gravity(p, XDG_POSITIONER_GRAVITY_BOTTOM_RIGHT);
  struct xdg_popup *popup = xdg_surface_get_popup(x, main_xdg, p);
  xdg_popup_grab(popup, seat, click_serial);
  wl_surface_commit(popup_surface);
  xdg_positioner_destroy(p);
}
static const struct xdg_wm_base_listener bases = {ping};
static void configure(void *d, struct xdg_surface *s, uint32_t serial) {
  xdg_surface_ack_configure(s, serial);
  configured = 1;
}
static const struct xdg_surface_listener surfaces = {configure};
static void top_configure(void *d, struct xdg_toplevel *t, int32_t w, int32_t h,
                          struct wl_array *a) {}
static void top_close(void *d, struct xdg_toplevel *t) { exit(0); }
static const struct xdg_toplevel_listener tops = {top_configure, top_close};
int main(void) {
  const char *path = getenv("PROBE_EVENTS");
  if (!path)
    return 2;
  int log_fd =
      open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (log_fd < 0)
    return 2;
  if (fchmod(log_fd, 0600) < 0 || !(events = fdopen(log_fd, "w"))) {
    close(log_fd);
    return 2;
  }
  struct wl_display *display = wl_display_connect(NULL);
  if (!display)
    return 3;
  struct wl_registry *r = wl_display_get_registry(display);
  wl_registry_add_listener(r, &registry, NULL);
  wl_display_roundtrip(display);
  wl_display_roundtrip(display);
  if (!compositor || !shm || !base || !keyboard)
    return 4;
  xdg_wm_base_add_listener(base, &bases, NULL);
  struct wl_surface *s = wl_compositor_create_surface(compositor);
  struct xdg_surface *x = xdg_wm_base_get_xdg_surface(base, s);
  xdg_surface_add_listener(x, &surfaces, NULL);
  main_xdg = x;
  struct xdg_toplevel *t = xdg_surface_get_toplevel(x);
  xdg_toplevel_add_listener(t, &tops, NULL);
  xdg_toplevel_set_title(t, "Popup serial probe");
  wl_surface_commit(s);
  while (!configured)
    if (wl_display_dispatch(display) < 0)
      return 5;
  draw(s, 400, 300);
  while (1) {
    wl_display_dispatch_pending(display);
    wl_display_flush(display);
    if (click_serial && !popup_started &&
        access(getenv("PROBE_GRAB_FILE"), F_OK) == 0)
      open_popup();
    struct pollfd fd = {wl_display_get_fd(display), POLLIN, 0};
    if (poll(&fd, 1, 50) < 0)
      return 6;
    if (fd.revents && wl_display_dispatch(display) < 0)
      break;
  }
  return 0;
}
