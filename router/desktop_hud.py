"""Windows 11 Desktop Use Permission Popup & Active Screen HUD for NorthCUA.

Runs in its own `pythonw.exe -I` process on the interactive `Winsta0\\Default`
desktop so Tkinter always owns the main thread cleanly.

Modes:
1. `--mode confirm --task "<task>"`
   Shows a centered, always-on-top permission dialog before NorthCUA touches
   the mouse or keyboard.
   Exit codes:
     0 = Allowed by user
     1 = Denied by user (or closed / timed out)

2. `--mode hud --task "<task>"`
   Shows:
   - A 4px glowing cyan/amber border around the primary screen (click-through
     via `WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE`, excluded from
     screenshots via `SetWindowDisplayAffinity(..., WDA_EXCLUDEFROMCAPTURE)`).
   - A sleek top-center floating HUD bar showing the active step/tool and a
     clickable red `[ ⏹ STOP (F8) ]` button (also excluded from screenshots via
     `WDA_EXCLUDEFROMCAPTURE` and `WS_EX_NOACTIVATE` so it never steals focus).
   - Polls `VK_F8` (0x77) and `VK_PAUSE` (0x13) every 50 ms as a global
     emergency stop hotkey that works even without moving the mouse.
   - Reads JSON status lines `{"status": "..."}` from stdin to update the HUD
     label in real time.
   Exit codes:
     42 = User clicked STOP or pressed F8 / Pause
     0  = Normal exit when parent closes stdin / terminates HUD
"""

from __future__ import annotations

import argparse
import ctypes
import json
import queue
import sys
import threading
import tkinter as tk
import winsound

user32 = ctypes.windll.user32

# Win32 constants
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

VK_F8 = 0x77
VK_PAUSE = 0x13


def _enable_dpi_awareness() -> None:
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def _get_toplevel_hwnd(widget: tk.Misc) -> int:
    widget.update_idletasks()
    hwnd = int(widget.winfo_id())
    parent = int(user32.GetParent(hwnd))
    return parent if parent else hwnd


def _exclude_from_capture(widget: tk.Misc) -> None:
    """Hide window from BitBlt / mss screenshots while keeping it visible on monitor."""
    widget.update_idletasks()
    hwnd_inner = int(widget.winfo_id())
    hwnd_outer = int(user32.GetParent(hwnd_inner))
    for h in (hwnd_inner, hwnd_outer):
        if h:
            try:
                user32.SetWindowDisplayAffinity(ctypes.c_void_p(h), WDA_EXCLUDEFROMCAPTURE)
            except Exception:
                pass


def _make_click_through(widget: tk.Misc, no_activate: bool = True) -> None:
    hwnd = _get_toplevel_hwnd(widget)
    style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    new_style = style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
    if no_activate:
        new_style |= WS_EX_NOACTIVATE
    user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, new_style)


def _make_no_activate_toolwindow(widget: tk.Misc) -> None:
    hwnd = _get_toplevel_hwnd(widget)
    style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    new_style = style | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, new_style)


def run_confirm_dialog(task: str, timeout_s: int = 120) -> int:
    _enable_dpi_awareness()
    result = {"code": 1}

    root = tk.Tk()
    root.title("NorthCUA — Desktop Control Permission")
    root.configure(bg="#111827")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    w, h = 640, 340
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2 - 40)
    root.geometry(f"{w}x{h}+{x}+{y}")

    # Outer border frame
    outer = tk.Frame(root, bg="#38bdf8", padx=2, pady=2)
    outer.pack(fill="both", expand=True)

    inner = tk.Frame(outer, bg="#111827", padx=24, pady=20)
    inner.pack(fill="both", expand=True)

    header = tk.Label(
        inner,
        text="🖥️  NorthCUA Wants to Use Your Desktop",
        font=("Segoe UI Semibold", 15),
        fg="#f9fafb",
        bg="#111827",
        anchor="w",
    )
    header.pack(fill="x", pady=(0, 6))

    sub = tk.Label(
        inner,
        text="Allow NorthCUA to control your mouse and keyboard for this task?",
        font=("Segoe UI", 10),
        fg="#9ca3af",
        bg="#111827",
        anchor="w",
    )
    sub.pack(fill="x", pady=(0, 12))

    # Task description box
    task_box = tk.Frame(inner, bg="#1f2937", padx=14, pady=12, highlightthickness=1, highlightbackground="#374151")
    task_box.pack(fill="both", expand=True, pady=(0, 12))

    display_task = (task or "").strip()
    if len(display_task) > 260:
        display_task = display_task[:257] + "..."

    task_lbl = tk.Label(
        task_box,
        text=display_task,
        font=("Consolas", 10),
        fg="#e5e7eb",
        bg="#1f2937",
        justify="left",
        anchor="nw",
        wraplength=560,
    )
    task_lbl.pack(fill="both", expand=True)

    tip = tk.Label(
        inner,
        text="💡 While running, a top bar & glowing screen border will appear. Click ⏹ STOP or press F8 anytime to stop.",
        font=("Segoe UI", 9),
        fg="#38bdf8",
        bg="#111827",
        anchor="w",
    )
    tip.pack(fill="x", pady=(0, 14))

    btn_frame = tk.Frame(inner, bg="#111827")
    btn_frame.pack(fill="x")

    countdown = {"remaining": timeout_s}

    def on_allow(event=None):
        result["code"] = 0
        root.destroy()

    def on_deny(event=None):
        result["code"] = 1
        root.destroy()

    deny_btn = tk.Button(
        btn_frame,
        text=f"✕  Deny (Esc) [{countdown['remaining']}s]",
        command=on_deny,
        font=("Segoe UI Semibold", 10),
        fg="#f9fafb",
        bg="#374151",
        activeforeground="#ffffff",
        activebackground="#4b5563",
        relief="flat",
        padx=18,
        pady=8,
        cursor="hand2",
    )
    deny_btn.pack(side="right", padx=(10, 0))

    allow_btn = tk.Button(
        btn_frame,
        text="✓  Allow Desktop Control (Enter)",
        command=on_allow,
        font=("Segoe UI Semibold", 10),
        fg="#030712",
        bg="#38bdf8",
        activeforeground="#030712",
        activebackground="#7dd3fc",
        relief="flat",
        padx=20,
        pady=8,
        cursor="hand2",
    )
    allow_btn.pack(side="right")

    root.bind("<Return>", on_allow)
    root.bind("<Escape>", on_deny)
    root.protocol("WM_DELETE_WINDOW", on_deny)

    def _tick():
        countdown["remaining"] -= 1
        if countdown["remaining"] <= 0:
            on_deny()
            return
        try:
            deny_btn.configure(text=f"✕  Deny (Esc) [{countdown['remaining']}s]")
            root.after(1000, _tick)
        except Exception:
            pass

    def _bring_to_front():
        try:
            winsound.MessageBeep(winsound.MB_ICONQUESTION)
        except Exception:
            pass
        try:
            root.lift()
            root.attributes("-topmost", True)
            hwnd = _get_toplevel_hwnd(root)
            user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
            user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002)
            user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
            root.focus_force()
            allow_btn.focus_set()
        except Exception:
            pass

    root.after(80, _bring_to_front)
    root.after(1000, _tick)
    root.mainloop()
    return result["code"]


def run_hud(task: str) -> int:
    _enable_dpi_awareness()
    exit_state = {"code": 0}
    msg_queue: "queue.Queue[dict]" = queue.Queue()

    # Background thread reading JSON lines from stdin
    def _stdin_reader():
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        msg_queue.put(data)
                except Exception:
                    msg_queue.put({"status": line})
        except Exception:
            pass
        finally:
            msg_queue.put({"eof": True})

    t = threading.Thread(target=_stdin_reader, daemon=True)
    t.start()

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#0f172a")

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()

    # Top-center HUD bar dimensions
    hud_w = min(860, sw - 80)
    hud_h = 46
    hud_x = (sw - hud_w) // 2
    hud_y = 10
    root.geometry(f"{hud_w}x{hud_h}+{hud_x}+{hud_y}")

    # Glowing border around primary screen (Toplevel window)
    border_win = tk.Toplevel(root)
    border_win.overrideredirect(True)
    border_win.attributes("-topmost", True)
    transparent_key = "#010203"
    border_win.configure(bg=transparent_key)
    border_win.attributes("-transparentcolor", transparent_key)
    border_win.geometry(f"{sw}x{sh}+0+0")

    border_canvas = tk.Canvas(
        border_win,
        width=sw,
        height=sh,
        bg=transparent_key,
        highlightthickness=0,
        bd=0,
    )
    border_canvas.pack(fill="both", expand=True)
    border_rect = border_canvas.create_rectangle(
        2, 2, sw - 2, sh - 2, outline="#38bdf8", width=4
    )

    # HUD Bar layout
    hud_outer = tk.Frame(root, bg="#38bdf8", padx=2, pady=2)
    hud_outer.pack(fill="both", expand=True)

    hud_inner = tk.Frame(hud_outer, bg="#0f172a", padx=12, pady=4)
    hud_inner.pack(fill="both", expand=True)

    dot_lbl = tk.Label(
        hud_inner,
        text="●  NorthCUA Active",
        font=("Segoe UI Semibold", 10),
        fg="#38bdf8",
        bg="#0f172a",
    )
    dot_lbl.pack(side="left", padx=(0, 12))

    sep = tk.Frame(hud_inner, bg="#334155", width=1)
    sep.pack(side="left", fill="y", pady=4, padx=(0, 12))

    short_task = (task or "").strip().replace("\n", " ")
    if len(short_task) > 55:
        short_task = short_task[:52] + "..."

    status_var = tk.StringVar(value=f"Starting — {short_task}")
    status_lbl = tk.Label(
        hud_inner,
        textvariable=status_var,
        font=("Consolas", 10),
        fg="#f8fafc",
        bg="#0f172a",
        anchor="w",
    )
    status_lbl.pack(side="left", fill="x", expand=True, padx=(0, 12))

    def _trigger_stop():
        exit_state["code"] = 42
        try:
            sys.stdout.write("STOP\n")
            sys.stdout.flush()
        except Exception:
            pass
        root.destroy()

    stop_btn = tk.Button(
        hud_inner,
        text="⏹  STOP (F8)",
        command=_trigger_stop,
        font=("Segoe UI Semibold", 9),
        fg="#ffffff",
        bg="#dc2626",
        activeforeground="#ffffff",
        activebackground="#b91c1c",
        relief="flat",
        padx=12,
        pady=2,
        cursor="hand2",
    )
    stop_btn.pack(side="right")

    def _init_win32_styles():
        _exclude_from_capture(root)
        _exclude_from_capture(border_win)
        _make_click_through(border_win, no_activate=True)
        _make_no_activate_toolwindow(root)
        try:
            user32.GetAsyncKeyState(VK_F8)
            user32.GetAsyncKeyState(VK_PAUSE)
        except Exception:
            pass

    root.after(30, _init_win32_styles)

    pulse_state = {"on": True}

    def _poll():
        # Check global emergency stop keys: F8 (0x77) or Pause (0x13) — 0x8000 (held) or 0x0001 (tapped since last poll)
        try:
            if (user32.GetAsyncKeyState(VK_F8) & 0x8001) or (user32.GetAsyncKeyState(VK_PAUSE) & 0x8001):
                _trigger_stop()
                return
        except Exception:
            pass

        # Process any status updates from stdin
        while True:
            try:
                msg = msg_queue.get_nowait()
            except queue.Empty:
                break
            if msg.get("eof"):
                root.destroy()
                return
            if msg.get("action") == "stop":
                stop_btn.invoke()
                return
            st = msg.get("status")
            if st:
                st_clean = str(st).strip().replace("\n", " ")
                if len(st_clean) > 68:
                    st_clean = st_clean[:65] + "..."
                status_var.set(st_clean)

        root.after(50, _poll)

    def _pulse():
        pulse_state["on"] = not pulse_state["on"]
        color = "#38bdf8" if pulse_state["on"] else "#0284c7"
        try:
            dot_lbl.configure(fg=color)
            hud_outer.configure(bg=color)
            border_canvas.itemconfig(border_rect, outline=color)
            # Re-assert topmost so fullscreen/maximized windows don't cover HUD
            root.attributes("-topmost", True)
            border_win.attributes("-topmost", True)
            root.after(700, _pulse)
        except Exception:
            pass

    root.after(60, _poll)
    root.after(700, _pulse)
    root.mainloop()
    return exit_state["code"]


def main() -> int:
    parser = argparse.ArgumentParser(description="NorthCUA Desktop HUD & Permission Dialog")
    parser.add_argument("--mode", choices=["confirm", "hud"], required=True)
    parser.add_argument("--task", default="")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    if args.mode == "confirm":
        return run_confirm_dialog(args.task, timeout_s=args.timeout)
    return run_hud(args.task)


if __name__ == "__main__":
    sys.exit(main())
