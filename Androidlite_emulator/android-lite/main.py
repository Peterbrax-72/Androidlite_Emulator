#!/usr/bin/env python3
"""Android Lite entry point.

  python main.py                          open the GUI (multi-instance manager)
  python main.py status                   engine, acceleration, RAM, instance states
  python main.py setup                    install QEMU and download the Android image (once)
  python main.py new "Game 1" --profile lite
  python main.py install                  boot the installer in the main instance
  python main.py start                    boot an installed instance
  python main.py clone "Game 1" "Game 2"  copy an instance (with its apps)
  python main.py pause | resume | stop | screenshot [ID]
  python main.py apk app.apk --to ID      copy an APK into an instance's shared folder
  python main.py --help                   all commands
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from android_lite.gui import App
        App().mainloop()
        return 0
    from android_lite import cli
    args = cli.build_parser().parse_args(argv)
    return cli.run(args)


if __name__ == "__main__":
    sys.exit(main())
