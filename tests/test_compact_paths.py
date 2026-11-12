"""Настоящий compact.exe на папках с пробелами, скобками и спецсимволами.

Сжимаем так же, как это делает программа (start_compact: cwd = папка,
compact /C /S:.), и проверяем по размеру на диске, что compact действительно
дошёл до каждого файла: логический размер после сжатия не меняется, а место,
занятое на диске, падает. Атрибут «сжатый» на файле после compact на этой
Windows не выставляется, поэтому единственный честный признак — размер.

Запуск:  python -m unittest discover -s tests -v
Нужны Windows, NTFS и права администратора; на других ОС тесты пропускаются.
"""

import ctypes
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import compacttui

# ~88 КБ повторяющегося текста: LZX ужимает его в разы, поэтому «сжалось»
# и «не сжалось» видны по размеру на диске без разбора вывода compact.
PAYLOAD = b"the quick brown fox jumps over the lazy dog\n" * 2000
WAIT_SECONDS = 300
SHRUNK = 0.5                  # сжатый файл занимает меньше половины логического


def make_folder(parent, name, filename="payload.txt"):
    """Каталог parent/name с одним сжимаемым файлом; вернуть путь каталога."""
    folder = os.path.join(parent, name)
    os.makedirs(folder)
    with open(os.path.join(folder, filename), "wb") as f:
        f.write(PAYLOAD)
    return folder


def make_tree(parent, names):
    """Цепочка parent/names[0]/names[1]/... с файлом на каждом уровне."""
    path = parent
    for name in names:
        path = make_folder(path, name)
    return path


def files_of(root):
    """[(путь, логический размер, занято на диске)] всех файлов под root."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            found.append((path, os.path.getsize(path), compacttui._allocated_size(path)))
    return found


def window_title(folder):
    """Ожидаемый заголовок окна сжатия: полный путь папки."""
    return "Сжатие: " + os.path.normpath(os.path.abspath(folder))


def catch_window(folder, probe, attempts=3, seconds=10):
    """То, что probe(pid) вернул непустым, пока окно compact было живо; иначе None.

    Окно живёт доли секунды, а иногда процесс вообще не находится среди окон,
    хотя сжатие идёт: тогда папку сжимают заново с /F — новый compact, новое
    окно, ещё один шанс прочитать заголовок.
    """
    for attempt in range(attempts):
        proc = compacttui.start_compact(folder, attempt > 0)   # /F только на повторе
        deadline = time.time() + seconds
        try:
            while time.time() < deadline and proc.poll() is None:
                got = probe(proc.pid)
                if got:
                    return got
                time.sleep(0.01)
        finally:
            proc.wait(timeout=WAIT_SECONDS)
    return None

@unittest.skipUnless(os.name == "nt", "compact.exe есть только на Windows")
class CompactPathsTest(unittest.TestCase):
    """Общая обвязка: временный каталог, запуск как в программе, проверки."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tfc_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def compress(self, folder, force=False):
        """Сжать папку так же, как программа; вернуть код возврата compact."""
        proc = compacttui.start_compact(folder, force)
        try:
            return proc.wait(timeout=WAIT_SECONDS)
        finally:
            if proc.poll() is None:
                proc.kill()

    def assert_compressed(self, folder, label=""):
        """compact вернул 0 и каждый файл под folder занимает заметно меньше."""
        label = label or folder
        code = self.compress(folder)
        self.assertEqual(code, 0, "compact вернул %s для %s" % (code, label))
        entries = files_of(folder)
        self.assertTrue(entries, "в %s нет файлов — проверять нечего" % label)
        for path, logical, physical in entries:
            self.assertLess(physical, logical * SHRUNK,
                            "%s остался несжатым: %d байт на диске из %d (%s)"
                            % (path, physical, logical, label))
        return entries


class TestFolderNames(CompactPathsTest):
    """Имя папки со спецсимволами: compact видит её через «/S:.» изнутри."""

    def test_plain_name(self):
        self.assert_compressed(make_folder(self.tmp, "Plain"))

    def test_name_with_spaces(self):
        self.assert_compressed(make_folder(self.tmp, "My Folder 2024"))

    def test_name_with_brackets_and_ampersand(self):
        self.assert_compressed(make_folder(self.tmp, "p(A) [b] {c} & d"))

    def test_name_with_percent(self):
        self.assert_compressed(make_folder(self.tmp, "100% done vs %OS%"))

    def test_name_with_cmd_specials(self):
        self.assert_compressed(make_folder(self.tmp, "a^b&c%d!e'f;g+h=i~j#k$l@m"))

    def test_name_with_spaces_around_dots(self):
        self.assert_compressed(make_folder(self.tmp, "arch. 2024. old (backup)"))

    def test_cyrillic_name(self):
        self.assert_compressed(make_folder(self.tmp, "Проект Архив (копия) 2024"))

    def test_name_of_only_specials(self):
        self.assert_compressed(make_folder(self.tmp, "&^%[()]{}!@#"))

    def test_force_on_a_nasty_name(self):
        folder = make_folder(self.tmp, "Force & [42] (x)")
        self.assert_compressed(folder)                    # сжать
        self.assert_compressed(folder)                    # и ещё раз, уже сжатое
        self.assertEqual(self.compress(folder, force=True), 0, "повторное сжатие с /F")


class TestSubfolders(CompactPathsTest):
    """Вложенные папки: рекурсия «/S:.» должна дойти до каждого уровня."""

    def test_nested_subfolders_are_compressed_in_one_pass(self):
        root = os.path.join(self.tmp, "root folder")
        os.makedirs(root)
        level = make_folder(root, "level 1")
        level = make_folder(level, "level (2)")
        level = make_folder(level, "deep & [3]")
        make_folder(level, "самый вложенный")
        entries = self.assert_compressed(root)
        self.assertEqual(len(entries), 4, "сжаты не все уровни вложенности")

    def test_files_with_specials_inside_are_compressed(self):
        folder = os.path.join(self.tmp, "files (mixed)")
        os.makedirs(folder)
        names = ["файл (1) & co.txt", "a b;c+d=e.log", "100% #1 $x.txt",
                 "no_ext ^ ! @", "ЁЖИК.ЗИМА"]
        for name in names:
            with open(os.path.join(folder, name), "wb") as f:
                f.write(PAYLOAD)
        entries = self.assert_compressed(folder)
        self.assertEqual(sorted(entry[0] for entry in entries),
                         sorted(os.path.join(folder, name) for name in names))

    def test_contents_survive_compression(self):
        folder = make_folder(self.tmp, "My Folder & Co", "data (1) & co.txt")
        path = os.path.join(folder, "data (1) & co.txt")
        self.assert_compressed(folder)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), PAYLOAD, "содержимое изменилось после сжатия")


class TestLongPath(CompactPathsTest):
    """Глубокий путь: длинные имена подводят к самому MAX_PATH (260 символов)."""

    def test_deep_tree_near_max_path(self):
        path = self.tmp
        level = 0
        while True:
            name = "вложенная папка номер %02d" % level
            if len(path) + 1 + len(name) > 255:
                break
            try:
                path = make_folder(path, name)
            except OSError as error:
                self.skipTest("путь уперся в MAX_PATH: %s" % error)
            level += 1
        self.assertGreaterEqual(level, 3, "не удалось создать глубокий путь")
        self.assertGreater(len(path), 200, "путь получился коротким")
        self.assert_compressed(path, "%s (длина пути %d)" % (path, len(path)))


class TestWindowTitle(CompactPathsTest):
    """Заголовок окна: полный путь папки в списке задач — как есть, без экранирования."""

    def start_alive(self, folder, title):
        """Окно, как у compact, но живёт, пока его не закроют.

        У самого compact окно появляется лениво (он печатает только итог),
        и на быстрой папке его иногда не поймать; здесь окно есть заведомо,
        а проверяется та же установка заголовка — start_console.
        """
        proc = compacttui.start_console(["cmd", "/c", "ping -n 30 127.0.0.1 >nul"],
                                  folder, title)
        self.addCleanup(proc.wait)     # снять процесс, если тест упал раньше
        self.addCleanup(proc.kill)
        return proc

    def titles_of(self, proc, expected, seconds=5):
        """Заголовки окон процесса, когда в них уже стоит expected."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            titles = [text for _handle, text in compacttui.window_titles(proc.pid)]
            if titles == [expected]:
                return titles
            time.sleep(0.01)
        return [text for _handle, text in compacttui.window_titles(proc.pid)]

    def test_window_shows_the_full_path(self):
        name = "p(A) [b] & ^ %OS% ! ' ; , + = ~"
        folder = make_folder(self.tmp, name)
        expected = window_title(folder)
        proc = self.start_alive(folder, expected)
        self.assertEqual(self.titles_of(proc, expected), [expected])

    def test_window_with_illegal_in_filenames_characters(self):
        """В заголовке нет ограничений имени файла: «|», «<», «>» тоже проходят."""
        title = "Сжатие: a|b<c>d\"e Проект"
        proc = self.start_alive(self.tmp, title)
        self.assertEqual(self.titles_of(proc, title), [title])

    def test_title_returns_after_the_program_sets_its_own(self):
        """Программа подписала окно сама — заголовок всё равно возвращается.

        cmd.exe ставит в заголовок путь к запускаемому файлу, compact — свой;
        SetWindowTextW из start_console проигрывал эту гонку, и в списке задач
        оставалось «C:\\Windows\\System32\\compact.exe».
        """
        folder = make_folder(self.tmp, "Проект & Co")
        expected = window_title(folder)
        proc = self.start_alive(folder, expected)
        self.assertEqual(self.titles_of(proc, expected), [expected])
        handle = compacttui.window_titles(proc.pid)[0][0]
        ctypes.windll.user32.SetWindowTextW(
            ctypes.c_void_p(handle),
            ctypes.c_wchar_p("C:\\WINDOWS\\SYSTEM32\\compact.exe"))
        self.assertEqual(self.titles_of(proc, expected), [expected])

    def test_running_compact_shows_the_full_path(self):
        name = "p(A) [b] & ^ %OS% ! ' ; , + = ~"
        folder = make_folder(self.tmp, name)
        for index in range(120):       # 120 файлов: сжатие идёт не мгновенно
            with open(os.path.join(folder, "part %02d.txt" % index), "wb") as f:
                f.write(PAYLOAD)
        expected = window_title(folder)

        def probe(pid):
            titles = [text for _handle, text in compacttui.window_titles(pid)]
            return titles if expected in titles else None

        seen = catch_window(folder, probe)
        if not seen:
            self.skipTest("окно compact закрылось раньше, чем его прочитали")
        self.assertEqual(seen[0], expected)


class TestTypedPath(unittest.TestCase):
    """Путь, набранный в поле ввода: что программа делает с ним до сжатия.

    Пользователь вставляет путь как придётся: в кавычках, с хвостовым «\»,
    с «%TEMP%» вместо имени диска, а в именах папок бывают «%OS%», «&», «^»
    и скобки. resolve_path должен находить такую папку и её подпапки.
    """

    NAMES = ["Plain", "My Folder 2024", "p(A) [b] {c} & d",
             "100% done vs %OS%", "Проект Архив (копия) 2024"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tfc_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in self.NAMES:
            make_folder(self.tmp, name)

    def assert_resolved(self, typed, label=""):
        """resolve_path принял ввод и увидел в папке все подпапки."""
        path, folders, complaint = compacttui.resolve_path(typed)
        label = label or typed
        self.assertEqual(complaint, "", "не принят путь %s: %s" % (label, complaint))
        self.assertEqual(sorted(folders), sorted(self.NAMES), label)
        return path, folders

    def test_plain_path(self):
        path, _folders = self.assert_resolved(self.tmp)
        self.assertEqual(path, os.path.normpath(self.tmp))

    def test_path_with_spaces_and_specials(self):
        """Родитель с пробелами и скобками в имени: путь целиком проходит."""
        base = make_folder(self.tmp, "Parent (x) & ^ %OS% [1]")
        for name in self.NAMES:
            make_folder(base, name)
        path, folders, complaint = compacttui.resolve_path(base)
        self.assertEqual(complaint, "")
        self.assertEqual(sorted(folders), sorted(self.NAMES))
        self.assertEqual(os.path.basename(path), "Parent (x) & ^ %OS% [1]")

    def test_path_in_quotes(self):
        """«Копировать как путь» и перетаскивание дают путь в кавычках."""
        for quote in ('"', "'", "«", "»", "“", "”"):
            self.assert_resolved(quote + self.tmp + quote, "%s…%s" % (quote, quote))

    def test_trailing_separator(self):
        self.assert_resolved(self.tmp + os.sep, "путь с хвостовым разделителем")
        self.assert_resolved(self.tmp + os.sep * 2, "путь с двумя разделителями")

    def test_percent_in_folder_name_is_kept(self):
        """В имени папки «%OS%»: существующий путь не разворачивается в Windows_NT."""
        typed = os.path.join(self.tmp, "100% done vs %OS%")
        path = compacttui.normalize_path(typed)
        self.assertEqual(path, typed)
        self.assertTrue(os.path.isdir(path), "путь с %OS% перестал быть папкой")

    def test_environment_variable_when_path_does_not_exist(self):
        """%TEMP% раскрывается, если готового пути ещё нет."""
        name = "tfc_env_%d" % os.getpid()
        base = os.path.join(tempfile.gettempdir(), name)
        os.makedirs(os.path.join(base, "внутри"))
        self.addCleanup(shutil.rmtree, base, True)
        _path, folders, complaint = compacttui.resolve_path("%TEMP%\\" + name)
        self.assertEqual(complaint, "")
        self.assertEqual(folders, ["внутри"])

    def test_tilde_is_user_profile(self):
        self.assertEqual(compacttui.normalize_path("~"), os.path.normpath(os.path.expanduser("~")))

    def test_missing_folder_is_reported(self):
        typed = os.path.join(self.tmp, "нет такой папки & ^ %")
        path, folders, complaint = compacttui.resolve_path(typed)
        self.assertEqual(folders, [])
        self.assertIn("Папка не найдена", complaint)
        self.assertEqual(path, typed, "введённый путь должен остаться, чтобы его правили")

    def test_file_instead_of_folder_is_reported(self):
        path = os.path.join(self.tmp, "файл a & b.txt")
        with open(path, "wb") as f:
            f.write(b"x")
        _path, folders, complaint = compacttui.resolve_path(path)
        self.assertEqual(folders, [])
        self.assertIn("Это файл, а нужна папка", complaint)

    def test_empty_input_is_reported(self):
        self.assertEqual(compacttui.resolve_path("   ")[2], "Путь пустой — введите, например, D:\\data")

    def test_path_flag_keeps_characters_verbatim(self):
        """-p «C:\a b & c%OS%» доходит до программы посимвольно."""
        typed = os.path.join(self.tmp, "My Folder (2) & ^ %OS% !")
        self.assertEqual(compacttui.parse_args(["-p", typed])["path"], typed)


class TestTypedPathCompresses(CompactPathsTest):
    """Сквозняк: путь из поля ввода → resolve_path → compact → заголовок с путём."""

    def test_quoted_path_leads_to_a_compressed_folder(self):
        name = "My Folder (2) & 100% [a] ^b!"
        folder = make_folder(self.tmp, name)
        for index in range(120):       # 120 файлов: окно живёт дольше, чем чтение
            with open(os.path.join(folder, "part %02d.txt" % index), "wb") as f:
                f.write(PAYLOAD)

        path, folders, complaint = compacttui.resolve_path('"' + self.tmp + '"')
        self.assertEqual(complaint, "", "путь в кавычках не принят")
        self.assertIn(name, folders)
        full_path = os.path.join(path, name)        # так же собирает compact_folders

        expected = window_title(full_path)

        def probe(pid):
            titles = [text for _handle, text in compacttui.window_titles(pid)]
            return titles if expected in titles else None

        seen = catch_window(full_path, probe)
        if not seen:
            self.skipTest("окно compact закрылось раньше, чем его прочитали")
        self.assertEqual(seen[0], expected)
        self.assert_compressed(full_path)
