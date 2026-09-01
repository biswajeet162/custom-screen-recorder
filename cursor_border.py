"""
Cursor-centered aspect-ratio border overlay for Windows.

Shows a setup prompt (quality, microphone, frame size), then a red rectangle
that follows the mouse. The screen inside that rectangle is recorded to MP4
with the chosen microphone. Press Esc to stop and save.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox, ttk

from recorder import (
    NO_AUDIO,
    QUALITY_PRESETS,
    ScreenRecorder,
    default_output_path,
    ensure_recording_deps,
    even,
    find_ffmpeg,
    list_microphones,
    preferred_microphone,
    size_for_quality,
)

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
WDA_EXCLUDEFROMCAPTURE = 0x00000011

# =============================================================================
# CONFIG — used by overlay-only mode and as custom-size defaults
# =============================================================================

# Option 1 — 16:9 (widescreen)
OPTION1_WIDTH = 1280
OPTION1_HEIGHT = 720

# Option 2 — 9:16 (vertical / portrait)
OPTION2_WIDTH = 720
OPTION2_HEIGHT = 1280

# Border look
BORDER_WIDTH = 3
BORDER_COLOR = "#FF2B2B"  # red while recording
BORDER_COLOR_IDLE = "#00FFFF"  # cyan when overlay-only

# =============================================================================

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
    """Return (width, height) from the CONFIG block for overlay-only mode."""
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
    user32.SetLayeredWindowAttributes(hwnd, KEY_COLORREF, 0, LWA_COLORKEY)
    # Keep the viewfinder out of the recording when Windows supports it
    try:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except OSError:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record the screen inside a cursor-centered aspect-ratio border"
    )
    parser.add_argument(
        "ratio",
        nargs="?",
        choices=("16:9", "9:16"),
        help="Skip the size picker and use this aspect ratio",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=None,
        help="Custom frame width in pixels",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=None,
        help="Custom frame height in pixels",
    )
    parser.add_argument(
        "--border",
        type=int,
        default=BORDER_WIDTH,
        help=f"Border line thickness in pixels (default {BORDER_WIDTH})",
    )
    parser.add_argument(
        "--quality",
        choices=tuple(QUALITY_PRESETS),
        default=None,
        help="Video quality preset: low, hd, 2k",
    )
    parser.add_argument(
        "--mic",
        default=None,
        help="Microphone name (DirectShow). Use 'none' for no audio.",
    )
    parser.add_argument(
        "--overlay-only",
        action="store_true",
        help="Show the following border without recording",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Use console prompts instead of the setup window",
    )
    return parser.parse_args()


# -----------------------------------------------------------------------------
# Setup UI
# -----------------------------------------------------------------------------


def _mic_choices(mics: list[str]) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    default = preferred_microphone(mics)
    if default:
        choices.append((f"Auto-detected: {default}", default))
        for name in mics:
            if name != default:
                choices.append((name, name))
    choices.append(("No audio (screen only)", NO_AUDIO))
    return choices


def ask_setup_gui(mics: list[str]) -> dict | None:
    """Quality / microphone / frame-size window. None if the user cancels."""
    result: dict | None = None
    root = tk.Tk()
    root.title("Cursor Follower — Record")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    quality_var = tk.StringVar(value="hd")
    ratio_var = tk.StringVar(value="16:9")
    custom_w = tk.StringVar(value="1920")
    custom_h = tk.StringVar(value="1080")
    mic_choices = _mic_choices(mics)
    mic_display = tk.StringVar(value=mic_choices[0][0])
    size_note = tk.StringVar()

    def current_size_for(ratio: str) -> tuple[int, int]:
        q = quality_var.get()
        if ratio == "custom":
            try:
                return even(int(custom_w.get())), even(int(custom_h.get()))
            except ValueError:
                return 0, 0
        return size_for_quality(ratio, q)

    def refresh_size_labels() -> None:
        w16, h16 = size_for_quality("16:9", quality_var.get())
        w916, h916 = size_for_quality("9:16", quality_var.get())
        radio_169.config(text=f"16:9 widescreen   {w16} × {h16}")
        radio_916.config(text=f"9:16 vertical     {w916} × {h916}")
        if ratio_var.get() != "custom":
            w, h = current_size_for("16:9")
            custom_w.set(str(w))
            custom_h.set(str(h))
        q = QUALITY_PRESETS[quality_var.get()]
        if ratio_var.get() == "custom":
            size_note.set(
                f"Records your custom size at {q['fps']} fps  ({q['label']} encode)"
            )
        else:
            size_note.set(
                f"Records inside the red border at {q['fps']} fps  ({q['label']}: {q['detail']})"
            )
        custom_state = "normal" if ratio_var.get() == "custom" else "disabled"
        entry_w.configure(state=custom_state)
        entry_h.configure(state=custom_state)

    pad = {"padx": 16, "pady": 4}
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Start recording", font=("Segoe UI", 14, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8)
    )

    ttk.Label(frm, text="1. Video quality", font=("Segoe UI", 10, "bold")).grid(
        row=1, column=0, columnspan=3, sticky="w", **pad
    )
    q_row = 2
    for key, preset in QUALITY_PRESETS.items():
        ttk.Radiobutton(
            frm,
            text=f"{preset['label']}   —  {preset['detail']}",
            variable=quality_var,
            value=key,
            command=refresh_size_labels,
        ).grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28, pady=1)
        q_row += 1

    ttk.Label(frm, text="2. Microphone", font=("Segoe UI", 10, "bold")).grid(
        row=q_row, column=0, columnspan=3, sticky="w", pady=(12, 4), padx=16
    )
    q_row += 1
    mic_combo = ttk.Combobox(
        frm,
        textvariable=mic_display,
        values=[c[0] for c in mic_choices],
        state="readonly",
        width=52,
    )
    mic_combo.grid(row=q_row, column=0, columnspan=3, sticky="ew", padx=28, pady=2)
    q_row += 1
    if not mics:
        ttk.Label(
            frm,
            text="No microphones found — recording will be screen-only unless you plug one in.",
            foreground="#666666",
        ).grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28)
        q_row += 1

    ttk.Label(frm, text="3. Frame size (the following border)", font=("Segoe UI", 10, "bold")).grid(
        row=q_row, column=0, columnspan=3, sticky="w", pady=(12, 4), padx=16
    )
    q_row += 1
    radio_169 = ttk.Radiobutton(
        frm, variable=ratio_var, value="16:9", command=refresh_size_labels
    )
    radio_169.grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28, pady=1)
    q_row += 1
    radio_916 = ttk.Radiobutton(
        frm, variable=ratio_var, value="9:16", command=refresh_size_labels
    )
    radio_916.grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28, pady=1)
    q_row += 1

    custom_row = ttk.Frame(frm)
    custom_row.grid(row=q_row, column=0, columnspan=3, sticky="w", padx=28, pady=1)
    ttk.Radiobutton(
        custom_row,
        text="Custom",
        variable=ratio_var,
        value="custom",
        command=refresh_size_labels,
    ).pack(side="left")
    entry_w = ttk.Entry(custom_row, textvariable=custom_w, width=7)
    entry_w.pack(side="left", padx=(10, 4))
    ttk.Label(custom_row, text="×").pack(side="left")
    entry_h = ttk.Entry(custom_row, textvariable=custom_h, width=7)
    entry_h.pack(side="left", padx=4)
    ttk.Label(custom_row, text="pixels").pack(side="left")
    q_row += 1

    ttk.Label(frm, textvariable=size_note, foreground="#444444").grid(
        row=q_row, column=0, columnspan=3, sticky="w", padx=16, pady=(10, 4)
    )
    q_row += 1

    def start() -> None:
        nonlocal result
        ratio = ratio_var.get()
        quality = quality_var.get()
        if ratio == "custom":
            try:
                width = even(int(custom_w.get().strip()))
                height = even(int(custom_h.get().strip()))
            except ValueError:
                messagebox.showerror("Invalid size", "Custom width and height must be whole numbers.")
                return
            if width < 64 or height < 64:
                messagebox.showerror("Invalid size", "Custom size must be at least 64 × 64.")
                return
            if width > 7680 or height > 7680:
                messagebox.showerror("Invalid size", "Custom size must be 7680 × 7680 or smaller.")
                return
            ratio_label = f"{width}:{height}"
        else:
            width, height = size_for_quality(ratio, quality)
            ratio_label = ratio

        display = mic_display.get()
        mic_name = next((val for label, val in mic_choices if label == display), NO_AUDIO)
        if mic_name == NO_AUDIO:
            mic_name = None

        result = {
            "quality": quality,
            "ratio": ratio_label,
            "width": width,
            "height": height,
            "mic_name": mic_name,
            "record": True,
        }
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(row=q_row, column=0, columnspan=3, sticky="e", pady=(12, 4), padx=8)
    ttk.Button(btns, text="Cancel", command=root.destroy).pack(side="right", padx=4)
    ttk.Button(btns, text="Start recording", command=start).pack(side="right", padx=4)

    refresh_size_labels()
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result


def ask_setup_cli(mics: list[str]) -> dict | None:
    print()
    print("  Cursor Follower — Record")
    print("  ------------------------")
    print()
    print("  Video quality")
    keys = list(QUALITY_PRESETS)
    for i, key in enumerate(keys, start=1):
        p = QUALITY_PRESETS[key]
        default = "  (recommended)" if key == "hd" else ""
        print(f"    {i}) {p['label']:3}  —  {p['detail']}{default}")
    print()
    quality = "hd"
    while True:
        raw = input("  Choose quality [1/2/3] (default 2): ").strip() or "2"
        if raw in ("1", "2", "3"):
            quality = keys[int(raw) - 1]
            break
        print("  Please enter 1, 2, or 3.")

    print()
    print("  Microphone")
    mic_name: str | None = None
    if mics:
        default = preferred_microphone(mics) or mics[0]
        default_idx = mics.index(default) + 1
        print(f"    Auto-detected: {default}")
        print()
        for i, name in enumerate(mics, start=1):
            tag = "  [default]" if name == default else ""
            print(f"    {i}) {name}{tag}")
        print(f"    {len(mics) + 1}) No audio (screen only)")
        print()
        while True:
            raw = input(
                f"  Choose microphone [1-{len(mics) + 1}] (default {default_idx}): "
            ).strip() or str(default_idx)
            try:
                idx = int(raw)
            except ValueError:
                print("  Please enter a number from the list.")
                continue
            if 1 <= idx <= len(mics):
                mic_name = mics[idx - 1]
                break
            if idx == len(mics) + 1:
                mic_name = None
                break
            print("  That number is not in the list.")
    else:
        print("    None found. Recording will be screen-only.")
        input("  Press Enter to continue...")

    print()
    w16, h16 = size_for_quality("16:9", quality)
    w916, h916 = size_for_quality("9:16", quality)
    print("  Frame size (the following border)")
    print(f"    1) 16:9  widescreen  {w16}x{h16}")
    print(f"    2) 9:16  vertical    {w916}x{h916}")
    print("    3) Custom width x height")
    print()
    while True:
        raw = input("  Choose size [1/2/3]: ").strip()
        if raw == "1":
            ratio, width, height = "16:9", w16, h16
            break
        if raw == "2":
            ratio, width, height = "9:16", w916, h916
            break
        if raw == "3":
            try:
                width = even(int(input("  Width  (pixels): ").strip()))
                height = even(int(input("  Height (pixels): ").strip()))
            except ValueError:
                print("  Please enter whole numbers.")
                continue
            if width < 64 or height < 64:
                print("  Size must be at least 64x64.")
                continue
            ratio = f"{width}:{height}"
            break
        print("  Please enter 1, 2, or 3.")

    return {
        "quality": quality,
        "ratio": ratio,
        "width": width,
        "height": height,
        "mic_name": mic_name,
        "record": True,
    }


def resolve_session(args: argparse.Namespace, mics: list[str]) -> dict | None:
    """Build a session dict from CLI flags, or open the setup prompt."""
    overlay_only = bool(args.overlay_only)
    fully_specified = bool(args.quality) and bool(args.ratio or (args.width and args.height))
    if overlay_only:
        ratio = args.ratio or "16:9"
        if args.width and args.height:
            width, height = even(args.width), even(args.height)
            ratio = f"{width}:{height}"
        else:
            width, height = configured_size(ratio)
        return {
            "quality": args.quality or "hd",
            "ratio": ratio,
            "width": width,
            "height": height,
            "mic_name": None,
            "record": False,
        }

    if fully_specified:
        quality = args.quality or "hd"
        if args.width and args.height:
            width, height = even(args.width), even(args.height)
            ratio = f"{width}:{height}"
        else:
            ratio = args.ratio or "16:9"
            width, height = size_for_quality(ratio, quality)
        mic_name: str | None
        if args.mic is None:
            mic_name = preferred_microphone(mics)
        elif args.mic.strip().lower() in ("none", "off", "no"):
            mic_name = None
        else:
            mic_name = args.mic
        return {
            "quality": quality,
            "ratio": ratio,
            "width": width,
            "height": height,
            "mic_name": mic_name,
            "record": True,
        }

    if args.cli:
        return ask_setup_cli(mics)
    return ask_setup_gui(mics)


# -----------------------------------------------------------------------------
# Overlay + record
# -----------------------------------------------------------------------------


def run(
    ratio: str,
    border_w: int,
    width: int,
    height: int,
    *,
    record: bool,
    quality: str,
    mic_name: str | None,
    ffmpeg: str | None,
) -> None:
    box_w = even(max(50, int(width)))
    box_h = even(max(50, int(height)))
    border_w = max(2, min(30, int(border_w)))
    rec_color = BORDER_COLOR if record else BORDER_COLOR_IDLE

    # Outer ring is the viewfinder; inner box_w x box_h is what gets recorded
    win_w = box_w + 2 * border_w
    win_h = box_h + 2 * border_w

    root = tk.Tk()
    root.title(f"Cursor Border {ratio}")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=KEY_COLOR)
    root.geometry(f"{win_w}x{win_h}+0+0")

    canvas = tk.Canvas(
        root,
        width=win_w,
        height=win_h,
        bg=KEY_COLOR,
        highlightthickness=0,
        bd=0,
    )
    canvas.pack(fill="both", expand=True)

    bw = border_w
    canvas.create_rectangle(0, 0, win_w, bw, fill=rec_color, outline="")
    canvas.create_rectangle(0, win_h - bw, win_w, win_h, fill=rec_color, outline="")
    canvas.create_rectangle(0, 0, bw, win_h, fill=rec_color, outline="")
    canvas.create_rectangle(win_w - bw, 0, win_w, win_h, fill=rec_color, outline="")

    hwnd_holder: dict[str, int] = {"hwnd": 0}
    last_pos: dict[str, tuple[int, int] | None] = {"xy": None}
    recorder: ScreenRecorder | None = None
    stopping = {"done": False}

    def move_to_cursor() -> None:
        """Place the box so the live cursor sits exactly at its content center."""
        cx, cy = get_cursor_pos()
        x = cx - (box_w // 2) - bw
        y = cy - (box_h // 2) - bw
        if last_pos["xy"] == (x, y):
            return
        last_pos["xy"] = (x, y)

        hwnd = hwnd_holder["hwnd"]
        if hwnd:
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                x,
                y,
                0,
                0,
                SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER,
            )
        root.geometry(f"+{x}+{y}")

    def finish() -> None:
        if stopping["done"]:
            return
        stopping["done"] = True
        saved = None
        elapsed = 0.0
        frames = 0
        if recorder is not None:
            print("  Stopping recorder...")
            elapsed = recorder.elapsed_s()
            frames = recorder.frames_written
            try:
                saved = recorder.stop()
            except RuntimeError as exc:
                print(f"  Recorder error: {exc}")
        try:
            root.destroy()
        except tk.TclError:
            pass
        if saved:
            mins, secs = divmod(int(elapsed), 60)
            print()
            print(f"  Saved: {saved}")
            print(f"  Length: {mins:02d}:{secs:02d}  ({frames} frames)")
        elif record:
            print("  No video file was written.")

    def tick() -> None:
        if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            finish()
            return
        move_to_cursor()
        root.after(UPDATE_MS, tick)

    root.update_idletasks()
    root.update()
    hwnd = get_hwnd(root)
    hwnd_holder["hwnd"] = hwnd
    setup_layered_window(hwnd)
    try:
        root.attributes("-transparentcolor", KEY_COLOR)
    except tk.TclError:
        pass

    user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOMOVE,
    )

    q = QUALITY_PRESETS[quality]
    print()
    if record:
        print(f"  Recording  {ratio}   {box_w}x{box_h}   {q['label']} @ {q['fps']} fps")
        if mic_name:
            print(f"  Microphone: {mic_name}")
        else:
            print("  Microphone: off")
        out = default_output_path(ratio, quality, box_w, box_h)
        print(f"  File: {out}")
        print("  Red border = captured area (cursor stays in the center).")
        print("  Press Esc to stop and save.")
        recorder = ScreenRecorder(
            width=box_w,
            height=box_h,
            fps=int(q["fps"]),
            crf=int(q["crf"]),
            x264_preset=str(q["preset"]),
            mic_name=mic_name,
            audio_bitrate=str(q["audio_bitrate"]),
            output_path=out,
            get_cursor_pos=get_cursor_pos,
        )
        assert ffmpeg is not None
        try:
            recorder.start(ffmpeg)
        except Exception as exc:  # noqa: BLE001
            print(f"  Could not start recorder: {exc}")
            try:
                root.destroy()
            except tk.TclError:
                pass
            return
    else:
        print(f"  Overlay only: {ratio}   {box_w}x{box_h}")
        print("  Press Esc to quit.")

    root.protocol("WM_DELETE_WINDOW", finish)
    move_to_cursor()
    tick()
    try:
        root.mainloop()
    finally:
        finish()


def main() -> int:
    args = parse_args()
    print()
    print("  Cursor Follower")
    print("  ---------------")
    try:
        ensure_recording_deps()
    except Exception as exc:  # noqa: BLE001
        print(f"  Could not install packages: {exc}")
        print("  Try:  py -3 -m pip install -r requirements.txt")
        return 1

    ffmpeg = None
    mics: list[str] = []
    if not args.overlay_only:
        try:
            ffmpeg = find_ffmpeg()
        except FileNotFoundError as exc:
            print(f"  {exc}")
            return 1
        try:
            mics = list_microphones(ffmpeg)
        except Exception as exc:  # noqa: BLE001
            print(f"  Could not list microphones ({exc}). Continuing without a default mic.")
            mics = []
        if mics:
            print(f"  Auto-detected microphone: {preferred_microphone(mics)}")
        else:
            print("  No microphones detected.")

    session = resolve_session(args, mics)
    if session is None:
        print("  Cancelled.")
        return 0

    try:
        run(
            session["ratio"],
            args.border,
            session["width"],
            session["height"],
            record=session["record"],
            quality=session["quality"],
            mic_name=session["mic_name"],
            ffmpeg=ffmpeg,
        )
    except KeyboardInterrupt:
        print("\n  Exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
