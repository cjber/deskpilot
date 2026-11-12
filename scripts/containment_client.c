#include "pointer-constraints-unstable-v1-client-protocol.h"
#include "relative-pointer-unstable-v1-client-protocol.h"
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"
#include "xdg-shell-client-protocol.h"
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <wayland-client.h>

static struct wl_seat *seat, *user_seat;
static struct wl_pointer *pointer;
static struct wl_compositor *compositor;
static struct wl_surface *surface;
static struct wl_shm *shm;
static struct xdg_wm_base *xdg_base;
static struct xdg_surface *xdg_surface;
static struct xdg_toplevel *toplevel;
static int configured;
static struct zwp_relative_pointer_manager_v1 *relative_manager;
static struct zwp_pointer_constraints_v1 *constraints;
static struct zwlr_virtual_pointer_manager_v1 *virtual_manager;
static int relative_events, locked_events, confined_events, protocol_errors,
    agent_ready;
static void relative(void *d, struct zwp_relative_pointer_v1 *o, uint32_t h,
                     uint32_t l, wl_fixed_t dx, wl_fixed_t dy, wl_fixed_t ux,
                     wl_fixed_t uy) {
  relative_events++;
}
static const struct zwp_relative_pointer_v1_listener relative_listener = {
    relative};
static void locked(void *d, struct zwp_locked_pointer_v1 *o) {
  locked_events++;
}
static void unlocked(void *d, struct zwp_locked_pointer_v1 *o) {}
static const struct zwp_locked_pointer_v1_listener locked_listener = {locked,
                                                                      unlocked};
static void confined(void *d, struct zwp_confined_pointer_v1 *o) {
  confined_events++;
}
static void unconfined(void *d, struct zwp_confined_pointer_v1 *o) {}
static const struct zwp_confined_pointer_v1_listener confined_listener = {
    confined, unconfined};
static void seat_caps(void *d, struct wl_seat *s, uint32_t caps) {
  if (agent_ready && s == seat && caps & WL_SEAT_CAPABILITY_POINTER && !pointer)
    pointer = wl_seat_get_pointer(s);
}
static void seat_name(void *d, struct wl_seat *s, const char *name) {
  if (!strcmp(name, "deskpilot")) {
    seat = s;
    agent_ready = 1;
    pointer = wl_seat_get_pointer(s);
  } else if (!strcmp(name, "Hyprland"))
    user_seat = s;
}
static const struct wl_seat_listener seat_listener = {seat_caps, seat_name};
static void global(void *d, struct wl_registry *r, uint32_t n, const char *name,
                   uint32_t v) {
  if (!strcmp(name, "wl_seat")) {
    struct wl_seat *s = wl_registry_bind(r, n, &wl_seat_interface, 7);
    wl_seat_add_listener(s, &seat_listener, NULL);
  } else if (!strcmp(name, "wl_compositor"))
    compositor = wl_registry_bind(r, n, &wl_compositor_interface, 4);
  else if (!strcmp(name, "wl_shm"))
    shm = wl_registry_bind(r, n, &wl_shm_interface, 1);
  else if (!strcmp(name, "xdg_wm_base"))
    xdg_base = wl_registry_bind(r, n, &xdg_wm_base_interface, 1);
  else if (!strcmp(name, "zwp_relative_pointer_manager_v1"))
    relative_manager =
        wl_registry_bind(r, n, &zwp_relative_pointer_manager_v1_interface, 1);
  else if (!strcmp(name, "zwp_pointer_constraints_v1"))
    constraints =
        wl_registry_bind(r, n, &zwp_pointer_constraints_v1_interface, 1);
  else if (!strcmp(name, "zwlr_virtual_pointer_manager_v1"))
    virtual_manager =
        wl_registry_bind(r, n, &zwlr_virtual_pointer_manager_v1_interface, 2);
}
static void removed(void *d, struct wl_registry *r, uint32_t n) {}
static const struct wl_registry_listener registry_listener = {global, removed};
static void ping(void *d, struct xdg_wm_base *b, uint32_t serial) {
  xdg_wm_base_pong(b, serial);
}
static const struct xdg_wm_base_listener base_listener = {ping};
static void xdg_configure(void *d, struct xdg_surface *s, uint32_t serial) {
  xdg_surface_ack_configure(s, serial);
  configured = 1;
}
static const struct xdg_surface_listener surface_listener = {xdg_configure};
static void top_configure(void *d, struct xdg_toplevel *t, int32_t w, int32_t h,
                          struct wl_array *states) {}
static void top_close(void *d, struct xdg_toplevel *t) {}
static const struct xdg_toplevel_listener top_listener = {top_configure,
                                                          top_close};
int main(int argc, char **argv) {
  int seconds = argc > 1 ? atoi(argv[1]) : 3;
  int negative = argc > 2;
  int duplicate = negative && !strcmp(argv[2], "constraints");
  struct wl_display *display = wl_display_connect(NULL);
  if (!display)
    return 2;
  struct wl_registry *registry = wl_display_get_registry(display);
  wl_registry_add_listener(registry, &registry_listener, NULL);
  wl_display_roundtrip(display);
  wl_display_roundtrip(display);
  if (negative) {
    if (!user_seat) {
      fprintf(stderr, "missing user seat for negative control\n");
      return 3;
    }
    seat = user_seat;
    pointer = wl_seat_get_pointer(seat);
  }
  if (!seat || !pointer || !compositor || !shm || !xdg_base ||
      !relative_manager || !constraints || !virtual_manager) {
    fprintf(stderr, "missing seat, xdg, shm or pointer protocols\n");
    return 3;
  }
  xdg_wm_base_add_listener(xdg_base, &base_listener, NULL);
  surface = wl_compositor_create_surface(compositor);
  xdg_surface = xdg_wm_base_get_xdg_surface(xdg_base, surface);
  xdg_surface_add_listener(xdg_surface, &surface_listener, NULL);
  toplevel = xdg_surface_get_toplevel(xdg_surface);
  xdg_toplevel_add_listener(toplevel, &top_listener, NULL);
  xdg_toplevel_set_title(toplevel, "deskpilot containment");
  wl_surface_commit(surface);
  while (!configured) {
    if (wl_display_dispatch(display) < 0)
      return 4;
  }
  int width = 400, height = 300, size = width * height * 4;
  int fd = syscall(SYS_memfd_create, "deskpilot-containment", 0);
  ftruncate(fd, size);
  uint32_t *pixels =
      mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  for (int i = 0; i < width * height; i++)
    pixels[i] = 0xff204060;
  struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
  struct wl_buffer *buffer = wl_shm_pool_create_buffer(
      pool, 0, width, height, width * 4, WL_SHM_FORMAT_XRGB8888);
  wl_surface_attach(surface, buffer, 0, 0);
  wl_surface_damage_buffer(surface, 0, 0, width, height);
  wl_surface_commit(surface);
  munmap(pixels, size);
  close(fd);
  wl_shm_pool_destroy(pool);
  struct zwp_relative_pointer_v1 *relative_pointer =
      zwp_relative_pointer_manager_v1_get_relative_pointer(relative_manager,
                                                           pointer);
  zwp_relative_pointer_v1_add_listener(relative_pointer, &relative_listener,
                                       NULL);
  struct zwp_locked_pointer_v1 *locked_pointer =
      zwp_pointer_constraints_v1_lock_pointer(constraints, surface, pointer,
                                              NULL, 1);
  zwp_locked_pointer_v1_add_listener(locked_pointer, &locked_listener, NULL);
  struct zwp_confined_pointer_v1 *confined_pointer = NULL;
  if (!negative || duplicate) {
    confined_pointer = zwp_pointer_constraints_v1_confine_pointer(
        constraints, surface, pointer, NULL, 1);
    zwp_confined_pointer_v1_add_listener(confined_pointer, &confined_listener,
                                         NULL);
  }
  wl_surface_commit(surface);
  wl_display_flush(display);
  puts("READY");
  fflush(stdout);
  struct zwlr_virtual_pointer_v1 *virtual_pointer =
      zwlr_virtual_pointer_manager_v1_create_virtual_pointer(virtual_manager,
                                                             NULL);
  for (int i = 0; i < seconds * 10; ++i) {
    struct pollfd pfd = {.fd = wl_display_get_fd(display), .events = POLLIN};
    wl_display_flush(display);
    if (virtual_pointer) {
      zwlr_virtual_pointer_v1_motion(virtual_pointer, 0, 20, 10);
      zwlr_virtual_pointer_v1_frame(virtual_pointer);
      wl_display_flush(display);
    }
    if (poll(&pfd, 1, 100) < 0) {
      protocol_errors++;
      break;
    }
    if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
      protocol_errors++;
      break;
    }
    if (pfd.revents & POLLIN && wl_display_dispatch(display) < 0) {
      protocol_errors++;
      break;
    }
    wl_display_dispatch_pending(display);
  }
  if (virtual_pointer)
    zwlr_virtual_pointer_v1_destroy(virtual_pointer);
  zwp_relative_pointer_v1_destroy(relative_pointer);
  zwp_locked_pointer_v1_destroy(locked_pointer);
  if (confined_pointer)
    zwp_confined_pointer_v1_destroy(confined_pointer);
  wl_surface_destroy(surface);
  wl_pointer_destroy(pointer);
  wl_seat_destroy(seat);
  if (wl_display_get_error(display))
    protocol_errors++;
  wl_display_disconnect(display);
  printf("relative_events=%d locked_events=%d confined_events=%d "
         "protocol_errors=%d\n",
         relative_events, locked_events, confined_events, protocol_errors);
  return relative_events || locked_events || confined_events || protocol_errors;
}
