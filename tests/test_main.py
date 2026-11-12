"""Тесты логики compacttui.py, не требующие настоящего терминала.

Запуск:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import curses

import compacttui


class FakeWin:
    """Замена curses-окна: запоминает текст, отдаёт заранее заданные клавиши."""

    def __init__(self, keys=(), height=30, width=100):
        self.keys = list(keys)
        self.height, self.width = height, width
        self.lines = {}
        self.history = []
        self.attrs = {}
        self.current_attr = 0
        self.nodelay_set = None

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y, x, text, attr=None):
        if y >= self.height or x >= self.width:
            raise curses.error("out of bounds")
        if y < 0 or x < 0:
            raise curses.error("negative coordinates")
        self.lines[(y, x)] = text
        self.history.append(text)
        # как в curses: явный attr заменяет состояние attron(), отсутствие - сохраняет
        self.attrs[(y, x)] = self.current_attr if attr is None else attr

    def move(self, y, x):
        pass

    def clrtoeol(self):
        pass

    def clear(self):
        self.lines = {}

    def refresh(self):
        pass

    def attron(self, attr):
        self.current_attr |= attr

    def attroff(self, attr):
        self.current_attr &= ~attr

    def nodelay(self, flag):
        self.nodelay_set = flag

    def getch(self):
        if not self.keys:
            raise AssertionError("окно осталось без клавиш: экран ждёт ввода вечно")
        return self.keys.pop(0)

    def text(self):
        return " | ".join(str(v) for v in self.lines.values())


class TestParseArgs(unittest.TestCase):
    def test_defaults(self):
        opts = compacttui.parse_args([])
        self.assertIsNone(opts["path"])
        self.assertEqual((opts["select_all"], opts["force"], opts["hdd"], opts["logo"]),
                         (False, False, False, True))

    def test_all_flags(self):
        opts = compacttui.parse_args(["-p", "D:/data", "-a", "-z", "-f", "-hdd", "-l"])
        self.assertEqual(opts["path"], "D:/data")
        self.assertTrue(opts["select_all"] and opts["full_paths"])
        self.assertTrue(opts["force"] and opts["hdd"])
        self.assertFalse(opts["logo"])

    def test_path_with_spaces_kept_whole(self):
        self.assertEqual(compacttui.parse_args(["-p", "D:/My Data/x y"])["path"], "D:/My Data/x y")

    def test_help_spellings(self):
        for flag in ("-h", "-help", "--help"):
            self.assertTrue(compacttui.parse_args([flag])["help"], flag)

    def test_path_without_value_means_ask(self):
        self.assertEqual(compacttui.parse_args(["-p"])["path"], "")

    def test_unknown_flags_ignored(self):
        self.assertIsNone(compacttui.parse_args(["-zzz"])["path"])
    def test_every_flag_is_described_in_help(self):
        """Справка — docstring модуля: флаг, забытый в ней, считается багом."""
        help_text = compacttui.__doc__ or ""
        for flag in ("-p", "-a", "-z", "-f", "-hdd", "-l", "-n", "-h"):
            self.assertIn(flag, help_text, "флаг %s не описан в справке" % flag)



class TestFolderHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name in ("b", "a", "c"):
            os.mkdir(os.path.join(self.tmp, name))
        with open(os.path.join(self.tmp, "file.txt"), "w") as f:
            f.write("x")

    def test_get_folders_sorted_and_files_excluded(self):
        self.assertEqual(compacttui.get_folders(self.tmp), ["a", "b", "c"])

    def test_get_folders_on_missing_dir(self):
        self.assertEqual(compacttui.get_folders(os.path.join(self.tmp, "nope")), [])

    def test_folder_size_reads_files(self):
        with open(os.path.join(self.tmp, "a", "f.bin"), "wb") as f:
            f.write(b"0" * 4000000)
        self.assertGreater(compacttui.get_folder_size(os.path.join(self.tmp, "a")), 0)

    def test_physical_size_not_above_logical(self):
        with open(os.path.join(self.tmp, "a", "f.bin"), "wb") as f:
            f.write(b"0" * 4000000)
        logical = compacttui.get_folder_size(os.path.join(self.tmp, "a"))
        physical = compacttui.get_folder_physical_size(os.path.join(self.tmp, "a"))
        self.assertGreater(physical, 0)
        self.assertLessEqual(physical, logical * 1.05)

    def test_write_file_appends_utf8(self):
        compacttui.write_file("первая строка")
        compacttui.write_file("second")
        with open(compacttui.LOG_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[-2:], ["первая строка", "second"])


class TestCompactCommand(unittest.TestCase):
    def test_command_without_force(self):
        self.assertEqual(compacttui.compact_command("C:/a b", False),
                         ["compact", "/C", "/S:C:/a b", "/A", "/I", "/EXE:LZX"])

    def test_command_with_force_ends_with_f(self):
        self.assertEqual(compacttui.compact_command("C:/a", True)[-1], "/F")

    def test_path_with_spaces_is_one_argument(self):
        self.assertIn("/S:C:/My Data/x y", compacttui.compact_command("C:/My Data/x y", False))


class TestPut(unittest.TestCase):
    def test_no_crash_at_screen_corner(self):
        win = FakeWin(height=10, width=20)
        compacttui.put(win, 9, 19, "overflowing text")
        compacttui.put(win, 9, 0, "x" * 100)
        compacttui.put(win, 42, 0, "off screen")

    def test_text_truncated_to_width(self):
        win = FakeWin(height=10, width=20)
        compacttui.put(win, 0, 0, "y" * 100)
        self.assertLessEqual(len(win.lines[(0, 0)]), 20)


class TestMenu(unittest.TestCase):
    def setUp(self):
        self.folders = ["a", "b", "c", "d", "e"]
        self.opts = compacttui.parse_args([])

    def test_enter_returns_selection(self):
        state = [False] * 5
        win = FakeWin([ord(' '), curses.KEY_RIGHT, ord(' '), 10])
        chosen = compacttui.menu(win, self.folders, state, self.opts, "D:/x")
        self.assertEqual(chosen, ["a", "b"])
        self.assertEqual(state, [True, True, False, False, False])

    def test_select_all_updates_caller_list(self):
        state = [False] * 5
        compacttui.menu(FakeWin([ord('a'), 10]), self.folders, state, self.opts, "D:/x")
        self.assertEqual(state, [True] * 5)

    def test_select_all_twice_deselects(self):
        state = [True] * 5
        chosen = compacttui.menu(FakeWin([ord('a'), ord('r')]), self.folders, state, self.opts, "D:/x")
        self.assertEqual(state, [False] * 5)
        self.assertIsNone(chosen)

    def test_enter_with_nothing_selected_warns_and_stays(self):
        state = [False] * 5
        win = FakeWin([10, ord(' '), ord('r')])
        compacttui.menu(win, self.folders, state, self.opts, "D:/x")
        self.assertIn("Ничего не выбрано", " ".join(win.history))
        self.assertEqual(state, [False] * 5)

    def test_navigation_and_toggle(self):
        state = [False] * 5
        win = FakeWin([curses.KEY_RIGHT, curses.KEY_DOWN, ord(' '), 13])
        self.assertEqual(compacttui.menu(win, self.folders, state, self.opts, "D:/x"), ["e"])

    def test_down_from_short_row_does_not_crash(self):
        state = [False] * 5
        win = FakeWin([curses.KEY_RIGHT, curses.KEY_RIGHT, curses.KEY_DOWN, ord(' '), 10])
        self.assertEqual(compacttui.menu(win, self.folders, state, self.opts, "D:/x"), ["c"])

    def test_f_and_d_toggle_opts_in_place(self):
        win = FakeWin([ord('f'), ord('d'), 27])
        with self.assertRaises(SystemExit):
            compacttui.menu(win, self.folders, [True] * 5, self.opts, "D:/x")
        self.assertTrue(self.opts["force"] and self.opts["hdd"])

    def test_current_item_is_highlighted(self):
        win = FakeWin([ord('r')])
        compacttui.menu(win, self.folders, [False] * 5, self.opts, "D:/x")
        highlighted = [win.lines[pos] for pos, attr in win.attrs.items()
                       if attr and win.lines[pos].startswith("[")]
        self.assertTrue(highlighted, "ни один пункт не подсвечен")
        self.assertEqual(len(highlighted), 1, "подсвечено больше одного пункта")
        self.assertIn("a", highlighted[0])

    def test_back_key_returns_none(self):
        self.assertIsNone(compacttui.menu(FakeWin([ord('r')]), self.folders, [False] * 5,
                                    self.opts, "D:/x"))

    def test_tiny_screen_does_not_crash(self):
        win = FakeWin([ord(' '), 10], height=6, width=30)
        compacttui.menu(win, self.folders[:2], [False] * 2, self.opts, "C:/a/very/long/path/title")

    def test_full_paths_option(self):
        opts = compacttui.parse_args(["-z"])
        win = FakeWin([ord(' '), 10])
        compacttui.menu(win, ["one"], [False], opts, "C:/base")
        self.assertIn("C:/base", win.text())


class TestCompactFolders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name in ("one", "two"):
            os.mkdir(os.path.join(self.tmp, name))
            with open(os.path.join(self.tmp, name, "data.bin"), "wb") as f:
                f.write(b"\0" * 200000)

    def test_both_modes_log_every_folder(self):
        calls = []

        class FakeProc:
            returncode = 0

            def poll(self):
                return 0

        def fake_start(path, force):
            calls.append((path, force))
            return FakeProc()

        original = compacttui.start_compact
        compacttui.start_compact = fake_start
        try:
            for mode in (True, False):
                opts = compacttui.parse_args(["-f"])
                opts["hdd"] = mode
                open(compacttui.LOG_PATH, "w").close()
                results = compacttui.compact_folders(FakeWin([-1] * 4), self.tmp,
                                               ["one", "two"], opts)
                self.assertEqual(sorted(r[0] for r in results),
                                 sorted(os.path.join(self.tmp, n) for n in ("one", "two")))
                with open(compacttui.LOG_PATH, encoding="utf-8") as f:
                    self.assertEqual(len(f.read().splitlines()), 2)
        finally:
            compacttui.start_compact = original
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(force for _path, force in calls))

    def test_launch_failure_logged_not_raised(self):
        def boom(path, force):
            raise OSError("нет прав")

        original = compacttui.start_compact
        compacttui.start_compact = boom
        try:
            open(compacttui.LOG_PATH, "w").close()
            results = compacttui.compact_folders(FakeWin([-1] * 4), self.tmp, ["one"],
                                           compacttui.parse_args(["-hdd"]))
        finally:
            compacttui.start_compact = original
        self.assertEqual(results[0][1], -1)
        with open(compacttui.LOG_PATH, encoding="utf-8") as f:
            self.assertIn("ОШИБКА", f.read())


class FakeEdit:
    """Окно для read_line/ask_path: отдаёт клавиши по одной, помнит последнюю строку."""

    def __init__(self, keys, height=30, width=100):
        self.keys = list(keys)
        self.height, self.width = height, width
        self.drawn = []
        self.cursor = (0, 0)

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y, x, text, attr=None):
        if y >= self.height or x >= self.width:
            raise curses.error("out of bounds")
        self.drawn.append((y, x, text))
        self.cursor = (y, x + len(text))

    def move(self, y, x):
        self.cursor = (y, x)

    def clrtoeol(self):
        pass

    def clear(self):
        pass

    def refresh(self):
        pass

    def get_wch(self):
        if not self.keys:
            raise AssertionError("редактору не хватило клавиш")
        return self.keys.pop(0)


def line_of(win, row):
    """Что нарисовано в строке row (склейка кусков по порядку)."""
    return "".join(text for y, _, text in win.drawn if y == row)


class TestNormalizePath(unittest.TestCase):
    def test_quotes_are_dropped(self):
        self.assertEqual(compacttui.normalize_path('"C:\\data"'), "C:\\data")
        self.assertEqual(compacttui.normalize_path("'C:\\data'"), "C:\\data")
        self.assertEqual(compacttui.normalize_path("«C:\\data»"), "C:\\data")

    def test_spaces_and_padding_dropped(self):
        self.assertEqual(compacttui.normalize_path("   C:\\data   "), "C:\\data")
        self.assertEqual(compacttui.normalize_path('  "C:\\Program Files"  '),
                         "C:\\Program Files")

    def test_quotes_inside_path_kept(self):
        self.assertEqual(compacttui.normalize_path("C:\\a'b\\c"), "C:\\a'b\\c")

    def test_variables_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["COMPACT_TEST_DIR"] = tmp
            try:
                self.assertEqual(compacttui.normalize_path("%COMPACT_TEST_DIR%"), tmp)
            finally:
                del os.environ["COMPACT_TEST_DIR"]

    def test_existing_dir_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(compacttui.normalize_path(tmp + os.sep + "."), tmp)

    def test_empty_and_none(self):
        self.assertEqual(compacttui.normalize_path(""), "")
        self.assertEqual(compacttui.normalize_path(None), "")


class TestResolvePath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "one"))
        os.makedirs(os.path.join(self.root, "two"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_good_path_returns_folders_without_complaint(self):
        path, folders, problem = compacttui.resolve_path('"%s"' % self.root)
        self.assertEqual(path, self.root)
        self.assertEqual(folders, ["one", "two"])
        self.assertEqual(problem, "")

    def test_file_is_reported(self):
        f = os.path.join(self.root, "file.txt")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("x")
        path, folders, problem = compacttui.resolve_path(f)
        self.assertEqual(folders, [])
        self.assertIn("файл", problem)

    def test_missing_dir_is_reported(self):
        path, folders, problem = compacttui.resolve_path("Z:\\нет такого")
        self.assertEqual(folders, [])
        self.assertIn("не найдена", problem)

    def test_dir_without_subfolders_is_reported(self):
        empty = os.path.join(self.root, "one")
        path, folders, problem = compacttui.resolve_path(empty)
        self.assertEqual(folders, [])
        self.assertIn("нет подпапок", problem)

    def test_empty_input_is_reported(self):
        path, folders, problem = compacttui.resolve_path("   ")
        self.assertEqual(path, "")
        self.assertIn("Путь пустой", problem)


class TestReadLine(unittest.TestCase):
    def test_typing_and_enter(self):
        win = FakeEdit(list("C:\\data") + [curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> "), "C:\\data")

    def test_left_arrow_and_insert_in_middle(self):
        win = FakeEdit([curses.KEY_LEFT, curses.KEY_LEFT, "c", curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "abd"), "acbd")

    def test_home_and_end(self):
        win = FakeEdit([curses.KEY_HOME, "x", curses.KEY_END, "y", curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "ab"), "xaby")

    def test_delete_removes_char_under_cursor(self):
        win = FakeEdit([curses.KEY_HOME, curses.KEY_RIGHT, curses.KEY_DC,
                        curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "abc"), "ac")

    def test_backspace_removes_char_before_cursor(self):
        win = FakeEdit([curses.KEY_BACKSPACE, curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "abc"), "ab")

    def test_ctrl_u_and_ctrl_k(self):
        win = FakeEdit([curses.KEY_LEFT, 21, curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "abc"), "c")
        win = FakeEdit([curses.KEY_HOME, curses.KEY_RIGHT, 11, curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "abc"), "a")

    def test_esc_returns_none(self):
        win = FakeEdit([curses.KEY_HOME, "\x1b"])
        self.assertIsNone(compacttui.read_line(win, 2, 0, "> ", "abc"))

    def test_left_at_start_and_right_at_end_do_not_crash(self):
        win = FakeEdit([curses.KEY_HOME, curses.KEY_LEFT, curses.KEY_LEFT,
                        curses.KEY_END, curses.KEY_RIGHT, "!", curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "ab"), "ab!")

    def test_long_line_scrolls_and_stays_on_screen(self):
        win = FakeEdit(list("0123456789" * 4) + [curses.KEY_ENTER], width=20)
        result = compacttui.read_line(win, 2, 0, "> ")
        self.assertEqual(result, "0123456789" * 4)
        self.assertLessEqual(win.cursor[1], win.width - 1)

    def test_typed_text_and_cursor_are_drawn(self):
        win = FakeEdit(list("abc") + [curses.KEY_ENTER])
        compacttui.read_line(win, 2, 0, "> ")
        self.assertIn("> abc", line_of(win, 2))


class TestAskPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "one"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_quoted_path(self):
        win = FakeEdit(list('"%s"' % self.root) + [curses.KEY_ENTER])
        path, folders = compacttui.ask_path(win)
        self.assertEqual(path, self.root)
        self.assertEqual(folders, ["one"])

    def test_bad_path_kept_for_editing(self):
        good = self.root
        win = FakeEdit(list(good + "x") + [curses.KEY_ENTER,
                       curses.KEY_BACKSPACE, curses.KEY_ENTER])
        path, folders = compacttui.ask_path(win)
        self.assertEqual(path, good)
        self.assertEqual(folders, ["one"])

    def test_initial_value_is_prefilled_and_editable(self):
        prefilled = '"%s"' % self.root     # как пришло из -p или перетаскиванием
        win = FakeEdit([curses.KEY_ENTER])
        path, folders = compacttui.ask_path(win, initial=prefilled)
        self.assertEqual(path, self.root)
        self.assertEqual(folders, ["one"])

    def test_esc_quits(self):
        win = FakeEdit([curses.KEY_HOME, "\x1b"])
        with self.assertRaises(SystemExit):
            compacttui.ask_path(win)

class TestCli(unittest.TestCase):
    def test_help_returns_zero(self):
        self.assertEqual(compacttui.cli(["-h"]), 0)


if __name__ == "__main__":
    unittest.main()

class TestReadLineControlKeysAsStrings(unittest.TestCase):
    """В Windows get_wch() отдаёт управляющие клавиши строкой, а не числом."""

    def test_backspace_as_string(self):
        for ch in ("\x08", "\x7f"):
            win = FakeEdit([ch, curses.KEY_ENTER])
            self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "abc"), "ab")

    def test_delete_as_string(self):
        win = FakeEdit([curses.KEY_HOME, "\x04", curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "abc"), "bc")

    def test_home_end_ctrl_u_ctrl_k_as_strings(self):
        win = FakeEdit(["\x01", "x", curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "ab"), "xab")
        win = FakeEdit([curses.KEY_HOME, "\x05", "y", curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "ab"), "aby")
        win = FakeEdit(["\x15", curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "abc"), "")
        win = FakeEdit(["\x01", "\x0b", curses.KEY_ENTER])
        self.assertEqual(compacttui.read_line(win, 2, 0, "> ", "abc"), "")

    def test_enter_as_string(self):
        for ch in ("\r", "\n"):
            win = FakeEdit(list("ok") + [ch])
            self.assertEqual(compacttui.read_line(win, 2, 0, "> "), "ok")

    def test_esc_as_string_returns_none(self):
        win = FakeEdit(["\x1b"])
        self.assertIsNone(compacttui.read_line(win, 2, 0, "> ", "abc"))

    def test_typo_fixed_by_backspace_after_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'a')
            os.mkdir(path)
            os.mkdir(os.path.join(path, 'sub'))
            typed = path + 'x'
            win = FakeEdit(list(typed) + ["\r", "\x08", "\r"])
            got, folders = compacttui.ask_path(win)
            self.assertEqual(got, os.path.normpath(path))
            self.assertEqual(folders, ['sub'])


class TestSizesAndHistory(unittest.TestCase):
    """Подписи [файлов|на диске], флаг -n и история сжатых папок из %TEMP%."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.folder = os.path.join(self.tmp, "big")
        os.mkdir(self.folder)
        os.mkdir(os.path.join(self.folder, "nested"))
        for name in ("a.bin", "b.bin", os.path.join("nested", "c.txt")):
            with open(os.path.join(self.folder, name), "wb") as file:
                file.write(b"0" * 5000)
        self.opts = compacttui.parse_args([])
        self.saved_history = (compacttui.HISTORY_PATH, compacttui.DISMISSED_PATH)
        compacttui.HISTORY_PATH = os.path.join(self.tmp, "history", "compressed.tsv")
        compacttui.DISMISSED_PATH = os.path.join(self.tmp, "history", "cleared.tsv")

    def tearDown(self):
        compacttui.HISTORY_PATH, compacttui.DISMISSED_PATH = self.saved_history

    def test_scan_counts_files_and_sizes(self):
        files, physical, logical = compacttui.scan_infos(FakeWin(), self.tmp, ["big"],
                                                   self.opts)[0]
        self.assertEqual(files, 3)
        self.assertGreaterEqual(logical, 15000)
        self.assertGreater(physical, 0)

    def test_flag_n_skips_scanning(self):
        infos = compacttui.scan_infos(FakeWin(), self.tmp, ["big", "big"],
                                compacttui.parse_args(["-n"]))
        self.assertEqual(infos, [None, None])

    def test_human_units(self):
        self.assertEqual((compacttui.human_count(999), compacttui.human_count(1024),
                          compacttui.human_bytes(512), compacttui.human_bytes(1024 ** 2)),
                         ("999", "1.0k", "512", "1.0m"))

    def test_info_text_matches_format(self):
        gygabyte = 1024 ** 3
        self.assertEqual(compacttui.format_info((1200, 13 * gygabyte, 0)),
                         "[1.2k|13.0g]")
        self.assertEqual(compacttui.format_info(None), "")

    def test_label_fits_column_and_keeps_size(self):
        label = compacttui.folder_label("o" * 40, (10, 2048, 2048), 20)
        self.assertLessEqual(len(label), 20)
        self.assertTrue(label.endswith("[10|2.0k]"))

    def test_history_records_only_success(self):
        compacttui.record_history(self.folder, 1, 2.0, 2.0)
        self.assertEqual(compacttui.load_history(), {})
        compacttui.record_history(self.folder, 0, 2.0, 0.5)
        self.assertIn(compacttui.history_key(self.folder), compacttui.load_history())

    def test_compressed_already_by_history(self):
        compacttui.record_history(self.folder, 0, 2.0, 0.5)
        self.assertTrue(compacttui.compressed_already(self.folder, None, compacttui.load_history()))

    def test_compressed_already_by_disk_size(self):
        self.assertTrue(compacttui.compressed_already(self.folder, (3, 100, 5000), {}))
        self.assertFalse(compacttui.compressed_already(self.folder, (3, 5000, 5000), {}))

    def test_menu_shows_sizes(self):
        win = FakeWin([ord('r')])
        compacttui.menu(win, ["big"], [False], self.opts, self.tmp, [(1200, 1024 ** 3, 1024 ** 3)])
        self.assertIn("[1.2k|1.0g]", win.text())

    def test_menu_without_sizes_has_no_info(self):
        win = FakeWin([ord('r')])
        compacttui.menu(win, ["big"], [False], compacttui.parse_args(["-n"]), self.tmp, [None])
        labels = [text for text in win.lines.values() if text.startswith("[ ] big")]
        self.assertEqual(labels, ["[ ] big"])

    def test_menu_marks_compressed_folder(self):
        compacttui.record_history(self.folder, 0, 2.0, 0.5)
        win = FakeWin([ord('r')])
        compacttui.menu(win, ["big"], [False], self.opts, self.tmp, [None],
                  compacttui.load_history())
        positions = [pos for pos, text in win.lines.items() if text.startswith("[ ] big")]
        self.assertEqual(len(positions), 1, "пункт меню не найден")
        if compacttui.attr(2):
            self.assertTrue(win.attrs[positions[0]] & ~curses.A_REVERSE,
                            "сжатая папка не подсвечена цветом")

    def test_confirm_screen_shows_sizes(self):
        win = FakeWin([ord('r')])
        compacttui.display_selected(win, ["big"], self.opts, {"big": (1200, 1024 ** 3, 0)})
        self.assertIn("[1.2k|1.0g]", win.text())


class TestParallelScan(unittest.TestCase):
    """Счёт размеров идёт одновременно по всем папкам, а не по одной."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.names = []
        for index in range(8):
            name = "f%d" % index
            os.mkdir(os.path.join(self.tmp, name))
            self.names.append(name)
        self.opts = compacttui.parse_args([])

    def test_folders_are_counted_concurrently(self):
        original = compacttui.folder_info
        active = {"now": 0, "max": 0}
        lock = threading.Lock()

        def slow(path):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.1)
            with lock:
                active["now"] -= 1
            return (1, 1, 1)

        compacttui.folder_info = slow
        try:
            infos = compacttui.scan_infos(FakeWin(), self.tmp, self.names, self.opts)
        finally:
            compacttui.folder_info = original
        self.assertEqual(infos, [(1, 1, 1)] * len(self.names))
        self.assertGreaterEqual(active["max"], 3,
                                "папки считались не одновременно: %d" % active["max"])

    def test_single_folder_still_counted(self):
        win = FakeWin()
        infos = compacttui.scan_infos(win, self.tmp, ["f0"], self.opts)
        self.assertEqual(infos[0][0], 0)
        self.assertIsNotNone(infos[0])


class TestPendingAndReset(unittest.TestCase):
    """Очередь недожатых папок и Shift+G в меню."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.folder = os.path.join(self.tmp, "big")
        os.mkdir(self.folder)
        with open(os.path.join(self.folder, "a.bin"), "wb") as file:
            file.write(b"0" * 4096)
        self.opts = compacttui.parse_args([])
        self.saved = (compacttui.HISTORY_PATH, compacttui.PENDING_PATH, compacttui.DISMISSED_PATH)
        store = os.path.join(self.tmp, "hist")
        compacttui.HISTORY_PATH = os.path.join(store, "compressed-folders.tsv")
        compacttui.PENDING_PATH = os.path.join(store, "pending-folders.tsv")
        compacttui.DISMISSED_PATH = os.path.join(store, "cleared-folders.tsv")

    def tearDown(self):
        compacttui.HISTORY_PATH, compacttui.PENDING_PATH, compacttui.DISMISSED_PATH = self.saved

    def test_pending_roundtrip(self):
        compacttui.record_pending(self.folder)
        self.assertIn(compacttui.history_key(self.folder), compacttui.load_pending())
        compacttui.clear_pending(self.folder)
        self.assertEqual(compacttui.load_pending(), {})

    def test_clear_history_removes_both_lists(self):
        compacttui.record_pending(self.folder)
        compacttui.record_history(self.folder, 0, 2.0, 0.5)
        self.assertTrue(compacttui.clear_history())
        self.assertEqual((compacttui.load_history(), compacttui.load_pending()), ({}, {}))
        self.assertFalse(compacttui.clear_history(), "повторная очистка должна сказать «пусто»")

    def test_folder_state_prefers_pending(self):
        key = compacttui.history_key(self.folder)
        history, pending = {key: "t"}, {key: ("t", self.folder)}
        self.assertEqual(compacttui.folder_state(self.folder, None, history, pending), "pending")
        self.assertEqual(compacttui.folder_state(self.folder, None, history, {}), "compressed")
        self.assertEqual(compacttui.folder_state(self.folder, None, {}, {}), "")

    def _paint(self, history, pending):
        win = FakeWin([ord('r')])
        compacttui.menu(win, ["big"], [False], self.opts, self.tmp, [None], history, pending)
        position = [pos for pos, text in win.lines.items()
                    if text.startswith("[ ] big")][0]
        return win.attrs[position] & ~curses.A_REVERSE

    def test_queued_folder_is_orange_and_done_folder_green(self):
        original = compacttui.attr
        compacttui.attr = lambda pair: {1: 0x100, 2: 0x200, 4: 0x400, 8: 0x800}.get(pair, 0)
        key = compacttui.history_key(self.folder)
        try:
            self.assertEqual(self._paint({}, {key: ("t", self.folder)}), 0x800)
            self.assertEqual(self._paint({key: "t"}, {}), 0x200)
            self.assertEqual(self._paint({key: "t"}, {key: ("t", self.folder)}), 0x800)
            self.assertEqual(self._paint({}, {}), 0)
        finally:
            compacttui.attr = original

    def test_shift_g_clears_history_and_repaints(self):
        compacttui.record_pending(self.folder)
        compacttui.record_history(self.folder, 0, 2.0, 0.5)
        win = FakeWin([ord('G'), ord('r')])
        compacttui.menu(win, ["big"], [False], self.opts, self.tmp, [None],
                  compacttui.load_history(), compacttui.load_pending())
        self.assertEqual((compacttui.load_history(), compacttui.load_pending()), ({}, {}))
        self.assertIn("очищена", " ".join(win.history))

    SQUEEZED = (10, 400, 1000)      # на диске 40% логического — «сжата» по размеру

    def _shift_g(self, infos):
        win = FakeWin([ord('G'), ord('r')])
        compacttui.menu(win, ["big"], [False], self.opts, self.tmp, infos, {}, {})
        return compacttui.load_dismissed()

    def test_shift_g_clears_green_guessed_by_size(self):
        """Папку сжали мимо программы: зелёный по размеру Shift+G тоже снимает."""
        self.assertEqual(compacttui.folder_state(self.folder, self.SQUEEZED, {}, {}), "compressed")
        dismissed = self._shift_g([self.SQUEEZED])
        self.assertIn(compacttui.history_key(self.folder), dismissed,
                      "Shift+G не запомнил, что зелёный снят")
        self.assertEqual(compacttui.folder_state(self.folder, self.SQUEEZED, {}, {}, dismissed), "",
                         "папка осталась зелёной после Shift+G")

    def test_shift_g_clears_green_in_menu_right_away(self):
        """Меню после Shift+G рисует папку обычным цветом, без перезапуска."""
        original = compacttui.attr
        compacttui.attr = lambda pair: {1: 0x100, 2: 0x200, 4: 0x400, 8: 0x800}.get(pair, 0)
        try:
            self.assertEqual(self._paint({}, {}), 0, "папка без данных не должна быть цветной")
            dismissed = {compacttui.history_key(self.folder): 400.0}
            win = FakeWin([ord('r')])
            compacttui.menu(win, ["big"], [False], self.opts, self.tmp, [self.SQUEEZED],
                      {}, {}, dismissed)
            position = [pos for pos, text in win.lines.items()
                        if text.startswith("[ ] big")][0]
            self.assertEqual(win.attrs[position] & ~curses.A_REVERSE, 0,
                             "снятый зелёный всё ещё красит папку")
        finally:
            compacttui.attr = original

    def test_green_returns_only_after_real_compression(self):
        dismissed = self._shift_g([self.SQUEEZED])
        self.assertEqual(compacttui.folder_state(self.folder, (10, 395, 1000), {}, {}, dismissed), "",
                         "папка позеленела без нового сжатия")
        self.assertEqual(compacttui.folder_state(self.folder, (10, 300, 1000), {}, {}, dismissed),
                         "compressed", "папка не позеленела после нового сжатия")

    def test_shift_g_without_sizes_still_clears(self):
        """Запуск с -n: размеров нет, но зелёный по размеру тоже надо погасить."""
        dismissed = self._shift_g([None])
        self.assertEqual(compacttui.folder_state(self.folder, self.SQUEEZED, {}, {}, dismissed), "")


class TestCompactWindow(unittest.TestCase):
    """Окно сжатия: свёрнутое, в своей папке и с именем папки в заголовке."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.folder = os.path.join(self.tmp, "My Folder & Co")
        os.mkdir(self.folder)
        self.saved_popen = compacttui.subprocess.Popen
        self.saved_title = compacttui.set_window_title
        self.titles = []

    def tearDown(self):
        compacttui.subprocess.Popen = self.saved_popen
        compacttui.set_window_title = self.saved_title

    def _launch(self, force=False):
        calls = []

        class FakeProc:
            returncode = 0
            pid = 4242

        def fake_popen(args, **kwargs):
            calls.append((args, kwargs))
            return FakeProc()

        def fake_title(pid, title, **_kwargs):
            self.titles.append((pid, title))

        compacttui.subprocess.Popen = fake_popen
        compacttui.set_window_title = fake_title
        compacttui.start_compact(self.folder, force)
        return calls[0]

    def test_compact_runs_in_its_own_folder(self):
        args, kwargs = self._launch()
        self.assertEqual(args, compacttui.compact_command(".", False),
                         "compact должен получать «/S:.»")
        self.assertEqual(kwargs["cwd"], self.folder, "compact запущен не из своей папки")
        self.assertEqual(kwargs["creationflags"], compacttui.subprocess.CREATE_NEW_CONSOLE)
        self.assertEqual(kwargs["startupinfo"].wShowWindow, compacttui.SW_SHOWMINNOACTIVE)
        self.assertTrue(kwargs["startupinfo"].dwFlags & compacttui.subprocess.STARTF_USESHOWWINDOW,
                        "окно не просит свёрнутый показ")

    def test_window_gets_the_folder_title(self):
        self._launch()
        deadline = time.time() + 2
        while not self.titles and time.time() < deadline:
            time.sleep(0.01)
        expected = "Сжатие: " + os.path.normpath(os.path.abspath(self.folder))
        self.assertEqual(self.titles, [(4242, expected)])

    def test_title_is_the_folder_name_verbatim(self):
        name = "p(A) [b] & ^ %OS% ! ' ; , + = ~"
        folder = os.path.join(self.tmp, name)
        title = compacttui.compact_window_title(folder)
        self.assertEqual(title, "Сжатие: " + os.path.normpath(os.path.abspath(folder)))
        self.assertNotIn('"', title)

    def test_force_adds_the_flag(self):
        args, _kwargs = self._launch(force=True)
        self.assertEqual(args, compacttui.compact_command(".", True))
        self.assertEqual(args[-1], "/F")


class TestPendingDuringCompaction(unittest.TestCase):
    """Очередь: папка попадает в неё при запуске, а уходит только при успехе."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.folder = os.path.join(self.tmp, "big")
        os.mkdir(self.folder)
        with open(os.path.join(self.folder, "a.bin"), "wb") as file:
            file.write(b"0" * 4096)
        self.saved = (compacttui.HISTORY_PATH, compacttui.PENDING_PATH, compacttui.DISMISSED_PATH)
        store = os.path.join(self.tmp, "hist")
        compacttui.HISTORY_PATH = os.path.join(store, "compressed-folders.tsv")
        compacttui.PENDING_PATH = os.path.join(store, "pending-folders.tsv")
        compacttui.DISMISSED_PATH = os.path.join(store, "cleared-folders.tsv")

    def tearDown(self):
        compacttui.HISTORY_PATH, compacttui.PENDING_PATH, compacttui.DISMISSED_PATH = self.saved

    def _run(self, code, hdd=False):
        seen = []

        class FakeProc:
            returncode = code

            def poll(self):
                return code

        def fake_start(path, force):
            seen.append(dict(compacttui.load_pending()))
            return FakeProc()

        original = compacttui.start_compact
        compacttui.start_compact = fake_start
        try:
            opts = compacttui.parse_args([])
            opts["hdd"] = hdd
            results = compacttui.compact_folders(FakeWin([-1] * 4), self.tmp, ["big"], opts)
        finally:
            compacttui.start_compact = original
        return results, seen

    def test_queued_before_launch_and_cleared_after_success(self):
        for hdd in (False, True):
            compacttui.clear_history()
            results, seen = self._run(0, hdd)
            self.assertEqual(results[0][1], 0)
            self.assertIn(compacttui.history_key(self.folder), seen[0],
                          "папка не попала в очередь до запуска compact")
            self.assertEqual(compacttui.load_pending(), {}, "успешная папка осталась в очереди")

    def test_failed_folder_stays_queued(self):
        compacttui.clear_history()
        results, _seen = self._run(1)
        self.assertEqual(results[0][1], 1)
        self.assertIn(compacttui.history_key(self.folder), compacttui.load_pending(),
                      "недожатая папка не осталась в очереди")
        self.assertEqual(compacttui.load_history(), {}, "неудачу записали в историю")


class TestCompactionStatus(unittest.TestCase):
    """Экран подтверждения показывает ход сжатия и итог, а не молчит."""

    class TimedProc:
        """Процесс, который сначала жив, а потом завершается с заданным кодом."""

        def __init__(self, ticks=1, code=0):
            self.ticks, self.code, self.returncode = ticks, code, None

        def poll(self):
            if self.ticks > 0:
                self.ticks -= 1
                return None
            self.returncode = self.code
            return self.code

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved_start = compacttui.start_compact
        self.saved_log = compacttui.LOG_PATH
        compacttui.LOG_PATH = os.path.join(self.tmp, "log.txt")

    def tearDown(self):
        compacttui.start_compact = self.saved_start
        compacttui.LOG_PATH = self.saved_log

    def folder(self, name):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "data.bin"), "wb") as f:
            f.write(b"\0" * 4096)
        return path

    def run_compaction(self, selected, code=0, ticks=1, hdd=True):
        """Прогнать сжатие на подставном compact и вернуть экран после него."""
        codes = list(code) if isinstance(code, (list, tuple)) else [code] * len(selected)
        started = []

        def fake_start(path, force):
            started.append(path)
            return self.TimedProc(ticks, codes[len(started) - 1])

        compacttui.start_compact = fake_start
        opts = compacttui.parse_args(["-hdd"] if hdd else [])
        win = FakeWin([-1] * 20)
        compacttui.compact_folders(win, self.tmp, selected, opts)
        return win, started

    def test_folders_are_marked_waiting_before_start(self):
        compacttui.draw_selection(FakeWin(), ["a", "b"], compacttui.parse_args([]), None,
                            tags={"a": compacttui.TAG_WAITING, "b": compacttui.TAG_WAITING})
        win = FakeWin([-1])
        compacttui.draw_selection(win, ["a", "b"], compacttui.parse_args([]), None,
                            tags={"a": compacttui.TAG_WAITING, "b": compacttui.TAG_RUNNING})
        self.assertIn(compacttui.TAG_WAITING, win.text())
        self.assertIn(compacttui.TAG_RUNNING, win.text())

    def test_confirm_screen_has_no_status_marks(self):
        win = FakeWin([-1])
        compacttui.display_selected(win, ["big"], compacttui.parse_args([]), None)
        for tag in (compacttui.TAG_WAITING, compacttui.TAG_RUNNING, compacttui.TAG_DONE, compacttui.TAG_FAILED):
            self.assertNotIn(tag, win.text())

    def test_progress_is_shown_while_compressing(self):
        self.folder("one")
        seen = []
        original_draw = compacttui.draw_selection

        def spy(stdscr, selected, opts, infos=None, tags=None, **kwargs):
            seen.append((dict(tags or {}), kwargs.get("progress", "")))
            return original_draw(stdscr, selected, opts, infos, tags=tags, **kwargs)

        compacttui.draw_selection = spy
        try:
            _win, started = self.run_compaction(["one"], ticks=2)
        finally:
            compacttui.draw_selection = original_draw
        self.assertEqual(len(started), 1, "compact запущен не был")
        self.assertTrue(any(sum(v == compacttui.TAG_RUNNING for v in tags.values()) == 1
                            for tags, _progress in seen),
                        "пока сжатие идёт, папка ни разу не показана как «сжимается»")

    def test_finished_folder_is_marked_done_with_summary(self):
        self.folder("one")
        win, _started = self.run_compaction(["one"], code=0)
        self.assertIn(compacttui.TAG_DONE, win.text())
        self.assertIn("Готово: сжато 1 из 1", win.text())
        self.assertIn("ошибок 0", win.text())
        self.assertIn("Enter — к выбору папок", win.text())

    def test_failed_folder_is_marked_and_counted(self):
        self.folder("one")
        self.folder("two")
        win, _started = self.run_compaction(["one", "two"], code=[0, 1])
        self.assertIn(compacttui.TAG_OK if hasattr(compacttui, "TAG_OK") else compacttui.TAG_DONE, win.text())
        self.assertIn(compacttui.TAG_FAILED, win.text())
        self.assertIn("ошибок 1", win.text())

    def test_parallel_mode_reports_each_folder(self):
        self.folder("one")
        self.folder("two")
        win, started = self.run_compaction(["one", "two"], hdd=False, ticks=1)
        self.assertEqual(len(started), 2, "в параллельном режиме запускаются обе папки")
        self.assertIn("Готово: сжато 2 из 2", win.text())

    def test_compaction_refreshes_info_of_one_folder(self):
        """Размеры сжатой папки обновляет само сжатие, без общего счёта."""
        self.folder("one")
        infos = {"one": (7, 999, 999)}          # заведомо старое
        compacttui.start_compact = lambda path, force: self.TimedProc(0, 0)
        compacttui.compact_folders(FakeWin([-1] * 4), self.tmp, ["one"],
                             compacttui.parse_args(["-hdd"]), infos)
        files, physical, logical = infos["one"]
        self.assertEqual(files, 1, "число файлов посчитано заново")
        self.assertEqual(logical, 4096, "логический размер посчитан заново")
        self.assertNotEqual(physical, 999, "«на диске» пересчитан после сжатия")

    def test_other_folders_info_is_kept(self):
        """Несжатые папки сохраняют свои размеры: пересчёта всех нет."""
        self.folder("one")
        self.folder("two")
        infos = {"one": (1, 4096, 4096), "two": (5, 555, 777)}
        compacttui.start_compact = lambda path, force: self.TimedProc(1, 0)
        compacttui.compact_folders(FakeWin([-1] * 4), self.tmp, ["one"],
                             compacttui.parse_args(["-hdd"]), infos)
        self.assertEqual(infos["two"], (5, 555, 777),
                         "размеры папки, которую не сжимали, не трогаются")

    def test_error_also_refreshes_info(self):
        """Даже с кодом != 0 папка могла частично ужаться — инфу обновляем."""
        self.folder("one")
        infos = {"one": (1, 999, 999)}
        compacttui.start_compact = lambda path, force: self.TimedProc(1, 1)
        compacttui.compact_folders(FakeWin([-1] * 4), self.tmp, ["one"],
                             compacttui.parse_args(["-hdd"]), infos)
        self.assertNotEqual(infos["one"][2], 999,
                            "логический размер пересчитан и при ошибке")

    def test_launch_error_is_marked_failed(self):
        self.folder("one")

        def boom(path, force):
            raise OSError("нет прав")

        compacttui.start_compact = boom
        win = FakeWin([-1] * 4)
        compacttui.compact_folders(win, self.tmp, ["one"], compacttui.parse_args(["-hdd"]))
        self.assertIn(compacttui.TAG_FAILED, win.text())
        self.assertIn("ошибок 1", win.text())

    def test_summary_screen_waits_for_a_key(self):
        """Без нажатия программа не уходит в меню: итог должен быть прочитан."""
        self.folder("one")

        def fake_start(path, force):
            return self.TimedProc(0, 0)

        compacttui.start_compact = fake_start
        win = FakeWin([])            # ни одной клавиши: getch сообщит, что экран ждал
        opts = compacttui.parse_args(["-hdd"])
        with self.assertRaises(AssertionError):
            compacttui.compact_folders(win, self.tmp, ["one"], opts)

    def test_parallel_mode_refreshes_every_info(self):
        """Без -hdd папки идут одновременно — размеры обновляются у каждой."""
        self.folder("one")
        self.folder("two")
        infos = {"one": (9, 999, 999), "two": (9, 888, 888)}
        compacttui.start_compact = lambda path, force: self.TimedProc(1, 0)
        compacttui.compact_folders(FakeWin([-1] * 20), self.tmp, ["one", "two"],
                             compacttui.parse_args([]), infos)
        for name, stale in (("one", 999), ("two", 888)):
            self.assertNotEqual(infos[name][2], stale,
                                "логический размер %s пересчитан" % name)
            self.assertEqual(infos[name][0], 1, "число файлов %s посчитано" % name)

class TestAfterCompaction(unittest.TestCase):
    """Итог сжатия читается, Enter возвращает к экрану выбора папок."""

    def test_enter_on_summary_returns_to_folder_selection(self):
        order = []
        calls = []                   # сколько раз считали размеры
        names = ("menu", "display_selected", "compact_folders", "scan_infos",
                 "init_colors", "display_logo", "get_folders", "set_cursor",
                 "load_history", "load_pending", "load_dismissed")
        saved = {name: getattr(compacttui, name) for name in names}

        seen_infos = []

        def fake_menu(_stdscr, _folders, _selected, _opts, _path,
                      infos=None, *args, **kwargs):
            order.append("menu")
            seen_infos.append(infos)
            if order.count("menu") > 1:
                raise StopIteration("экран выбора папок показан снова")
            return ["big"]

        def fake_display(*args, **kwargs):
            order.append("confirm")
            return compacttui.ENTER_KEYS[0]

        def fake_compact(_stdscr, _path, _selected, _opts, infos=None):
            order.append("compact")
            infos["big"] = (1, 42, 100)      # папка ужалась
            return []

        try:
            compacttui.menu = fake_menu
            compacttui.display_selected = fake_display
            compacttui.compact_folders = fake_compact
            def fake_scan(_stdscr, _path, folders, _opts):
                calls.append(list(folders))
                return [None] * len(folders)

            compacttui.scan_infos = fake_scan
            compacttui.init_colors = lambda: None
            compacttui.display_logo = lambda stdscr: None
            compacttui.get_folders = lambda path: ["big"]
            compacttui.set_cursor = lambda visible: None
            compacttui.load_history = lambda: {}
            compacttui.load_pending = lambda: set()
            compacttui.load_dismissed = lambda: set()
            with self.assertRaises(StopIteration):
                compacttui.main(FakeWin([-1] * 4), compacttui.parse_args(["-p", "C:\\tmp", "-n"]))
        finally:
            for name, value in saved.items():
                setattr(compacttui, name, value)
        self.assertEqual(order, ["menu", "confirm", "compact", "menu"],
                         "после итога управление должно идти к выбору папок")
        self.assertEqual(len(calls), 1,
                         "вернувшись к выбору папок, размеры заново не считаем")
        self.assertEqual(seen_infos[-1], [(1, 42, 100)],
                         "меню получает размеры, обновлённые самим сжатием")

    def test_untouched_folder_keeps_its_sizes(self):
        """После сжатия новые размеры получает сжатая папка, соседние — старые."""
        order = []
        names = ("menu", "display_selected", "compact_folders", "scan_infos",
                 "init_colors", "display_logo", "get_folders", "set_cursor",
                 "load_history", "load_pending", "load_dismissed")
        saved = {name: getattr(compacttui, name) for name in names}
        seen_infos = []

        def fake_menu(_stdscr, _folders, _selected, _opts, _path,
                      infos=None, *args, **kwargs):
            order.append("menu")
            seen_infos.append(list(infos or []))
            if order.count("menu") > 1:
                raise StopIteration("экран выбора папок показан снова")
            return ["big"]

        def fake_display(*args, **kwargs):
            order.append("confirm")
            return compacttui.ENTER_KEYS[0]

        def fake_compact(_stdscr, _path, _selected, _opts, infos=None):
            order.append("compact")
            infos["big"] = (1, 42, 100)      # ужалась только эта папка
            return []

        def fake_scan(_stdscr, _path, folders, _opts):
            return [(9, 900, 900)] * len(folders)

        try:
            compacttui.menu = fake_menu
            compacttui.display_selected = fake_display
            compacttui.compact_folders = fake_compact
            compacttui.scan_infos = fake_scan
            compacttui.init_colors = lambda: None
            compacttui.display_logo = lambda stdscr: None
            compacttui.get_folders = lambda path: ["big", "small"]
            compacttui.set_cursor = lambda visible: None
            compacttui.load_history = lambda: {}
            compacttui.load_pending = lambda: set()
            compacttui.load_dismissed = lambda: set()
            with self.assertRaises(StopIteration):
                compacttui.main(FakeWin([-1] * 4), compacttui.parse_args(["-p", "C:\\tmp"]))
        finally:
            for name, value in saved.items():
                setattr(compacttui, name, value)
        self.assertEqual(order, ["menu", "confirm", "compact", "menu"])
        self.assertEqual(seen_infos[-1], [(1, 42, 100), (9, 900, 900)],
                         "у сжатой папки новые размеры, у соседней — прежние")
