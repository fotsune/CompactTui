"""Проверка на настоящем compact.exe: сжатие каталога, код возврата, размер на диске в логе.

Запуск:  python -m unittest tests.test_integration -v
Нужны NTFS и права администратора; на других ФС тест пропускается.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import compacttui


class FakeWin:
    def __init__(self):
        self.lines = {}

    def getmaxyx(self):
        return 30, 100

    def addstr(self, y, x, text, attr=0):
        self.lines[(y, x)] = text

    def move(self, y, x):
        pass

    def clrtoeol(self):
        pass

    def clear(self):
        self.lines = {}

    def refresh(self):
        pass

    def nodelay(self, flag):
        pass

    def getch(self):
        return -1


def compact_hidden(path, force):
    """Тот же compact, но без отдельного окна — чтобы тест не открывал консоли."""
    return subprocess.Popen(compacttui.compact_command(path, force),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TestRealCompact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.folder = os.path.join(self.tmp, "data")
        os.mkdir(self.folder)
        with open(os.path.join(self.folder, "text.log"), "wb") as f:
            f.write(b"the quick brown fox jumps over the lazy dog\n" * 200000)

    def test_compaction_shrinks_size_on_disk_and_is_logged(self):
        before = compacttui.get_folder_physical_size(self.folder)
        if before == 0:
            self.skipTest("не удалось прочитать размер на диске")
        original = compacttui.start_compact
        compacttui.start_compact = compact_hidden
        try:
            open(compacttui.LOG_PATH, "w").close()
            results = compacttui.compact_folders(FakeWin(), self.tmp, ["data"], compacttui.parse_args(["-hdd"]))
        finally:
            compacttui.start_compact = original
        path, code, saved = results[0]
        self.assertEqual(path, self.folder)
        self.assertEqual(code, 0, "compact вернул ошибку")
        after = compacttui.get_folder_physical_size(self.folder)
        self.assertLess(after, before, "сжатие не уменьшило размер на диске")
        with open(compacttui.LOG_PATH, encoding="utf-8") as f:
            log = f.read()
        self.assertIn(self.folder, log)
        self.assertIn("код 0", log)


if __name__ == "__main__":
    unittest.main()
