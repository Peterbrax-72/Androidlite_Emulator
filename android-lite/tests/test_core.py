"""Tests for Android Lite. Run from the project root:

    python -m unittest discover -s tests -v

Tests that need a real QEMU are skipped when none is installed. Set ALITE_QEMU to choose a binary.
"""
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from android_lite import config, core, disk, instances, qmp  # noqa: E402

QEMU = os.environ.get("ALITE_QEMU") or shutil.which("qemu-system-x86_64")
try:
    from PIL import Image
except ImportError:          # Pillow is only used to double-check PNG output
    Image = None


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = config.paths(self.tmp)
        self.s = config.load_settings(self.p)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ConfigTests(unittest.TestCase):
    def test_paths_honour_alite_home(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("ALITE_HOME")
            os.environ["ALITE_HOME"] = d
            try:
                self.assertEqual(config.paths()["base"], d)
            finally:
                if old is None:
                    os.environ.pop("ALITE_HOME", None)
                else:
                    os.environ["ALITE_HOME"] = old

    def test_normalize_clamps_and_fixes_profile(self):
        s = config.normalize({"profile": "bogus", "ram_mb": 10, "cpus": 99})
        self.assertEqual(s["profile"], "standard")
        self.assertEqual(s["ram_mb"], config.RAM_RANGE[0])
        self.assertEqual(s["cpus"], config.CPU_RANGE[1])

    def test_lite_uses_less_memory_than_standard(self):
        self.assertLess(config.PROFILES["lite"]["ram_mb"], config.PROFILES["standard"]["ram_mb"])

    def test_recommended_profile_by_ram(self):
        self.assertEqual(config.recommended_profile(8 * 1024 - 1), "lite")
        self.assertEqual(config.recommended_profile(16 * 1024), "standard")

    def test_ram_budget(self):
        ok, _ = config.ram_budget_ok(2048, [2048], 16 * 1024)
        self.assertTrue(ok)
        ok, msg = config.ram_budget_ok(4096, [4096, 4096], 8 * 1024)
        self.assertFalse(ok)
        self.assertIn("GB", msg)
        self.assertTrue(config.ram_budget_ok(4096, [], None)[0])

    def test_corrupt_settings_do_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            p = config.paths(d)
            with open(p["settings"], "w") as f:
                f.write("{not json")
            self.assertIn(config.load_settings(p)["profile"], config.PROFILES)

    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = config.paths(d)
            s = config.apply_profile(dict(config.DEFAULTS), "lite")
            config.save_settings(p, s)
            self.assertEqual(config.load_settings(p)["ram_mb"], config.PROFILES["lite"]["ram_mb"])

    def test_host_ram_is_int_or_none(self):
        v = config.host_ram_mb()
        self.assertTrue(v is None or (isinstance(v, int) and v > 0))


class DiskTests(unittest.TestCase):
    def test_create_disk_size(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.img")
            disk.create_disk(path, 1)
            self.assertEqual(os.path.getsize(path), 1024 ** 3)

    def test_sparse_copy_keeps_data_and_size(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.img")
            with open(src, "wb") as f:
                f.truncate(64 * 1024 * 1024)
                f.seek(10 * 1024 * 1024)
                f.write(b"ANDROID-DATA")
                f.seek(50 * 1024 * 1024)
                f.write(b"END")
            dst = os.path.join(d, "dst", "copy.img")
            seen = []
            disk.sparse_copy(src, dst, lambda done, total: seen.append((done, total)))
            self.assertEqual(os.path.getsize(dst), os.path.getsize(src))
            with open(dst, "rb") as f:
                f.seek(10 * 1024 * 1024)
                self.assertEqual(f.read(12), b"ANDROID-DATA")
                f.seek(50 * 1024 * 1024)
                self.assertEqual(f.read(3), b"END")
            self.assertTrue(seen and seen[-1][0] == seen[-1][1])
            if sys.platform.startswith("linux"):
                # zero blocks were skipped: far fewer allocated blocks than the logical size
                self.assertLess(os.stat(dst).st_blocks * 512, 16 * 1024 * 1024)


class PngTests(unittest.TestCase):
    def _write_ppm(self, path, w, h, pixels):
        with open(path, "wb") as f:
            f.write(b"P6\n# test\n%d %d\n255\n" % (w, h))
            f.write(pixels)

    def test_ppm_to_png_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            w, h = 7, 5
            rnd = random.Random(1)
            pixels = bytes(rnd.randrange(256) for _ in range(w * h * 3))
            ppm = os.path.join(d, "a.ppm")
            png = os.path.join(d, "a.png")
            self._write_ppm(ppm, w, h, pixels)
            qmp.ppm_to_png(ppm, png)
            with open(png, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
            if Image is not None:
                im = Image.open(png).convert("RGB")
                self.assertEqual(im.size, (w, h))
                self.assertEqual(im.tobytes(), pixels)

    def test_truncated_ppm_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ppm = os.path.join(d, "bad.ppm")
            self._write_ppm(ppm, 4, 4, b"\x00" * 10)
            with self.assertRaises(ValueError):
                qmp.ppm_to_png(ppm, os.path.join(d, "x.png"))


class InstanceTests(TempHome):
    def test_main_instance_is_created_on_first_load(self):
        items = instances.load(self.p, self.s)
        self.assertEqual(items[0]["id"], instances.MAIN_ID)
        self.assertEqual(instances.resolve(self.p, items[0])["disk"], self.p["disk"])

    def test_create_clone_update_remove(self):
        a = instances.create(self.p, self.s, "Game 1", "lite")
        self.assertTrue(os.path.isfile(instances.resolve(self.p, a)["disk"]))
        self.assertEqual(a["ram_mb"], config.PROFILES["lite"]["ram_mb"])
        with self.assertRaises(ValueError):
            instances.create(self.p, self.s, "game 1")            # duplicate, case-insensitive
        with self.assertRaises(ValueError):
            instances.create(self.p, self.s, "   ")
        # clone needs an installed disk; give 'Game 1' some content first
        with open(instances.resolve(self.p, a)["disk"], "r+b") as f:
            f.write(b"APPDATA")
        b = instances.clone(self.p, self.s, "game 1", "Game 2")
        self.assertEqual(b["ram_mb"], a["ram_mb"])
        with open(instances.resolve(self.p, b)["disk"], "rb") as f:
            self.assertEqual(f.read(7), b"APPDATA")
        upd = instances.update(self.p, self.s, b["id"], name="Farm", ram_mb=99999, cpus=0)
        self.assertEqual(upd["name"], "Farm")
        self.assertEqual(upd["ram_mb"], config.RAM_RANGE[1])
        self.assertEqual(upd["cpus"], config.CPU_RANGE[0])
        folder = instances.resolve(self.p, b)["folder"]
        instances.remove(self.p, self.s, b["id"])
        self.assertFalse(os.path.exists(folder))
        ids = [i["id"] for i in instances.load(self.p, self.s)]
        self.assertNotIn(b["id"], ids)
        self.assertIn(a["id"], ids)

    def test_clone_without_disk_is_refused(self):
        with self.assertRaises(ValueError):
            instances.clone(self.p, self.s, "main", "Copy")

    def test_port_is_remembered_across_loads(self):
        instances.create(self.p, self.s, "Game 1")
        instances.set_port(self.p, self.s, "Game 1", 12345)
        self.assertEqual(instances.get(instances.load(self.p, self.s), "game 1")["qmp_port"], 12345)

    def test_corrupt_registry_falls_back_to_main(self):
        os.makedirs(self.p["base"], exist_ok=True)
        with open(os.path.join(self.p["base"], "instances.json"), "w") as f:
            f.write("[oops")
        self.assertEqual(instances.load(self.p, self.s)[0]["id"], instances.MAIN_ID)


class CoreTests(TempHome):
    def test_disk_path_with_comma_is_escaped(self):
        args = core.build_qemu_args(1024, 1, "tcg", "/x,y/android.img")
        self.assertIn("file=/x,,y/android.img,format=raw,if=ide,index=0", args)

    def test_args_tcg_boot_from_disk(self):
        args = core.build_qemu_args(1536, 2, "tcg", "/d/android.img")
        self.assertEqual(args[args.index("-m") + 1], "1536M")
        self.assertEqual(args[args.index("-cpu") + 1], "max")
        self.assertEqual(args[args.index("-boot") + 1], "order=c")
        self.assertNotIn("-cdrom", args)
        self.assertNotIn("usb-storage,drive=share", args)

    def test_args_installer_and_shared_folder(self):
        args = core.build_qemu_args(2048, 2, "whpx", "/d/a.img", iso="/d/a.iso", shared_dir="/d/shared")
        self.assertEqual(args[args.index("-cdrom") + 1], "/d/a.iso")
        self.assertEqual(args[args.index("-boot") + 1], "order=d")
        self.assertEqual(args[args.index("-cpu") + 1], "host")
        self.assertIn("file=fat:rw:/d/shared,format=raw,if=none,id=share", args)
        self.assertIn("usb-storage,drive=share", args)

    def test_find_qemu_respects_override(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            fake = f.name
        old = os.environ.get("ALITE_QEMU")
        os.environ["ALITE_QEMU"] = fake
        try:
            self.assertEqual(core.find_qemu(self.p), fake)
        finally:
            os.environ.pop("ALITE_QEMU", None)
            if old is not None:
                os.environ["ALITE_QEMU"] = old
            os.unlink(fake)

    def test_install_apk_validation(self):
        shared = os.path.join(self.tmp, "shared")
        txt = os.path.join(self.tmp, "notes.txt")
        with open(txt, "w") as f:
            f.write("hello")
        with self.assertRaises(core.SetupError):
            core.install_apk(shared, txt)
        fake = os.path.join(self.tmp, "fake.apk")
        with open(fake, "w") as f:
            f.write("not a zip")
        with self.assertRaises(core.SetupError):
            core.install_apk(shared, fake)
        good = os.path.join(self.tmp, "my game!.apk")
        with zipfile.ZipFile(good, "w") as z:
            z.writestr("AndroidManifest.xml", "x")
        dest = core.install_apk(shared, good)
        self.assertTrue(os.path.isfile(dest))
        self.assertTrue(os.path.basename(dest).endswith(".apk"))
        self.assertNotIn(" ", os.path.basename(dest))
        self.assertTrue(os.path.isfile(os.path.join(shared, "README.txt")))

    def test_start_without_qemu_or_disk_fails_cleanly(self):
        inst = instances.get(instances.load(self.p, self.s), "main")
        old = os.environ.pop("ALITE_QEMU", None)
        try:
            with unittest.mock.patch.object(core.config, "QEMU_EXE", "no-such-qemu-binary"), \
                    unittest.mock.patch.object(core.shutil, "which", return_value=None):
                with self.assertRaises(core.SetupError):
                    core.start_instance(self.p, self.s, inst)
        finally:
            if old is not None:
                os.environ["ALITE_QEMU"] = old

    @unittest.skipUnless(QEMU, "no qemu-system-x86_64 available")
    def test_detect_accel_returns_known_value(self):
        accel, why = core.detect_accel(QEMU)
        self.assertIn(accel, ("whpx", "kvm", "tcg"))
        self.assertTrue(why)


@unittest.skipUnless(QEMU, "no qemu-system-x86_64 available")
class QemuTests(TempHome):
    def test_full_vm_lifecycle(self):
        """Start a VM with our exact device set and a shared folder, then pause, resume,
        take a PNG screenshot, and stop it."""
        inst = instances.create(self.p, self.s, "Lifecycle", "lite")
        r = instances.resolve(self.p, inst)
        core.ensure_shared(r["shared"])
        port = core.free_port()
        log_path = os.path.join(self.tmp, "qemu.log")
        with open(log_path, "w") as logf:
            args = [QEMU, *core.build_qemu_args(inst["ram_mb"], inst["cpus"], "tcg", r["disk"],
                                                display="none", qmp_port=port, paused=True,
                                                shared_dir=r["shared"])]
            proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            status = None
            while status is None and time.monotonic() < deadline:
                if proc.poll() is not None:
                    self.fail("QEMU exited: " + open(log_path, errors="replace").read()[-1500:])
                status = core.qmp.query_status(port, 1.0)
                time.sleep(0.3)
            self.assertIn(status, ("prelaunch", "paused"))

            self.assertEqual(core.pause_vm(port, False), "running")
            self.assertEqual(core.pause_vm(port, True), "paused")
            self.assertEqual(core.pause_vm(port, False), "running")
            time.sleep(4)                                   # let the BIOS draw something
            out = os.path.join(self.tmp, "shot.png")
            core.screenshot(port, out)
            with open(out, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
            if Image is not None:
                with Image.open(out) as im:
                    self.assertGreater(im.size[0], 0)

            # Android is not installed here, so it will not power off: this exercises the forced path
            self.assertEqual(core.stop_vm(port, proc, grace=3), "forced quit")
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()


@unittest.skipUnless(os.environ.get("DISPLAY") or sys.platform == "win32", "needs a display")
class GuiTests(TempHome):
    def test_gui_lists_instances_and_pumps(self):
        from android_lite.gui import App
        old = os.environ.get("ALITE_QEMU")
        if QEMU:
            os.environ["ALITE_QEMU"] = QEMU
        try:
            instances.create(self.p, self.s, "Game 1", "lite")
            instances.create(self.p, self.s, "Game 2")
            app = App(self.p)
            app.update()
            self.assertEqual(len(app.tree.get_children()), 3)     # main + 2
            app.tree.selection_set("main")
            app._refresh_buttons()
            self.assertEqual(str(app.btn["start"].cget("state")), "disabled")   # no disk yet
            app.q.put(("log", "hello from a worker"))
            app._pump()
            app.update()
            app.destroy()
        finally:
            if old is None:
                os.environ.pop("ALITE_QEMU", None)
            else:
                os.environ["ALITE_QEMU"] = old


import unittest.mock  # noqa: E402  (used by a test above)

if __name__ == "__main__":
    unittest.main()
