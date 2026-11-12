// Winelib calibration client. Build with winegcc -m64 -mwindows, then pass
// DESKPILOT_CALIBRATION_LOG as a Unix path in its owned private session.
#include <stdio.h>
#include <stdlib.h>
#include <windows.h>

static FILE *events;
static LRESULT CALLBACK procedure(HWND window, UINT message, WPARAM w,
                                  LPARAM l) {
  if (message == WM_CHAR)
    fprintf(events, "CHAR %u\n", (unsigned)w);
  if (message == WM_LBUTTONDOWN || message == WM_LBUTTONUP)
    fprintf(events, "%s\n", message == WM_LBUTTONDOWN ? "DOWN" : "UP");
  if (message == WM_INPUT) {
    RAWINPUT input;
    UINT size = sizeof(input);
    if (GetRawInputData((HRAWINPUT)l, RID_INPUT, &input, &size,
                        sizeof(RAWINPUTHEADER)) != (UINT)-1 &&
        input.header.dwType == RIM_TYPEMOUSE) {
      if (input.data.mouse.usButtonFlags & RI_MOUSE_LEFT_BUTTON_DOWN)
        fprintf(events, "RAW_DOWN\n");
      if (input.data.mouse.usButtonFlags & RI_MOUSE_LEFT_BUTTON_UP)
        fprintf(events, "RAW_UP\n");
    }
  }
  fflush(events);
  if (message == WM_DESTROY)
    PostQuitMessage(0);
  return DefWindowProcW(window, message, w, l);
}
int WINAPI WinMain(HINSTANCE instance, HINSTANCE previous, LPSTR args,
                   int show) {
  const char *path = getenv("DESKPILOT_CALIBRATION_LOG");
  if (!path || !(events = fopen(path, "w")))
    return 2;
  WNDCLASSW cls = {0};
  cls.lpfnWndProc = procedure;
  cls.hInstance = instance;
  cls.lpszClassName = L"DeskpilotWineCalibration";
  if (!RegisterClassW(&cls))
    return 3;
  HWND window =
      CreateWindowW(cls.lpszClassName, L"Deskpilot Wine calibration",
                    WS_OVERLAPPEDWINDOW, 0, 0, 600, 400, 0, 0, instance, 0);
  if (!window)
    return 4;
  RAWINPUTDEVICE devices[2] = {{1, 2, RIDEV_INPUTSINK, window},
                               {1, 6, RIDEV_INPUTSINK, window}};
  if (!RegisterRawInputDevices(devices, 2, sizeof(RAWINPUTDEVICE)))
    return 5;
  ShowWindow(window, show);
  SetFocus(window);
  MSG message;
  while (GetMessageW(&message, 0, 0, 0) > 0) {
    TranslateMessage(&message);
    DispatchMessageW(&message);
  }
  fclose(events);
  return 0;
}
