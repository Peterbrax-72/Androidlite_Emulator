"""Tkinter front end (standard library only): engine status, the instance manager, and the log.

Long work runs on threads from core.py. Threads report back through a queue that the
Tk thread drains every 80 ms, so widgets are only touched from one thread.
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import automation, config, core, instances, qmp, viewer, vmio

BG = "#1b1d22"
CARD = "#262932"
FG = "#e8eaed"
MUTED = "#9aa0a6"
GREEN = "#3ddc84"
AMBER = "#fbbc04"
RED = "#ea4335"
UI_FONT = ("Segoe UI", 10)
UI_BOLD = ("Segoe UI", 10, "bold")
TITLE_FONT = ("Segoe UI", 18, "bold")
LOG_FONT = ("Consolas", 9)
COLORS = {"ok": GREEN, "warn": AMBER, "bad": RED, "idle": MUTED}
MAX_LOG_LINES = 400
RECONNECT_EVERY_MS = 5000


class _QueueHandler(logging.Handler):
    """Forwards log records to the Tk thread."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


class InstanceDialog(tk.Toplevel):
    """Create or edit an instance: name, profile, RAM and vCPUs. self.result is None if cancelled."""

    def __init__(self, master, title: str, name: str = "", profile: str = "standard",
                 ram: int | None = None, cpus: int | None = None, allow_profile: bool = True):
        super().__init__(master)
        self.title(title)
        self.configure(bg=CARD, padx=18, pady=14)
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        prof = config.PROFILES[profile]
        self.name_var = tk.StringVar(value=name)
        self.profile_var = tk.StringVar(value=profile)
        self.ram_var = tk.StringVar(value=str(ram or prof["ram_mb"]))
        self.cpu_var = tk.StringVar(value=str(cpus or prof["cpus"]))

        tk.Label(self, text="Name", bg=CARD, fg=FG, font=UI_BOLD).grid(row=0, column=0, sticky="w", pady=4)
        tk.Entry(self, textvariable=self.name_var, font=UI_FONT, width=28).grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=4)
        if allow_profile:
            tk.Label(self, text="Profile", bg=CARD, fg=FG, font=UI_BOLD).grid(row=1, column=0, sticky="nw", pady=4)
            pf = tk.Frame(self, bg=CARD)
            pf.grid(row=1, column=1, columnspan=3, sticky="w", pady=4)
            for key, prof_def in config.PROFILES.items():
                tk.Radiobutton(pf, text=f"{prof_def['label']} ({prof_def['ram_mb'] / 1024:.1f} GB RAM)",
                               value=key, variable=self.profile_var, command=self._apply_profile,
                               bg=CARD, fg=FG, selectcolor=BG, activebackground=CARD,
                               activeforeground=FG, font=UI_FONT, highlightthickness=0).pack(anchor="w")
        tk.Label(self, text="RAM (MB)", bg=CARD, fg=FG, font=UI_FONT).grid(row=2, column=0, sticky="w", pady=4)
        tk.Spinbox(self, from_=config.RAM_RANGE[0], to=config.RAM_RANGE[1], increment=256,
                   textvariable=self.ram_var, width=8, font=UI_FONT).grid(row=2, column=1, sticky="w", pady=4)
        tk.Label(self, text="vCPUs", bg=CARD, fg=FG, font=UI_FONT).grid(row=2, column=2, sticky="w", padx=(14, 0))
        tk.Spinbox(self, from_=config.CPU_RANGE[0], to=config.CPU_RANGE[1], textvariable=self.cpu_var,
                   width=5, font=UI_FONT).grid(row=2, column=3, sticky="w")
        tk.Label(self, text="Lite uses less memory; use it on PCs with 8 GB or less.",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 8)).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 8))
        bf = tk.Frame(self, bg=CARD)
        bf.grid(row=4, column=0, columnspan=4, sticky="e")
        tk.Button(bf, text="Cancel", command=self.destroy, bg=BG, fg=FG, relief="flat", padx=12, pady=6,
                  activebackground=BG, activeforeground=FG).pack(side="left", padx=4)
        tk.Button(bf, text="Save", command=self._save, bg=GREEN, fg="#0b0c0e", relief="flat", padx=16, pady=6,
                  activebackground=GREEN, font=UI_BOLD).pack(side="left", padx=4)
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window(self)

    def _apply_profile(self):
        prof = config.PROFILES[self.profile_var.get()]
        self.ram_var.set(str(prof["ram_mb"]))
        self.cpu_var.set(str(prof["cpus"]))

    def _save(self):
        try:
            ram = int(self.ram_var.get())
            cpus = int(self.cpu_var.get())
        except ValueError:
            messagebox.showerror("Android Lite", "RAM and vCPUs must be whole numbers.", parent=self)
            return
        self.result = {"name": self.name_var.get().strip(), "profile": self.profile_var.get(),
                       "ram_mb": ram, "cpus": cpus}
        self.destroy()


class App(tk.Tk):
    def __init__(self, p: dict | None = None):
        super().__init__()
        self.p = p or config.paths()
        core.setup_logging(self.p)
        self.settings = config.load_settings(self.p)
        self.host_mb = config.host_ram_mb()
        self.q: queue.Queue = queue.Queue()
        self.busy = False                 # global background task: setup or clone
        self.task: str | None = None
        self.vms: dict[str, dict] = {}    # instance id -> {"proc", "port", "paused", "stopping"}
        self.viewers: dict[str, viewer.Viewer] = {}   # instance id -> open VM window
        self.sync_on = False              # Synchronizer: input goes to every running VM
        self._accel: tuple[str, str] | None = None
        self.items: list[dict] = []

        self.title(f"Android Lite {config.APP_VERSION}")
        self.configure(bg=BG)
        self.geometry("900x940")
        self.minsize(820, 860)
        self._style()
        self._build()

        handler = _QueueHandler(self.q)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("alite").addHandler(handler)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._reattach()
        self.refresh_tree()
        self.refresh_status()
        logging.getLogger("alite").info("Ready. %d instance(s). Host RAM: %s", len(self.items),
                                        f"{self.host_mb} MB" if self.host_mb else "unknown")
        self.after(80, self._pump)
        self.after(RECONNECT_EVERY_MS, self._reap_tick)

    # ---------- layout ----------
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Horizontal.TProgressbar", background=GREEN, troughcolor=CARD,
                     bordercolor=BG, lightcolor=GREEN, darkcolor=GREEN)
        st.configure("Treeview", background=CARD, foreground=FG, fieldbackground=CARD,
                     bordercolor=CARD, rowheight=26, font=UI_FONT)
        st.configure("Treeview.Heading", background=BG, foreground=MUTED, relief="flat", font=UI_BOLD)
        st.map("Treeview", background=[("selected", "#2f5b45")], foreground=[("selected", FG)])

    def _card(self, title: str, expand: bool = False) -> tk.Frame:
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both" if expand else "x", padx=16, pady=6, expand=expand)
        tk.Label(outer, text=title.upper(), font=("Segoe UI", 8, "bold"), fg=MUTED, bg=BG).pack(anchor="w", padx=4)
        inner = tk.Frame(outer, bg=CARD, padx=12, pady=8)
        inner.pack(fill="both" if expand else "x", expand=expand, pady=(2, 0))
        return inner

    def _build(self):
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(head, text="Android Lite", font=TITLE_FONT, fg=FG, bg=BG).pack(anchor="w")
        tk.Label(head, text="Android 9 (x86-64) instances on QEMU, accelerated by Windows Hypervisor Platform",
                 font=UI_FONT, fg=MUTED, bg=BG).pack(anchor="w")

        status = self._card("Engine")
        self.rows: dict[str, tuple[tk.Label, tk.Label]] = {}
        for key, title in [("engine", "QEMU engine"), ("accel", "Hardware acceleration"),
                           ("image", "Android image")]:
            row = tk.Frame(status, bg=CARD)
            row.pack(fill="x", pady=2)
            dot = tk.Label(row, text="\u25CF", font=UI_BOLD, fg=MUTED, bg=CARD, width=2)
            dot.pack(side="left")
            tk.Label(row, text=title, font=UI_BOLD, fg=FG, bg=CARD, width=20, anchor="w").pack(side="left")
            detail = tk.Label(row, text="", font=UI_FONT, fg=MUTED, bg=CARD, anchor="w",
                              justify="left", wraplength=600)
            detail.pack(side="left", fill="x", expand=True)
            self.rows[key] = (dot, detail)

        inst = self._card("Instances", expand=True)
        cols = ("name", "profile", "ram", "cpus", "state", "disk")
        self.tree = ttk.Treeview(inst, columns=cols, show="headings", height=5, selectmode="browse")
        for col, text, width in [("name", "Name", 190), ("profile", "Profile", 90), ("ram", "RAM", 90),
                                 ("cpus", "vCPUs", 70), ("state", "State", 120), ("disk", "Disk", 120)]:
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, anchor="w", stretch=col == "name")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_buttons())
        self.tree.bind("<Double-1>", lambda e: self.on_start())

        acts = self._card("Actions")
        spec = [
            [("new", "New instance", self.on_new), ("clone", "Clone", self.on_clone),
             ("edit", "Edit settings", self.on_edit), ("remove", "Remove", self.on_remove)],
            [("start", "Start", self.on_start), ("pause", "Pause", self.on_pause),
             ("stop", "Stop", self.on_stop), ("shot", "Screenshot", self.on_shot)],
            [("install", "Install Android", self.on_install), ("apk", "Install APK", self.on_apk),
             ("shared", "Shared folder", self.on_shared), ("setup", "Set up", self.on_setup)],
            [("view", "Open window", self.on_view), ("sync", "Synchronizer: off", self.on_sync),
             ("macros", "Macros", self.on_macros), ("reattach", "Reconnect", self.on_reattach)],
        ]
        grid = tk.Frame(acts, bg=CARD)
        grid.pack(fill="x")
        self.btn: dict[str, tk.Button] = {}
        for r, row in enumerate(spec):
            for c, (key, label, cmd) in enumerate(row):
                b = tk.Button(grid, text=label, command=cmd, font=UI_BOLD, bg=CARD, fg=FG,
                              activebackground=BG, activeforeground=FG, relief="flat", bd=0,
                              padx=8, pady=8, cursor="hand2")
                b.grid(row=r, column=c, sticky="ew", padx=4, pady=3)
                self.btn[key] = b
        for c in range(4):
            grid.columnconfigure(c, weight=1)
        self.btn["start"].config(bg=GREEN, fg="#0b0c0e", activebackground=GREEN)

        self.bar = ttk.Progressbar(self, mode="determinate", style="Horizontal.TProgressbar")
        self.bar.pack(fill="x", padx=16, pady=(10, 2))
        self.status_line = tk.Label(self, text="Ready", font=UI_FONT, fg=FG, bg=BG, anchor="w")
        self.status_line.pack(fill="x", padx=16)

        logcard = self._card("Log", expand=True)
        self.log = tk.Text(logcard, height=6, bg="#111317", fg=MUTED, font=LOG_FONT,
                           relief="flat", wrap="word", state="disabled", bd=0)
        self.log.pack(fill="both", expand=True)

        tk.Label(self, text="Google Play apps need a GApps install inside the VM (see README). "
                            "Some apps refuse to run in virtual machines; that is their choice.",
                 font=("Segoe UI", 8), fg=MUTED, bg=BG, wraplength=860,
                 justify="left").pack(fill="x", padx=16, pady=(4, 10))

    # ---------- state helpers ----------
    def _sel_id(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _sel(self) -> dict | None:
        iid = self._sel_id()
        if not iid:
            return None
        try:
            return instances.get(self.items, iid)
        except KeyError:
            return None

    def _state_text(self, iid: str) -> str:
        v = self.vms.get(iid)
        if v is None:
            return "Stopped"
        if v["stopping"]:
            return "Stopping..."
        if v["port"] is None:
            return "Starting..."
        return "Paused" if v["paused"] else "Running"

    def refresh_tree(self, select: str | None = None):
        prev = select or self._sel_id()
        self.items = instances.load(self.p, self.settings)
        self.tree.delete(*self.tree.get_children())
        for it in self.items:
            has_disk = os.path.isfile(instances.resolve(self.p, it)["disk"])
            label = config.PROFILES.get(it["profile"], {}).get("label", it["profile"])
            self.tree.insert("", "end", iid=it["id"], values=(
                it["name"], label, f"{it['ram_mb']} MB", it["cpus"],
                self._state_text(it["id"]), "ready" if has_disk else "no disk"))
        if prev and self.tree.exists(prev):
            self.tree.selection_set(prev)
            self.tree.see(prev)
        elif self.items:
            self.tree.selection_set(self.items[0]["id"])
        self._refresh_buttons()

    def refresh_status(self):
        s = self.settings
        qemu = core.find_qemu(self.p, s)
        if qemu:
            self._set_row("engine", "ok", qemu)
            if self._accel is None:
                self._accel = core.detect_accel(qemu)
            accel, why = self._accel
            self._set_row("accel", "ok" if accel != "tcg" else "warn", f"{accel.upper()} - {why}")
        else:
            self._set_row("engine", "bad", "Not installed. 'Set up' downloads it once (about 200 MB).")
            self._set_row("accel", "idle", "Checked after QEMU is installed")
        if os.path.isfile(self.p["iso"]):
            mb = os.path.getsize(self.p["iso"]) // core.MIB
            self._set_row("image", "ok", f"Android-x86 9.0 r2, {mb} MB")
        else:
            self._set_row("image", "bad", "Not downloaded. 'Set up' fetches it (about 920 MB).")

    def _set_row(self, key: str, state: str, text: str):
        dot, detail = self.rows[key]
        dot.config(fg=COLORS[state])
        detail.config(text=text, fg=FG if state != "idle" else MUTED)

    def _refresh_buttons(self):
        sel = self._sel()
        idle = not self.busy
        v = self.vms.get(sel["id"]) if sel else None
        running = v is not None
        ready_vm = bool(running and v["port"] is not None and not v["stopping"])
        has_disk = bool(sel) and os.path.isfile(instances.resolve(self.p, sel)["disk"])

        def en(ok: bool):
            return "normal" if ok else "disabled"

        b = self.btn
        b["new"].config(state=en(idle))
        b["clone"].config(state=en(idle and bool(sel) and not running and has_disk))
        b["edit"].config(state=en(idle and bool(sel) and not running))
        b["remove"].config(state=en(idle and bool(sel) and not running))
        b["start"].config(state=en(bool(sel) and not running and has_disk))
        b["install"].config(state=en(bool(sel) and not running and idle))
        b["pause"].config(state=en(ready_vm), text="Resume" if (v and v["paused"]) else "Pause")
        b["stop"].config(state=en(ready_vm))
        b["shot"].config(state=en(ready_vm))
        b["view"].config(state=en(ready_vm))
        b["sync"].config(state=en(bool(self._running_ports())),
                         text="Synchronizer: on" if self.sync_on else "Synchronizer: off")
        b["macros"].config(state=en(idle))
        b["reattach"].config(state=en(idle))
        b["apk"].config(state=en(bool(sel)))
        b["shared"].config(state=en(bool(sel)))
        b["setup"].config(state=en(idle))

    # ---------- user actions ----------
    def _suggest_name(self) -> str:
        n = len(self.items) + 1
        names = {i["name"].lower() for i in self.items}
        while f"android {n}".lower() in names:
            n += 1
        return f"Android {n}"

    def on_new(self):
        dlg = InstanceDialog(self, "New instance", name=self._suggest_name(), profile=self.settings["profile"])
        r = dlg.result
        if not r:
            return
        try:
            entry = instances.create(self.p, self.settings, r["name"], r["profile"], r["ram_mb"], r["cpus"])
        except (ValueError, OSError) as e:
            messagebox.showerror("Android Lite", str(e))
            return
        self.refresh_tree(select=entry["id"])
        logging.getLogger("alite").info("Next: select '%s' and click Install Android.", entry["name"])

    def on_clone(self):
        sel = self._sel()
        if not sel:
            return
        name = simpledialog.askstring("Clone instance", "Name for the copy:",
                                      initialvalue=f"{sel['name']} copy", parent=self)
        if not name or not name.strip():
            return
        self._start_task("clone", lambda cb: instances.clone(self.p, self.settings, sel["id"], name, progress=cb))

    def on_edit(self):
        sel = self._sel()
        if not sel:
            return
        dlg = InstanceDialog(self, "Edit instance", name=sel["name"], profile=sel["profile"],
                             ram=sel["ram_mb"], cpus=sel["cpus"])
        r = dlg.result
        if not r:
            return
        try:
            instances.update(self.p, self.settings, sel["id"], name=r["name"], profile=r["profile"],
                             ram_mb=r["ram_mb"], cpus=r["cpus"])
        except ValueError as e:
            messagebox.showerror("Android Lite", str(e))
            return
        self.refresh_tree(select=sel["id"])

    def on_remove(self):
        sel = self._sel()
        if not sel:
            return
        if not messagebox.askyesno("Android Lite",
                                   f"Delete '{sel['name']}' and everything in it (apps, data, shared folder)?"):
            return
        try:
            instances.remove(self.p, self.settings, sel["id"])
        except OSError as e:
            messagebox.showerror("Android Lite", f"Could not delete all files: {e}")
        self.refresh_tree()

    def on_start(self):
        sel = self._sel()
        if not sel or sel["id"] in self.vms:
            return
        running = [i["ram_mb"] for i in self.items if i["id"] in self.vms]
        ok, msg = config.ram_budget_ok(sel["ram_mb"], running, self.host_mb)
        if not ok and not messagebox.askyesno("Android Lite", msg + "\n\nStart anyway?"):
            return
        self._launch(sel, install=False)

    def on_install(self):
        sel = self._sel()
        if not sel or sel["id"] in self.vms:
            return
        if os.path.isfile(instances.resolve(self.p, sel)["disk"]) and not messagebox.askyesno(
                "Android Lite",
                f"'{sel['name']}' already has a disk. Boot the installer on it again?\n\n"
                "Use Remove and create a new instance for a clean install."):
            return
        self._launch(sel, install=True)

    def _launch(self, inst: dict, install: bool):
        iid = inst["id"]
        self.vms[iid] = {"proc": None, "port": None, "paused": False, "stopping": False}
        self.refresh_tree(select=iid)
        threading.Thread(target=self._vm_worker, args=(dict(inst), install), daemon=True).start()

    def _vm_worker(self, inst: dict, install: bool):
        iid = inst["id"]

        def cb(done, total):
            self.q.put(("progress", done, total))

        def on_start(proc, port):
            self.q.put(("vm_started", iid, proc, port))
        try:
            rc = core.start_instance(self.p, self.settings, inst, install=install,
                                     progress=cb, on_start=on_start)
            self.q.put(("vm_exited", iid, rc, None))
        except core.SetupError as e:
            self.q.put(("vm_exited", iid, None, str(e)))
        except Exception as e:
            logging.getLogger("alite").exception("Starting '%s' failed", inst["name"])
            self.q.put(("vm_exited", iid, None, f"Unexpected error: {e}\n\nSee the log for details."))

    def on_stop(self):
        sel = self._sel()
        v = self.vms.get(sel["id"]) if sel else None
        if not v or v["stopping"] or v["port"] is None:
            return
        v["stopping"] = True
        self.refresh_tree(select=sel["id"])
        threading.Thread(target=self._stop_worker, args=(sel["id"], sel["name"], v["proc"], v["port"]),
                         daemon=True).start()

    def _stop_worker(self, iid, name, proc, port):
        try:
            msg = core.stop_vm(port, proc)
            self.q.put(("log", f"'{name}': {msg}"))
        except Exception as e:
            self.q.put(("log", f"Stop failed for '{name}': {e}"))
        if proc is None:                      # reattached VM: we have no process to wait on
            self.q.put(("vm_exited", iid, 0, None))

    # ---------- windows, input and macros ----------
    def on_view(self):
        sel = self._sel()
        v = self.vms.get(sel["id"]) if sel else None
        if v and v["port"] is not None:
            self._open_viewer(sel["id"])

    def _open_viewer(self, iid: str):
        existing = self.viewers.get(iid)
        if existing is not None:
            existing.lift()
            return
        try:
            inst = instances.get(self.items, iid)
        except KeyError:
            return
        port = self.vms[iid]["port"]
        win = viewer.Viewer(self, self.p, inst, port,
                            dispatch=lambda events, iid=iid: self._dispatch(iid, events),
                            on_close=lambda w, iid=iid: self._viewer_closed(iid, w))
        win.set_sync(self.sync_on)
        self.viewers[iid] = win

    def _viewer_closed(self, iid: str, win):
        if self.viewers.get(iid) is win:
            del self.viewers[iid]

    def _close_viewer(self, iid: str):
        win = self.viewers.pop(iid, None)
        if win is not None:
            win.close()

    def _running_ports(self) -> list[int]:
        return [v["port"] for v in self.vms.values() if v["port"] is not None and not v["stopping"]]

    def _dispatch(self, source_iid: str, events: list[dict]):
        """Route input from a window: to its own VM, or to every running VM while Synchronizer is on."""
        if self.sync_on:
            ports = self._running_ports()
        else:
            v = self.vms.get(source_iid)
            ports = [v["port"]] if v and v["port"] is not None else []
        for port in ports:
            vmio.sender_for(port).send(events)

    def on_sync(self):
        self.sync_on = not self.sync_on
        for win in self.viewers.values():
            win.set_sync(self.sync_on)
        if self.sync_on:
            self._on_log(f"Synchronizer on: input in any window goes to {len(self._running_ports())} "
                         "running instance(s). Keymaps and macros are applied on the window they come from.")
        else:
            self._on_log("Synchronizer off: input goes only to its own window.")
        self._refresh_buttons()

    def on_macros(self):
        viewer.MacroDialog(self, self.p, on_play=self._play_everywhere)

    def _play_everywhere(self, name: str):
        ports = self._running_ports()
        if not ports:
            messagebox.showinfo("Macros", "Start an instance first. Macros play on running instances.")
            return
        try:
            events = automation.to_playback(automation.load_macro(self.p["base"], name))
        except (OSError, ValueError, automation.AutomationError) as e:
            messagebox.showerror("Macros", str(e))
            return
        for port in ports:
            vmio.sender_for(port).send(events)
        self._on_log(f"Playing '{name}' on {len(ports)} running instance(s)")

    def on_reattach(self):
        """Look again for VMs started by an earlier session."""
        self._reattach()
        self.refresh_tree()

    def on_pause(self):
        sel = self._sel()
        v = self.vms.get(sel["id"]) if sel else None
        if not v or v["port"] is None:
            return
        want_pause = not v["paused"]
        threading.Thread(target=self._pause_worker, args=(sel["id"], sel["name"], v["port"], want_pause),
                         daemon=True).start()

    def _pause_worker(self, iid, name, port, pause):
        try:
            state = core.pause_vm(port, pause)
            self.q.put(("paused", iid, pause))
            self.q.put(("log", f"'{name}': {state}"))
        except Exception as e:
            self.q.put(("log", f"Could not {'pause' if pause else 'resume'} '{name}': {e}"))

    def on_shot(self):
        sel = self._sel()
        v = self.vms.get(sel["id"]) if sel else None
        if not v or v["port"] is None:
            return
        out = os.path.join(self.p["base"], "screenshots",
                           f"{sel['name']}-{time.strftime('%Y%m%d-%H%M%S')}.png")
        threading.Thread(target=self._shot_worker, args=(out, v["port"]), daemon=True).start()

    def _shot_worker(self, out, port):
        try:
            qmp.screendump_png(port, out)
            self.q.put(("log", f"Screenshot saved: {out}"))
        except Exception as e:
            self.q.put(("log", f"Screenshot failed: {e}"))

    def on_apk(self):
        sel = self._sel()
        if not sel:
            return
        path = filedialog.askopenfilename(title="Choose an APK", filetypes=[("Android apps", "*.apk"),
                                                                             ("All files", "*.*")])
        if not path:
            return
        try:
            dest = core.install_apk(instances.resolve(self.p, sel)["shared"], path)
        except core.SetupError as e:
            messagebox.showerror("Install APK", str(e))
            return
        messagebox.showinfo(
            "Install APK",
            f"Copied to the shared folder:\n{dest}\n\n"
            "In Android, open the Files app, go to the USB storage and tap the APK to install it.\n"
            "(This path is not yet verified on Windows; see the README.)")

    def on_shared(self):
        sel = self._sel()
        if not sel:
            return
        path = core.ensure_shared(instances.resolve(self.p, sel)["shared"])
        try:
            if sys.platform == "win32":
                os.startfile(path)              # noqa: S606 (Windows only)
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as e:
            messagebox.showinfo("Android Lite", f"Shared folder: {path}\n({e})")

    def on_setup(self):
        self._start_task("setup", lambda cb: core.setup_base(self.p, self.settings, cb))

    def on_close(self):
        if self.vms and not messagebox.askyesno(
                "Android Lite",
                f"{len(self.vms)} instance(s) are running. They keep running in the background and "
                "reconnect the next time you open Android Lite.\n\nQuit the app?"):
            return
        for win in list(self.viewers.values()):
            win.close()
        self.destroy()

    # ---------- background work ----------
    def _start_task(self, name: str, fn):
        if self.busy:
            return
        self.busy = True
        self.task = name
        self.bar.config(mode="determinate", maximum=100, value=0)
        self.status_line.config(text=f"{name.capitalize()}...", fg=FG)
        self._refresh_buttons()
        threading.Thread(target=self._task_worker, args=(name, fn), daemon=True).start()

    def _task_worker(self, name: str, fn):
        def cb(done, total):
            self.q.put(("progress", done, total))
        try:
            fn(cb)
            self.q.put(("done", name, None))
        except core.SetupError as e:
            self.q.put(("done", name, str(e)))
        except Exception as e:
            logging.getLogger("alite").exception("Task '%s' failed", name)
            self.q.put(("done", name, f"Unexpected error: {e}\n\nSee the log for details."))

    def _reattach(self):
        """Reconnect to VMs that were started by an earlier session and are still running."""
        for it in instances.load(self.p, self.settings):
            port = it.get("qmp_port")
            if not port:
                continue
            st = qmp.query_status(port, timeout=0.5)
            if st:
                self.vms[it["id"]] = {"proc": None, "port": port, "paused": st == "paused", "stopping": False}
                logging.getLogger("alite").info("Reconnected to running instance '%s'", it["name"])
            else:
                instances.set_port(self.p, self.settings, it["id"], None)

    def _reap_tick(self):
        """Every few seconds, drop reconnected VMs that have stopped."""
        ids = [i for i, v in self.vms.items() if v["proc"] is None and v["port"] is not None]
        if ids:
            threading.Thread(target=self._reap_worker, args=(ids,), daemon=True).start()
        self.after(RECONNECT_EVERY_MS, self._reap_tick)

    def _reap_worker(self, ids):
        gone = []
        for i in ids:
            port = self.vms.get(i, {}).get("port")
            if port and qmp.query_status(port, 0.5) is None:
                gone.append(i)
        if gone:
            self.q.put(("dead", gone))

    # ---------- message pump (Tk thread only) ----------
    def _pump(self):
        handlers = {
            "log": self._on_log,
            "progress": self._on_progress,
            "done": self._on_done,
            "vm_started": self._on_vm_started,
            "vm_exited": self._on_vm_exited,
            "paused": self._on_paused,
            "dead": self._on_dead,
        }
        try:
            while True:
                item = self.q.get_nowait()
                handlers[item[0]](*item[1:])
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _on_log(self, msg: str):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_progress(self, done: int, total: int):
        if total:
            self.bar.config(maximum=total, value=done)
            self.status_line.config(text=f"{(self.task or 'Working').capitalize()}... {done * 100 // total}%")

    def _on_done(self, name: str, err: str | None):
        self.busy = False
        self.task = None
        self.bar.config(value=0)
        if err:
            self.status_line.config(text=f"{name.capitalize()} failed", fg=RED)
            messagebox.showerror("Android Lite", err)
        else:
            self.status_line.config(text=f"{name.capitalize()} finished", fg=GREEN)
        self.refresh_status()
        self.refresh_tree()

    def _on_vm_started(self, iid: str, proc, port: int):
        old = self.vms.get(iid, {}).get("port")
        if old is not None and old != port:          # software-emulation retry: new QEMU, new port
            vmio.drop_sender(old)
        self._close_viewer(iid)
        self.vms[iid] = {"proc": proc, "port": port, "paused": False, "stopping": False}
        try:
            instances.set_port(self.p, self.settings, iid, port)
        except KeyError:
            pass
        self.refresh_tree()
        self._open_viewer(iid)

    def _on_vm_exited(self, iid: str, rc, err: str | None):
        self._forget_vm(iid)
        try:
            instances.set_port(self.p, self.settings, iid, None)
        except KeyError:
            pass
        if err:
            self.status_line.config(text="Could not start the instance", fg=RED)
            messagebox.showerror("Android Lite", err)
        else:
            self.status_line.config(text="Instance closed", fg=FG)
        self.refresh_status()
        self.refresh_tree()

    def _on_paused(self, iid: str, paused: bool):
        v = self.vms.get(iid)
        if v:
            v["paused"] = paused
        self.refresh_tree()

    def _forget_vm(self, iid: str):
        self._close_viewer(iid)
        port = self.vms.get(iid, {}).get("port")
        if port is not None:
            vmio.drop_sender(port)
        self.vms.pop(iid, None)

    def _on_dead(self, ids: list[str]):
        for iid in ids:
            self._forget_vm(iid)
            try:
                instances.set_port(self.p, self.settings, iid, None)
            except KeyError:
                pass
        self.refresh_tree()
