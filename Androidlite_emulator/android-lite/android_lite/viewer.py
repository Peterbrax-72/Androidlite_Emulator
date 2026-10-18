"""The VM window: shows the guest screen and sends keyboard and mouse input.

QEMU runs headless (-display none). A background thread asks QEMU for a screen dump a few
times per second, and this window shows it at 1:1 scale. Input goes back through vmio.
Standard library only (Tkinter).
"""
from __future__ import annotations

import os
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog

from . import automation, instances, qmp, vmio

BG = "#16181d"
BAR = "#23262d"
FG = "#e6e8ec"
ACCENT = "#3ddc84"
POLL_MS = 60
FRAME_INTERVAL = 0.3              # seconds between screen dumps (about 3 frames per second)
FAIL_NOTICE_AFTER = 3             # consecutive failures before the status says the VM is not answering
VOLUME_KEYS = {"volumeup", "volumedown", "audiomute"}


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "vm"


class Viewer(tk.Toplevel):
    """One window per running VM. Closing it does not stop the VM."""

    def __init__(self, master, p: dict, inst: dict, port: int, dispatch=None, on_close=None):
        super().__init__(master)
        self.p = p
        self.inst = inst
        self.port = port
        self._dispatch_fn = dispatch or (lambda events: vmio.sender_for(port).send(events))
        self._on_close = on_close
        folder = instances.resolve(p, inst)["folder"]
        self._keymap_path = os.path.join(folder, automation.KEYMAP_FILE)
        self._tmp_frame = os.path.join(folder, "frame.ppm")
        self.keymap: list[dict] = automation.load_keymap(self._keymap_path)

        self._recorder: automation.Recorder | None = None
        self._held: dict = {}                   # key id -> events to send on release
        self._held_btn: str | None = None
        self._map_mode = False
        self._map_key: str | None = None
        self._fullscreen = False
        self._sync = False
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._latest = None                     # (w, h, ppm bytes) waiting to be shown
        self._last_frame = None
        self._state = "Connecting..."
        self._frame_w = self._frame_h = 0
        self._photo = None
        self._message = ""
        self._poll_id = None

        self.title(f"{inst['name']} - Android Lite")
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        threading.Thread(target=self._frame_loop, name=f"frames-{port}", daemon=True).start()
        self.after(POLL_MS, self._poll)
        self.canvas.focus_set()

    # ---- layout ----------------------------------------------------------------
    def _build(self) -> None:
        bar = tk.Frame(self, bg=BAR)
        bar.pack(side="top", fill="x")
        self._rec_btn = None
        buttons = [
            ("Vol -", lambda: self.volume("volumedown")),
            ("Vol +", lambda: self.volume("volumeup")),
            ("Mute", lambda: self.volume("audiomute")),
            ("Fullscreen", self.toggle_fullscreen),
            ("Map key", self.toggle_map_mode),
            ("Keymap", self.open_keymap),
            ("Record", self.toggle_record),
            ("Macros", self.open_macros),
            ("Screenshot", self.screenshot),
        ]
        for label, cmd in buttons:
            b = tk.Button(bar, text=label, command=cmd, bg=BAR, fg=FG, activebackground="#343844",
                          activeforeground=FG, relief="flat", padx=8, pady=3, takefocus=0)
            b.pack(side="left", padx=(4, 0), pady=4)
            if label == "Record":
                self._rec_btn = b
            if label == "Map key":
                self._map_btn = b
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, width=640, height=360,
                                takefocus=1)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.status = tk.Label(self, anchor="w", bg=BAR, fg=FG, padx=8, pady=3)
        self.status.pack(side="bottom", fill="x")

        c = self.canvas
        c.bind("<ButtonPress-1>", lambda e: self._press(e, "left"))
        c.bind("<ButtonRelease-1>", lambda e: self._release("left"))
        c.bind("<B1-Motion>", self._motion)
        c.bind("<ButtonPress-3>", lambda e: self._press(e, "right"))
        c.bind("<ButtonRelease-3>", lambda e: self._release("right"))
        c.bind("<ButtonPress-2>", lambda e: self._press(e, "middle"))
        c.bind("<ButtonRelease-2>", lambda e: self._release("middle"))
        c.bind("<Configure>", lambda e: self._redraw())
        self.bind("<KeyPress>", self._key_press)
        self.bind("<KeyRelease>", self._key_release)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<F11>", lambda e: self.toggle_fullscreen())

    # ---- frames (background thread) ------------------------------------------
    def _frame_loop(self) -> None:
        fails = 0
        while not self._closed.is_set():
            try:
                data = vmio.grab_frame(self.port, self._tmp_frame)
                w, h = qmp.ppm_size(data)
                with self._lock:
                    self._latest = (w, h, data)
                    self._state = "Live"
                fails = 0
                wait = FRAME_INTERVAL
            except (OSError, ValueError, qmp.QMPError):
                fails += 1
                if fails >= FAIL_NOTICE_AFTER:
                    with self._lock:
                        self._state = "VM is not responding"
                wait = 1.0
            self._closed.wait(wait)

    # ---- drawing (main thread) -------------------------------------------------
    def _poll(self) -> None:
        if self._closed.is_set():
            return
        with self._lock:
            frame, self._latest = self._latest, None
            state = self._state
        if frame:
            self._show(*frame)
        self._update_status(state)
        self._poll_id = self.after(POLL_MS, self._poll)

    def _show(self, w: int, h: int, data: bytes) -> None:
        self._photo = tk.PhotoImage(master=self, data=data, format="PPM")
        self._last_frame = data
        if (w, h) != (self._frame_w, self._frame_h):
            first = self._frame_w == 0
            self._frame_w, self._frame_h = w, h
            if not self._fullscreen:
                self.canvas.config(width=w, height=h)
            if first:                            # park the window at the right edge, clear of the main window
                self.update_idletasks()
                x = max(0, self.winfo_screenwidth() - self.winfo_reqwidth() - 20)
                self.geometry(f"+{x}+40")
        self._redraw()

    def _offset(self) -> tuple[int, int]:
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        return (cw - self._frame_w) // 2, (ch - self._frame_h) // 2

    def _redraw(self) -> None:
        c = self.canvas
        c.delete("frame")
        c.delete("marker")
        if self._photo is None or not self._frame_w:
            return
        ox, oy = self._offset()
        c.create_image(ox, oy, anchor="nw", image=self._photo, tags="frame")
        for e in self.keymap:
            cx = ox + e["x"] * self._frame_w
            cy = oy + e["y"] * self._frame_h
            c.create_oval(cx - 13, cy - 13, cx + 13, cy + 13, outline=ACCENT, width=2, tags="marker")
            c.create_text(cx, cy, text=e["key"].upper()[:4], fill=ACCENT,
                          font=("Segoe UI", 8, "bold"), tags="marker")

    def _update_status(self, state: str) -> None:
        bits = [state]
        if self._frame_w:
            bits.append(f"{self._frame_w}x{self._frame_h}")
        if self._sync:
            bits.append("sync on")
        if self._recorder is not None:
            bits.append("recording")
        if self._message:
            bits.append(self._message)
        text = "   |   ".join(bits)
        if self.status.cget("text") != text:
            self.status.config(text=text)

    def set_message(self, text: str) -> None:
        self._message = text

    def set_sync(self, on: bool) -> None:
        self._sync = on

    # ---- input ---------------------------------------------------------------
    def _norm(self, x: int, y: int) -> tuple[float, float] | None:
        if not self._frame_w:
            return None
        ox, oy = self._offset()
        return (x - ox) / self._frame_w, (y - oy) / self._frame_h

    def _emit(self, events: list[dict]) -> None:
        if not events or self._closed.is_set():
            return
        if self._recorder is not None:
            self._recorder.add(events)
        self._dispatch_fn(events)

    def _press(self, e, btn: str) -> None:
        self.canvas.focus_set()
        pos = self._norm(e.x, e.y)
        if pos is None:
            return
        if self._map_mode and self._map_key and btn == "left":
            self._finish_map(*pos)
            return
        self._held_btn = btn
        self._emit([vmio.ev_move(*pos), vmio.ev_down(*pos, btn=btn)])

    def _motion(self, e) -> None:
        if self._held_btn is None:
            return
        pos = self._norm(e.x, e.y)
        if pos is not None:
            self._emit([vmio.ev_move(*pos)])

    def _release(self, btn: str) -> None:
        if self._held_btn == btn:
            self._held_btn = None
            self._emit([vmio.ev_up(btn)])

    def _key_id(self, e):
        return e.keycode or e.keysym

    def _key_press(self, e) -> None:
        kid = self._key_id(e)
        if kid in self._held:                   # auto-repeat: the key is already down
            return
        res = vmio.resolve_keysym(e.keysym)
        if self._map_mode:
            if e.keysym == "Escape":
                self._end_map_mode("Mapping cancelled")
            elif self._map_key is None and res is not None:
                self._map_key = res[0]
                self.set_message(f"Click where '{res[0]}' should tap")
            return
        if res is None:
            return
        qcode, shift = res
        mapped_down = automation.keymap_events(self.keymap, qcode, True)
        if mapped_down is not None:
            self._held[kid] = automation.keymap_events(self.keymap, qcode, False)
            self._emit(mapped_down)
            return
        down, release = [], [vmio.ev_key(qcode, False)]
        if shift:
            down.append(vmio.ev_key("shift", True))
            release.append(vmio.ev_key("shift", False))
        down.append(vmio.ev_key(qcode, True))
        self._held[kid] = release
        self._emit(down)

    def _key_release(self, e) -> None:
        release = self._held.pop(self._key_id(e), None)
        if release:
            self._emit(release)

    def _on_focus_out(self, _e) -> None:
        # Releasing everything when the window loses focus stops keys from getting stuck down.
        self._release_all()

    def _release_all(self) -> None:
        events = []
        for release in self._held.values():
            events += release
        self._held.clear()
        if self._held_btn:
            events.append(vmio.ev_up(self._held_btn))
            self._held_btn = None
        self._emit(events)

    # ---- toolbar actions -------------------------------------------------------
    def volume(self, qcode: str) -> None:
        if qcode in VOLUME_KEYS:
            self._emit([vmio.ev_key(qcode, True), vmio.ev_key(qcode, False)])

    def toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)
        self._redraw()

    def toggle_map_mode(self) -> None:
        if self._map_mode:
            self._end_map_mode("Mapping cancelled")
            return
        self._map_mode = True
        self._map_key = None
        self._map_btn.config(text="Cancel map", fg=ACCENT)
        self.set_message("Press the key to map (Esc cancels)")
        self.canvas.focus_set()

    def _end_map_mode(self, message: str = "") -> None:
        self._map_mode = False
        self._map_key = None
        self._map_btn.config(text="Map key", fg=FG)
        self.set_message(message)

    def _finish_map(self, x: float, y: float) -> None:
        key = self._map_key
        entries = [e for e in self.keymap if e["key"] != key]
        entries.append(automation.validate_entry({"key": key, "x": x, "y": y}))
        self._set_keymap(entries)
        self._end_map_mode(f"Mapped '{key}'")

    def _set_keymap(self, entries: list[dict]) -> None:
        try:
            automation.save_keymap(self._keymap_path, entries)
            self.keymap = automation.load_keymap(self._keymap_path)
        except (OSError, ValueError) as e:
            messagebox.showerror("Keymap", f"Could not save the keymap: {e}", parent=self)
            return
        self._redraw()

    def open_keymap(self) -> None:
        KeymapDialog(self, self.keymap, self._set_keymap)

    def toggle_record(self) -> None:
        if self._recorder is None:
            self._recorder = automation.Recorder()
            self._rec_btn.config(text="Stop", fg=ACCENT)
            self.set_message("Recording. Click 'Stop' when finished")
            return
        events = self._recorder.events
        self._recorder = None
        self._rec_btn.config(text="Record", fg=FG)
        self.set_message("")
        if not events:
            self.set_message("Nothing was recorded")
            return
        name = simpledialog.askstring("Save macro", "Name for this macro:", parent=self)
        if not name:
            return
        try:
            automation.save_macro(self.p["base"], name, events)
            self.set_message(f"Saved macro '{name.strip()}'")
        except (OSError, automation.AutomationError) as e:
            messagebox.showerror("Save macro", str(e), parent=self)

    def open_macros(self) -> None:
        MacroDialog(self, self.p, on_play=self.play_macro)

    def play_macro(self, name: str) -> None:
        try:
            events = automation.to_playback(automation.load_macro(self.p["base"], name))
        except (OSError, ValueError, automation.AutomationError) as e:
            messagebox.showerror("Macro", str(e), parent=self)
            return
        self._dispatch_fn(events)                # not recorded, and sent to every synced VM
        self.set_message(f"Playing '{name}'")

    def screenshot(self) -> None:
        data = self._last_frame
        if data is None:
            self.set_message("No picture yet")
            return
        out_dir = os.path.join(self.p["base"], "screenshots")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"{_safe_name(self.inst['name'])}-{time.strftime('%Y%m%d-%H%M%S')}.png")
        tmp = out + ".ppm"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            qmp.ppm_to_png(tmp, out)
        except (OSError, ValueError) as e:
            messagebox.showerror("Screenshot", str(e), parent=self)
            return
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self.set_message(f"Saved {out}")

    # ---- lifecycle -------------------------------------------------------------
    def close(self) -> None:
        if self._closed.is_set():
            return
        self._release_all()
        self._closed.set()
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        self.destroy()
        if self._on_close:
            self._on_close(self)


class KeymapDialog(tk.Toplevel):
    def __init__(self, master, entries: list[dict], on_change):
        super().__init__(master)
        self.title("Keymap")
        self.configure(bg=BG)
        self.transient(master)
        self._entries = list(entries)
        self._on_change = on_change
        tk.Label(self, text="Each key taps its position on the screen while held.",
                 bg=BG, fg=FG, anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        self.tree = _tree(self, ("Key", "X", "Y"), 320)
        self._fill()
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=10, pady=10)
        tk.Button(row, text="Delete selected", command=self._delete).pack(side="left")
        tk.Button(row, text="Close", command=self.destroy).pack(side="right")

    def _fill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self._entries):
            self.tree.insert("", "end", iid=str(i), values=(e["key"], f"{e['x']:.3f}", f"{e['y']:.3f}"))

    def _delete(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        drop = {int(i) for i in sel}
        self._entries = [e for i, e in enumerate(self._entries) if i not in drop]
        self._on_change(self._entries)
        self._fill()


class MacroDialog(tk.Toplevel):
    def __init__(self, master, p: dict, on_play):
        super().__init__(master)
        self.title("Macros")
        self.configure(bg=BG)
        self.transient(master)
        self._base = p["base"]
        self._on_play = on_play
        tk.Label(self, text="Recorded input. Playing a macro sends it to the VM (or all synced VMs).",
                 bg=BG, fg=FG, anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        self.list = tk.Listbox(self, width=36, height=10, bg="#101216", fg=FG, bd=0,
                               selectbackground=ACCENT, selectforeground="black")
        self.list.pack(fill="both", expand=True, padx=10)
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=10, pady=10)
        tk.Button(row, text="Play", command=self._play).pack(side="left")
        tk.Button(row, text="Delete", command=self._delete).pack(side="left", padx=6)
        tk.Button(row, text="Close", command=self.destroy).pack(side="right")
        self._refresh()

    def _refresh(self) -> None:
        self.list.delete(0, "end")
        for name in automation.list_macros(self._base):
            self.list.insert("end", name)

    def _selected(self) -> str | None:
        sel = self.list.curselection()
        return self.list.get(sel[0]) if sel else None

    def _play(self) -> None:
        name = self._selected()
        if name:
            self._on_play(name)

    def _delete(self) -> None:
        name = self._selected()
        if name and messagebox.askyesno("Macros", f"Delete macro '{name}'?", parent=self):
            automation.delete_macro(self._base, name)
            self._refresh()


def _tree(master, columns, width: int):
    from tkinter import ttk
    tree = ttk.Treeview(master, columns=columns, show="headings", height=8,
                        selectmode="extended")
    for c in columns:
        tree.heading(c, text=c)
        tree.column(c, width=width // len(columns), anchor="w")
    tree.pack(fill="both", expand=True, padx=10)
    return tree
