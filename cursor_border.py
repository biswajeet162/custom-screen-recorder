"""
Cursor-centered aspect-ratio border overlay for Windows.
Draws a cyan rectangle (16:9 or 9:16) that follows the mouse, cursor at center.
Press Esc to quit.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import tkinter as tk
from ctypes import wintypes

user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
LWA_COLORKEY = 0x00000001
VK_ESCAPE = 0x1B
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
GA_ROOT = 2

# =============================================================================
# CONFIG — edit width / height (pixels) for each option
# =============================================================================

# Option 1 — 16:9 (widescreen)  → chosen when you press 1 in run.bat
# Sample sizes (width x height):
#   640 x 360    |  854 x 480    |  1280 x 720   (HD)
#   1600 x 900   |  1920 x 1080  (Full HD)       |  2560 x 1440  (QHD)
OPTION1_WIDTH = 1280
OPTION1_HEIGHT = 720

# Option 2 — 9:16 (vertical / portrait)  → chosen when you press 2 in run.bat
# Sample sizes (width x height):
#   360 x 640    |  480 x 854    |  720 x 1280   (HD vertical)
#   900 x 1600   |  1080 x 1920  (Full HD vertical) |  1440 x 2560  (QHD vertical)
OPTION2_WIDTH = 720
OPTION2_HEIGHT = 1280

# Border look
BORDER_WIDTH = 3          # line thickness in pixels
BORDER_COLOR = "#00FFFF"  # cyan

# =============================================================================

# Chroma-key fill (made invisible by Windows layered color-key)
KEY_COLOR = "#010101"
KEY_COLORREF = 0x00010101  # 0x00bbggrr for RGB(1,1,1)
UPDATE_MS = 8  # ~120 FPS — keeps cursor locked to box center


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def configured_size(ratio: str) -> tuple[int, int]:
    """Return (width, height) from the CONFIG block for the chosen option."""
    if ratio == "16:9":
        return int(OPTION1_WIDTH), int(OPTION1_HEIGHT)
    return int(OPTION2_WIDTH), int(OPTION2_HEIGHT)


def get_hwnd(root: tk.Tk) -> int:
    """Resolve the real top-level HWND Tk uses for the overlay window."""
    hwnd = int(root.winfo_id())
    ancestor = int(user32.GetAncestor(hwnd, GA_ROOT))
    if ancestor:
        return ancestor
    parent = int(user32.GetParent(hwnd))
    return parent or hwnd


def setup_layered_window(hwnd: int) -> None:
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    # Make KEY_COLOR fully transparent; cyan border stays opaque
    user32.SetLayeredWindowAttributes(hwnd, KEY_COLORREF, 0, LWA_COLORKEY)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cyan aspect-ratio border centered on the cursor")
    parser.add_argument(
        "ratio",
        nargs="?",
        choices=("16:9", "9:16"),
        help="Aspect ratio of the border",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="Override frame width in pixels (otherwise uses CONFIG in this file)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=None,
        help="Override frame height in pixels (otherwise uses CONFIG in this file)",
    )
    parser.add_argument(
        "--border",
        type=int,
        default=BORDER_WIDTH,
        help=f"Border line thickness in pixels (default {BORDER_WIDTH})",
    )
    return parser.parse_args()


def ask_ratio_interactive() -> str:
    print()
    print("  Cursor Border Overlay")
    print("  ---------------------")
    print(f"  1) 16:9  (widescreen)  {OPTION1_WIDTH}x{OPTION1_HEIGHT}")
    print(f"  2) 9:16  (vertical)    {OPTION2_WIDTH}x{OPTION2_HEIGHT}")
    print()
    while True:
        choice = input("  Choose option [1/2]: ").strip()
        if choice == "1":
            return "16:9"
        if choice == "2":
            return "9:16"
        print("  Please enter 1 or 2.")


def run(ratio: str, border_w: int, width: int | None = None, height: int | None = None) -> None:
    cfg_w, cfg_h = configured_size(ratio)
    box_w = int(width) if width is not None else cfg_w
    box_h = int(height) if height is not None else cfg_h
    box_w = max(50, box_w)
    box_h = max(50, box_h)
    border_w = max(2, min(30, border_w))

    root = tk.Tk()
    root.title(f"Cursor Border {ratio}")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=KEY_COLOR)
    root.geometry(f"{box_w}x{box_h}+0+0")

    canvas = tk.Canvas(
        root,
        width=box_w,
        height=box_h,
        bg=KEY_COLOR,
        highlightthickness=0,
        bd=0,
    )
    canvas.pack(fill="both", expand=True)

    # Hollow cyan frame: four filled bars (center is chroma-keyed away)
    bw = border_w
    canvas.create_rectangle(0, 0, box_w, bw, fill=BORDER_COLOR, outline="")
    canvas.create_rectangle(0, box_h - bw, box_w, box_h, fill=BORDER_COLOR, outline="")
    canvas.create_rectangle(0, 0, bw, box_h, fill=BORDER_COLOR, outline="")
    canvas.create_rectangle(box_w - bw, 0, box_w, box_h, fill=BORDER_COLOR, outline="")

    hwnd_holder: dict[str, int] = {"hwnd": 0}
    last_pos: dict[str, tuple[int, int] | None] = {"xy": None}

    def move_to_cursor() -> None:
        """Place the box so the live cursor sits exactly at its center."""
        cx, cy = get_cursor_pos()
        x = cx - (box_w // 2)
        y = cy - (box_h // 2)
        if last_pos["xy"] == (x, y):
            return
        last_pos["xy"] = (x, y)

        hwnd = hwnd_holder["hwnd"]
        if hwnd:
            # Move the Win32 window directly (smooth, no Tk layout fight)
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                x,
                y,
                0,
                0,
                SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER,
            )
        # Keep Tk's idea of geometry in sync as a fallback
        root.geometry(f"+{x}+{y}")

    def tick() -> None:
        # Esc works even when the overlay has no keyboard focus
        if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            root.destroy()
            return
        move_to_cursor()
        root.after(UPDATE_MS, tick)

    root.update_idletasks()
    root.update()
    hwnd = get_hwnd(root)
    hwnd_holder["hwnd"] = hwnd
    setup_layered_window(hwnd)
    # Also set Tk transparentcolor as a fallback on builds that honor it
    try:
        root.attributes("-transparentcolor", KEY_COLOR)
    except tk.TclError:
        pass

    # Pin topmost once, then track without re-asserting z-order every frame
    user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOMOVE,
    )

    print(f"Border active: {ratio}")
    print(f"  Frame size : {box_w} x {box_h} pixels")
    print(f"  Line width : {border_w} pixels (cyan)")
    print("  Cursor stays at the center of the box while you move.")
    print("  Press Esc to quit.")
    move_to_cursor()
    tick()
    root.mainloop()


def main() -> int:
    args = parse_args()
    ratio = args.ratio or ask_ratio_interactive()
    try:
        run(ratio, args.border, width=args.width, height=args.height)
    except KeyboardInterrupt:
        print("\nExiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
