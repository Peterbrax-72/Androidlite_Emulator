"""Tests for the window, input, keymap, synchronizer and macro features.

    python -m unittest discover -s tests -v

Pure-logic tests run everywhere. Window tests need a display (DISPLAY set; run under Xvfb on
Linux). The end-to-end test needs QEMU and is skipped without it.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from android_lite import automation, config, core, instances, qmp, vmio  # noqa: E402

QEMU = os.environ.get("ALITE_QEMU") or shutil.which("qemu-system-x86_64")
HAVE_DISPLAY = bool(os.environ.get("DISPLAY"))


class KeysymTests(unittest.TestCase):
    def test_letters_digits_and_specials(self):
        self.assertEqual(vmio.resolve_keysym("a"), ("a", False))
        self.assertEqual(vmio.resolve_keysym("7"), ("7", False))
        self.assertEqual(vmio.resolve_keysym("Return"), ("ret", False))
        self.assertEqual(vmio.resolve_keysym("space"), ("spc", False))
        self.assertEqual(vmio.resolve_keysym("F12"), ("f12", False))
        self.assertEqual(vmio.resolve_keysym("XF86AudioRaiseVolume"), ("volumeup", False))

    def test_uppercase_and_shifted_symbols_need_shift(self):
        self.assertEqual(vmio.resolve_keysym("A"), ("a", True))
        self.assertEqual(vmio.resolve_keysym("exclam"), ("1", True))
        self.assertEqual(vmio.resolve_keysym("underscore"), ("minus", True))

    def test_unknown_keysym_is_not_sent(self):
        self.assertIsNone(vmio.resolve_keysym("Kanji"))
        self.assertIsNone(vmio.resolve_keysym("dead_acute"))


class QmpPayloadTests(unittest.TestCase):
    def test_normalized_events_become_qmp_events(self):
        out = vmio.to_qmp([
            vmio.ev_move(0.5, 0.25),
            vmio.ev_down(0.5, 0.25, "left"),
            vmio.ev_up("left"),
            vmio.ev_key("a", True),
            {"op": "wait", "ms": 10},
        ])
        self.assertEqual(out[0], {"type": "abs", "data": {"axis": "x", "value": round(0.5 * vmio.ABS_MAX)}})
        self.assertEqual(out[1], {"type": "abs", "data": {"axis": "y", "value": round(0.25 * vmio.ABS_MAX)}})
        self.assertEqual(out[2]["type"], "abs")
        self.assertEqual(out[4], {"type": "btn", "data": {"down": True, "button": "left"}})
        self.assertEqual(out[5], {"type": "btn", "data": {"down": False, "button": "left"}})
        self.assertEqual(out[6], {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": "a"}}})
        self.assertEqual(len(out), 7)                 # the wait is not sent

    def test_positions_are_clamped(self):
        self.assertEqual(vmio.ev_move(-1, 2), {"op": "move", "x": 0.0, "y": 1.0})


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._orig = qmp.command
        qmp.command = lambda port, name, args=None, timeout=5.0: self.sent.append((port, name, args))

    def tearDown(self):
        qmp.command = self._orig

    def test_events_are_sent_in_order_and_waits_are_honoured(self):
        s = vmio.Sender(4242)
        s.send([vmio.ev_key("a", True), vmio.ev_key("a", False), {"op": "wait", "ms": 30},
                vmio.ev_key("b", True)])
        s.close()
        s._thread.join(timeout=5)
        keys = [(name, a["events"]) for _port, name, a in self.sent]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0][1][0]["data"]["key"]["data"], "a")
        self.assertEqual(keys[1][1][0]["data"]["key"]["data"], "b")
        self.assertTrue(all(name == "input-send-event" for _p, name, _a in self.sent))

    def test_play_blocking_sends_everything_before_returning(self):
        events = [vmio.ev_key("c", True), {"op": "wait", "ms": 5}, vmio.ev_key("c", False)]
        vmio.play_blocking(7, events)
        self.assertEqual(len(self.sent), 2)

    def test_send_failure_does_not_raise(self):
        def boom(*a, **k):
            raise qmp.QMPError("gone")
        qmp.command = boom
        s = vmio.Sender(9)
        s.send([vmio.ev_key("a", True)])
        s.close()
        s._thread.join(timeout=5)
        self.assertTrue(s.failed)


class KeymapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip_and_validation(self):
        path = os.path.join(self.tmp, "keymap.json")
        automation.save_keymap(path, [{"key": "w", "x": 0.1, "y": 0.9}])
        self.assertEqual(automation.load_keymap(path), [{"key": "w", "x": 0.1, "y": 0.9}])
        with self.assertRaises(automation.AutomationError):
            automation.validate_entry({"key": "w", "x": 1.5, "y": 0.2})
        with self.assertRaises(automation.AutomationError):
            automation.validate_entry({"key": " ", "x": 0.2, "y": 0.2})

    def test_broken_file_gives_empty_keymap(self):
        path = os.path.join(self.tmp, "keymap.json")
        with open(path, "w") as f:
            f.write("{nope")
        self.assertEqual(automation.load_keymap(path), [])

    def test_mapped_key_taps_position_on_press_and_releases_on_up(self):
        entries = [{"key": "w", "x": 0.3, "y": 0.4}]
        down = automation.keymap_events(entries, "w", True)
        self.assertEqual(down[0]["op"], "move")
        self.assertEqual(down[1], {"op": "down", "x": 0.3, "y": 0.4, "btn": "left"})
        self.assertEqual(automation.keymap_events(entries, "w", False), [{"op": "up", "btn": "left"}])
        self.assertIsNone(automation.keymap_events(entries, "q", True))


class MacroTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_save_list_load_delete(self):
        events = [vmio.ev_key("a", True), vmio.ev_key("a", False)]
        automation.save_macro(self.base, "Daily login", events)
        self.assertEqual(automation.list_macros(self.base), ["Daily login"])
        self.assertEqual(automation.load_macro(self.base, "Daily login"), events)
        automation.delete_macro(self.base, "Daily login")
        self.assertEqual(automation.list_macros(self.base), [])

    def test_names_are_checked(self):
        for bad in ["", "../x", "a" * 33, "x/y"]:
            with self.assertRaises(automation.AutomationError):
                automation.save_macro(self.base, bad, [vmio.ev_key("a", True)])
        with self.assertRaises(automation.AutomationError):
            automation.save_macro(self.base, "empty", [])

    def test_recorder_timing_becomes_waits(self):
        rec = automation.Recorder()
        rec.add([vmio.ev_key("a", True)])
        time.sleep(0.05)
        rec.add([vmio.ev_key("a", False)])
        play = automation.to_playback(rec.events)
        self.assertEqual(play[0]["op"], "key")
        self.assertEqual(play[1]["op"], "wait")
        self.assertGreaterEqual(play[1]["ms"], 40)
        self.assertNotIn("t", play[2])

    def test_recorder_ignores_waits(self):
        rec = automation.Recorder()
        rec.add([{"op": "wait", "ms": 100}])
        self.assertEqual(rec.events, [])


@unittest.skipUnless(HAVE_DISPLAY, "needs a display (Xvfb)")
class ViewerTests(unittest.TestCase):
    """The viewer's key and mouse logic, with a dispatch callback instead of a live VM."""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        cls.tmp = tempfile.mkdtemp()
        cls.p = config.paths(cls.tmp)
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _viewer(self, port=1):
        from android_lite import viewer
        sent = []
        inst = {"id": "vt", "name": "View test", "profile": "lite", "ram_mb": 1024, "cpus": 1,
                "disk": "instances/vt/android.img", "shared": "instances/vt/shared"}
        win = viewer.Viewer(self.root, self.p, inst, port, dispatch=sent.append)
        win._frame_w, win._frame_h = 640, 480
        win.canvas.config(width=640, height=480)
        self.root.update()
        return win, sent

    def _key(self, keysym, keycode=0):
        return types.SimpleNamespace(keysym=keysym, keycode=keycode)

    def test_typing_sends_key_down_and_up(self):
        win, sent = self._viewer()
        try:
            win._key_press(self._key("a", 38))
            win._key_release(self._key("a", 38))
            flat = [e for batch in sent for e in batch]
            self.assertEqual([(e["key"], e["down"]) for e in flat], [("a", True), ("a", False)])
            self.assertEqual(win._held, {})
        finally:
            win.close()

    def test_auto_repeat_is_not_duplicated(self):
        win, sent = self._viewer()
        try:
            win._key_press(self._key("a", 38))
            win._key_press(self._key("a", 38))          # repeat while held
            self.assertEqual(len([e for b in sent for e in b]), 1)
        finally:
            win.close()

    def test_uppercase_wraps_shift(self):
        win, sent = self._viewer()
        try:
            win._key_press(self._key("A", 38))
            win._key_release(self._key("A", 38))
            flat = [(e["key"], e["down"]) for b in sent for e in b]
            self.assertEqual(flat, [("shift", True), ("a", True), ("a", False), ("shift", False)])
        finally:
            win.close()

    def test_keymap_tap_is_used_for_mapped_key(self):
        win, sent = self._viewer()
        try:
            win.keymap = [{"key": "w", "x": 0.5, "y": 0.5}]
            win._key_press(self._key("w", 25))
            win._key_release(self._key("w", 25))
            ops = [e["op"] for b in sent for e in b]
            self.assertEqual(ops, ["move", "down", "up"])
        finally:
            win.close()

    def test_map_mode_stores_a_position(self):
        win, sent = self._viewer()
        try:
            win.toggle_map_mode()
            win._key_press(self._key("q", 24))
            ox, oy = win._offset()
            win._press(types.SimpleNamespace(x=ox + 320, y=oy + 240), "left")
            e = win.keymap[0]
            self.assertEqual(e["key"], "q")
            self.assertAlmostEqual(e["x"], 0.5, places=2)
            self.assertAlmostEqual(e["y"], 0.5, places=2)
            self.assertEqual(automation.load_keymap(win._keymap_path), win.keymap)
            self.assertEqual(sent, [])                 # the mapping click was not sent to the guest
        finally:
            win.close()

    def test_losing_focus_releases_held_keys(self):
        win, sent = self._viewer()
        try:
            win._key_press(self._key("a", 38))
            win._on_focus_out(None)
            flat = [(e["key"], e["down"]) for b in sent for e in b if e["op"] == "key"]
            self.assertEqual(flat, [("a", True), ("a", False)])
        finally:
            win.close()

    def test_volume_keys_send_press_and_release(self):
        win, sent = self._viewer()
        try:
            win.volume("volumeup")
            win.volume("bogus")                       # ignored
            self.assertEqual([(e["key"], e["down"]) for b in sent for e in b],
                             [("volumeup", True), ("volumeup", False)])
        finally:
            win.close()


@unittest.skipUnless(HAVE_DISPLAY and QEMU, "needs a display and QEMU")
class ViewerEndToEndTests(unittest.TestCase):
    """A real headless VM: frames arrive, and a key from the window reaches the guest.

    SeaBIOS shows a boot menu on F12, so the screen must change after that key.
    """

    def test_key_reaches_guest(self):
        import tkinter as tk
        from android_lite import viewer
        tmp = tempfile.mkdtemp()
        try:
            p = config.paths(tmp)
            inst = {"id": "e2e", "name": "E2E", "profile": "lite", "ram_mb": 512, "cpus": 1,
                    "disk": "instances/e2e/android.img", "shared": "instances/e2e/shared"}
            folder = os.path.dirname(instances.resolve(p, inst)["disk"])
            os.makedirs(folder)
            disk = instances.resolve(p, inst)["disk"]
            subprocess.run(["qemu-img", "create", "-f", "raw", disk, "64M"], check=True,
                           capture_output=True)
            port = core.free_port()
            args = [QEMU, *core.build_qemu_args(512, 1, "tcg", disk, display="none", qmp_port=port)]
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            root = tk.Tk()
            root.withdraw()
            win = None
            try:
                win = viewer.Viewer(root, p, inst, port)

                def pump(seconds):
                    end = time.time() + seconds
                    while time.time() < end:
                        root.update()
                        time.sleep(0.02)

                deadline = time.time() + 60
                while win._last_frame is None and time.time() < deadline:
                    pump(0.2)
                self.assertIsNotNone(win._last_frame, "no frame from the VM")
                pump(3)
                before = win._last_frame

                class E:
                    keysym, keycode = "F12", 0x58
                win._key_press(E)
                win._key_release(E)
                pump(3)
                self.assertNotEqual(before, win._last_frame, "F12 did not change the screen")
            finally:
                if win is not None:
                    win.close()
                try:
                    qmp.command(port, "quit")
                except (OSError, ValueError, qmp.QMPError):
                    pass
                proc.wait(timeout=20)
                root.destroy()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
