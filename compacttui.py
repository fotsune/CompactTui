"""Выбор папок в консоли (curses) и их сжатие встроенным compact.exe (NTFS, LZX).

Запуск:
    CompactTui.exe                        интерактивный выбор папок
    CompactTui.exe -p D:/data -a -hdd     сразу все папки, сжимать по одной
    CompactTui.exe -h                     справка

Флаги:
    -p <path>  каталог, папки которого показываются в меню
    -a         отметить все папки сразу
    -z         показывать полные пути
    -f         сжимать принудительно (ключ /F к compact)
    -hdd       последовательный режим: папки сжимаются по одной, программа ждёт
               каждую; без флага все папки сжимаются параллельно
    -l         не показывать логотип
    -n         не считать размеры: меню открывается сразу, без подписей
               вида [1.2k|13.2g]; зелёная подсветка сжатых папок остаётся
    -h, -help  вывести справку и выйти

Подпись рядом с папкой — [файлов|на диске]: 1.2k — 1200 файлов, 13.2g — 13.2 ГиБ.
Уже сжатые папки показаны зелёным: по истории из %TEMP%\\CompactTui или
по размеру на диске (папку сжали мимо программы). Поставленные в очередь и
не дожатые — оранжевым. Shift+G в меню стирает историю, очередь и зелёный,
угаданный по размеру; чтобы зелёный вернулся, папку надо сжать сильнее, чем
в момент очистки.

После Enter на подтверждении тот же экран показывает ход: у каждой папки
пометка [    ] ждёт, [ >> ] сжимается, [ OK ] сжато, [ !! ] ошибка; внизу —
сколько сжато, сколько освобождено и сколько прошло. По окончании экран
остаётся с итогом, Enter возвращает к выбору папок.

Окно каждого compact появляется свёрнутым и с полным путём папки в заголовке:
свёрнутым — чтобы не всплывать поверх работы, с путём — чтобы в списке задач
было видно, какая именно папка сжимается.
"""

import ctypes
import curses
import os
import subprocess
import sys
import tempfile
import threading
import time

ENTER_KEYS = (10, 13, curses.KEY_ENTER)
QUIT_KEYS = (ord('Q'), ord('Й'), ord('й'), 27)
BACK_KEYS = (ord('r'), ord('R'), ord('к'), ord('К'))
POLL_SECONDS = 1
ITEMS_PER_ROW = 3

APP_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))
LOG_PATH = os.path.join(APP_DIR, "CompactTui.log.txt")

# История сжатий переживает запуски, поэтому лежит в %TEMP%, а не рядом с exe:
# CompactTui.log.txt очищается на каждом старте и для этого не годится.
HISTORY_DIR = os.path.join(tempfile.gettempdir(), "CompactTui")
HISTORY_PATH = os.path.join(HISTORY_DIR, "compressed-folders.tsv")
PENDING_PATH = os.path.join(HISTORY_DIR, "pending-folders.tsv")
DISMISSED_PATH = os.path.join(HISTORY_DIR, "cleared-folders.tsv")
COMPRESSED_RATIO = 0.9      # на диске меньше 90% логического — папка уже сжата
REDISCOVER_RATIO = 0.98    # зелёный вернётся, только если папка ужалась сильнее
SW_SHOWMINNOACTIVE = 7      # окно compact появляется свёрнутым, не поверх меню
SCAN_WORKERS = 32          # сколько папок считать одновременно
SCAN_POLL = 0.1            # как часто счёт обновляет строку состояния, секунды


# --------------------------------------------------------------------------- аргументы
def parse_args(argv):
    """Разобрать командную строку в словарь настроек.

    Возвращает dict с ключами path, select_all, full_paths, force, hdd,
    logo, sizes, help.
    """
    opts = {"path": None, "select_all": False, "full_paths": False,
            "force": False, "hdd": False, "logo": True, "sizes": True,
            "help": False}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "-help", "--help"):
            opts["help"] = True
        elif arg == "-p":
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                opts["path"] = argv[i + 1]
                i += 1
            else:
                opts["path"] = ""      # -p без значения: спросим путь в меню
        elif arg == "-a":
            opts["select_all"] = True
        elif arg == "-z":
            opts["full_paths"] = True
        elif arg == "-f":
            opts["force"] = True
        elif arg == "-hdd":
            opts["hdd"] = True
        elif arg == "-l":
            opts["logo"] = False
        elif arg in ("-n", "-nosize"):
            opts["sizes"] = False
        i += 1
    return opts


def print_help():
    """Напечатать справку, не падая на консоли без кириллицы."""
    text = (__doc__ or "").strip()
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(text.encode(encoding, "replace").decode(encoding))


# --------------------------------------------------------------------------- права администратора
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return True     # не смогли проверить — не перезапускаемся


def relaunch_as_admin():
    """Перезапустить себя с правами администратора, сохранив аргументы как есть."""
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, sys.argv[1:]
    else:
        exe, args = sys.executable, [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    params = subprocess.list2cmdline(args)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)


# --------------------------------------------------------------------------- рисование
def put(stdscr, y, x, text, attr=0):
    """Написать text так, чтобы никогда не упасть на границе экрана.

    attr=0 означает "не задавать атрибуты": тогда действует то, что включено
    через attron(). Передача нуля в addstr, наоборот, сбросила бы подсветку.
    """
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    text = text[: max(0, w - x - 1)]
    if not text:
        return
    try:
        if attr:
            stdscr.addstr(y, x, text, attr)
        else:
            stdscr.addstr(y, x, text)
    except curses.error:
        pass


def attr(pair):
    """Цветовая пара как атрибут; 0, если цвета недоступны или curses не готов."""
    try:
        return curses.color_pair(pair)
    except (curses.error, AttributeError):
        return 0


def put_hint(stdscr, y, text):
    try:
        stdscr.move(y, 0)
        stdscr.clrtoeol()
    except curses.error:
        pass
    put(stdscr, y, 0, text, attr(8))


def put_centered(stdscr, y, text, attr=0):
    _, w = stdscr.getmaxyx()
    put(stdscr, y, max(0, w // 2 - len(text) // 2), text, attr)


def init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    pairs = {1: curses.COLOR_RED, 2: curses.COLOR_GREEN, 3: curses.COLOR_BLUE,
             4: curses.COLOR_CYAN, 6: curses.COLOR_MAGENTA, 7: curses.COLOR_WHITE,
             8: curses.COLOR_YELLOW, 9: curses.COLOR_BLACK}
    for pair, color in pairs.items():
        try:
            curses.init_pair(pair, color, curses.COLOR_BLACK)
        except curses.error:
            pass
    try:
        curses.init_pair(10, curses.COLOR_MAGENTA, curses.COLOR_GREEN)
    except curses.error:
        pass


def set_cursor(visible):
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass


LOGO = r"""
        `7MMMmMmYMM            mm                                              .
          MM    `7             MM                                              .
        MM        ,pWWWq.  mmMMmm   ,pPSYbd `7MM  `7MM   `7MMpMMMb.    .gPMYa  
          MMmMMM   6W'   `Wb   MM     8I   `'   MM    MM     MM    MM   ,M'   Yb 
          MM   Y   8M     M8   MM     `YMMMa.   MM    MM     MM    MM   8MMMMMMM 
          MM       YA.   ,A9   MM     L.   I8   MM    MM     MM    MM   YM.    , 
        .JMML.      `Ybmd9'    `Mbmo  M9mmmP'   `MbodmYML. .JMML  JMML.  `Mbmmd' 
"""


def display_logo(stdscr):
    stdscr.clear()
    h, _ = stdscr.getmaxyx()
    set_cursor(False)
    lines = [line.rstrip() for line in LOGO.splitlines()]
    top = max(0, h // 2 - len(lines) // 2)
    for i, line in enumerate(lines):
        put_centered(stdscr, top + i, line, attr(1))
    stdscr.refresh()
    time.sleep(1)
    stdscr.clear()
    set_cursor(True)


# --------------------------------------------------------------------------- файловая система
def get_folders(directory):
    """Имена подкаталогов directory (пустой список, если это не каталог)."""
    try:
        return sorted(name for name in os.listdir(directory)
                      if os.path.isdir(os.path.join(directory, name)))
    except OSError:
        return []


def _allocated_size(path):
    """Сколько байт файл реально занимает на диске (с учётом NTFS-сжатия)."""
    size_high = ctypes.c_ulong(0)
    low = ctypes.windll.kernel32.GetCompressedFileSizeW(
        ctypes.c_wchar_p(path), ctypes.byref(size_high))
    if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
        raise ctypes.WinError(ctypes.get_last_error())
    return (size_high.value << 32) + low


def get_folder_size(path='.'):
    """Логический размер каталога в ГиБ."""
    try:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for filename in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, filename))
                except OSError:
                    pass
        return round(total / (1024 ** 3), 3)
    except OSError:
        return 0


def get_folder_physical_size(path='.'):
    """Размер на диске в ГиБ (после NTFS-сжатия он меньше логического)."""
    try:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for filename in filenames:
                try:
                    total += _allocated_size(os.path.join(dirpath, filename))
                except OSError:
                    pass
        return round(total / (1024 ** 3), 3)
    except OSError:
        return 0


def folder_info(path):
    """(файлов, на диске, логический размер) за одну прогулку по каталогу.

    Логический размер при NTFS-сжатии не меняется, «на диске» — то, что
    реально занято, поэтому их отношение и говорит, сжата ли папка.
    """
    files = physical = logical = 0
    stack = [path]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            files += 1
            logical += stat.st_size
            try:
                physical += _allocated_size(entry.path)
            except OSError:
                physical += stat.st_size
    return files, physical, logical


def human_unit(value, unit):
    """13.2g для значений до 100, 78m дальше — без ложной точности."""
    return ("%.1f%s" if value < 100 else "%.0f%s") % (value, unit)


def human_count(number):
    """850, 1.2k, 13m — приблизительное число файлов."""
    for unit, factor in (("m", 1024 ** 2), ("k", 1024)):
        if number >= factor:
            return human_unit(number / factor, unit)
    return str(number)


def human_bytes(number):
    """512, 1.2k, 13.2g — сколько папка занимает на диске."""
    for unit, factor in (("t", 1024 ** 4), ("g", 1024 ** 3),
                         ("m", 1024 ** 2), ("k", 1024)):
        if number >= factor:
            return human_unit(number / factor, unit)
    return str(number)


def format_info(info):
    """Подпись рядом с папкой: [1.2k|13.2g] — файлов | на диске."""
    if not info:
        return ""
    files, physical, _logical = info
    return "[%s|%s]" % (human_count(files), human_bytes(physical))


def folder_label(name, info, width):
    """«имя[1.2k|13.2g]», вписанное в колонку шириной width.

    Длинное имя подрезается и помечается «~»: важнее видеть размер,
    а полное имя есть в режиме -z и на экране подтверждения.
    """
    if not info:
        return name
    suffix = format_info(info)
    room = width - 1 - len(suffix) - 1
    if room < 4:
        return suffix[:max(1, width - 1)]      # имя не влезает — только размер
    if len(name) > room:
        name = name[:room - 1] + "~"
    return name + suffix


def history_key(path):
    """Один и тот же каталог — один ключ, как бы путь ни был записан."""
    try:
        return os.path.normcase(os.path.abspath(path))
    except OSError:
        return os.path.normcase(path)



def _read_rows(path):
    """Строки TSV «метка времени \t путь» из файла (нет файла — пусто)."""
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            for line in file:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) >= 2 and parts[1]:
                    rows.append((parts[0], parts[1]))
    except OSError:
        pass
    return rows


def _write_rows(path, rows):
    """Перезаписать TSV целиком; нет прав — молча ничего."""
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)     # HISTORY_PATH может быть переопределён
        with open(path, "w", encoding="utf-8") as file:
            for stamp, line_path in rows:
                file.write("%s\t%s\n" % (stamp, line_path))
    except OSError:
        pass


def load_history():
    """Уже сжатые папки из %TEMP%\CompactTui: {ключ пути: время}."""
    return dict((history_key(path), stamp) for stamp, path in _read_rows(HISTORY_PATH))


def load_pending():
    """Поставленные в очередь, но не дожатые папки: {ключ пути: (время, путь)}."""
    return dict((history_key(path), (stamp, path))
                for stamp, path in _read_rows(PENDING_PATH))


def load_dismissed():
    """Папки, которым Shift+G снял зелёный: {ключ пути: байт на диске}.

    -1 значит «в момент очистки размеров не знали» (запуск с -n).
    """
    dismissed = {}
    for size, path in _read_rows(DISMISSED_PATH):
        try:
            dismissed[history_key(path)] = float(size)
        except ValueError:
            dismissed[history_key(path)] = -1.0
    return dismissed


def record_history(full_path, code, before, after):
    """Дописать удачное сжатие в историю; неудачи не записываем."""
    if code != 0:
        return
    try:
        folder = os.path.dirname(HISTORY_PATH)
        if folder:
            os.makedirs(folder, exist_ok=True)     # HISTORY_PATH может быть переопределён
        with open(HISTORY_PATH, "a", encoding="utf-8") as file:
            file.write("%s\t%s\t%.3f\t%.3f\t%0.3f\tкод %s\n"
                       % (time.strftime("%Y-%m-%d %H:%M:%S"), full_path,
                          before, after, before - after, code))
    except OSError:
        pass


def record_pending(full_path):
    """Запомнить, что папка поставлена в очередь на сжатие."""
    pending = load_pending()
    pending[history_key(full_path)] = (time.strftime("%Y-%m-%d %H:%M:%S"), full_path)
    _write_rows(PENDING_PATH, sorted(pending.values()))


def clear_pending(full_path):
    """Папка дожата — убрать её из очереди."""
    pending = load_pending()
    key = history_key(full_path)
    if key in pending:
        del pending[key]
        _write_rows(PENDING_PATH, sorted(pending.values()))


def clear_history():
    """Стереть историю сжатых папок и очередь недожатых.

    Возвращает True, если что-то действительно было стёрто.
    """
    removed = False
    for path in (HISTORY_PATH, PENDING_PATH):
        try:
            os.remove(path)
            removed = True
        except OSError:
            pass
    return removed


def compressed_already(full_path, info, history, dismissed=None):
    """Сжимали ли папку раньше: по истории или по размеру на диске.

    Второй путь нужен для папок, сжатых мимо программы (руками или другой
    утилитой): они в историю не попали, но видно, что данных на диске
    заметно меньше, чем логического размера файлов. Догадку по размеру
    снимает Shift+G: dismissed хранит, сколько папка занимала в момент
    очистки, и зелёной она станет снова, только ужавшись заметно сильнее
    (иначе подсветка возвращалась бы сразу после очистки).
    """
    key = history_key(full_path)
    if key in history:
        return True
    if not info:
        return False
    _files, physical, logical = info
    if logical <= 0 or physical >= logical * COMPRESSED_RATIO:
        return False
    base = (dismissed or {}).get(key)
    if base is None:
        return True
    return base >= 0 and physical < base * REDISCOVER_RATIO


def dismiss_compressed(infos):
    """Погасить зелёный, угаданный по размеру на диске (Shift+G).

    infos — {полный путь: (файлов, на диске, логический) или None}. Размер
    на диске на момент очистки запоминаем, иначе подсветка вернулась бы
    при следующем запуске: файл настроек переживает перезапуски.
    """
    dismissed = load_dismissed()
    for path, info in infos.items():
        dismissed[history_key(path)] = float(info[1]) if info else -1.0
    _write_rows(DISMISSED_PATH, [("%.3f" % size, path) for path, size in dismissed.items()])


def folder_state(full_path, info, history, pending=None, dismissed=None):
    """Состояние папки для меню: «pending», «compressed» или пусто.

    Очередь важнее истории: папку поставили на сжатие и не дожали — её
    надо видить оранжевой, даже если раньше она сжималась успешно.
    """
    if pending and history_key(full_path) in pending:
        return "pending"
    if compressed_already(full_path, info, history, dismissed):
        return "compressed"
    return ""


def poll_abort(stdscr):
    """Нажал ли пользователь Esc; только на настоящем окне curses."""
    if not hasattr(stdscr, "get_wch"):
        return False
    try:
        stdscr.nodelay(True)
        return stdscr.getch() in QUIT_KEYS
    except curses.error:
        return False
    finally:
        stdscr.nodelay(False)


def scan_infos(stdscr, base_path, folders, opts):
    """Размеры и число файлов для каждой папки меню (None — не считали).

    Папки считаются одновременно, SCAN_WORKERS потоками: обход — это
    ожидание диска, и последовательный счёт растягивал открытие меню.
    Строка состояния показывает, сколько готово; Esc прекращает счёт.
    """
    if not opts.get("sizes", True):
        return [None] * len(folders)
    if not folders:
        return []
    h, _ = stdscr.getmaxyx()
    results = [None] * len(folders)
    queue = list(enumerate(folders))
    state = {"done": 0, "abort": False}
    lock = threading.Lock()

    def work():
        while True:
            with lock:
                if state["abort"] or not queue:
                    return
                index, folder = queue.pop(0)
            try:
                info = folder_info(os.path.join(base_path, folder))
            except OSError:
                info = None
            with lock:
                results[index] = info
                state["done"] += 1

    workers = [threading.Thread(target=work, daemon=True)
               for _ in range(min(SCAN_WORKERS, len(folders)))]
    for worker in workers:
        worker.start()
    while True:
        with lock:
            done, aborted = state["done"], state["abort"]
        if aborted or done >= len(folders):
            break
        put_hint(stdscr, h - 6, "Считаю %d из %d одновременно (Esc — не считать)"
                 % (done, len(folders)))
        stdscr.refresh()
        if poll_abort(stdscr):
            with lock:
                state["abort"] = True
            break
        time.sleep(SCAN_POLL)
    if state["abort"]:
        put_hint(stdscr, h - 6, "Счёт прерван: у остальных папок размер неизвестен")
        stdscr.refresh()
    else:
        for worker in workers:
            worker.join(SCAN_POLL * 2)
    return list(results)


def write_file(text):
    """Дописать строку в CompactTui.log.txt рядом с программой."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as file:
            file.write(text + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- compact
def compact_command(full_path, force):
    """Аргументы compact.exe для сжатия каталога в LZX."""
    cmd = ["compact", "/C", "/S:" + full_path, "/A", "/I", "/EXE:LZX"]
    if force:
        cmd.append("/F")
    return cmd


def compact_window_title(full_path):
    """Заголовок окна сжатия: полный путь папки, а не только её имя.

    В заголовке видно, какую именно папку сжимают, даже если папок
    с одинаковыми именами несколько.
    """
    return "Сжатие: %s" % os.path.normpath(os.path.abspath(str(full_path)))


def quiet_startupinfo():
    """Окно compact появляется свёрнутым и не отбирает фокус у меню."""
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = SW_SHOWMINNOACTIVE
    return info


def window_titles(pid):
    """[(HWND, заголовок)] окон верхнего уровня, принадлежащих процессу pid."""
    user32 = ctypes.windll.user32
    found = []

    def collect(handle, _lparam):
        owner = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid:
            text = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(handle, text, 512)
            found.append((handle, text.value))
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(collect)
    user32.EnumWindows(callback, 0)
    return found


def set_window_title(pid, title, timeout=2.0, delay=0.01, keep=False,
                     alive=None):
    """Дать окну процесса pid заголовок title; вернуть HWND окна или None.

    Окно консоли появляется не мгновенно, поэтому ждём до timeout секунд
    (а при keep — пока alive() не скажет, что процесс кончился).

    Заголовок ставится снаружи, а не командой «title» в командной строке:
    внутри «title ... & compact ...» имя папки с "&", "^", "|" или "%OS%"
    ломало разбор командной строки, а SetWindowTextW отдаёт имя как есть.

    Одной установки мало: консольные программы подписывают своё окно сами
    (cmd.exe — путём к запускаемому файлу), и если успевают после нас, в
    списке задач остаётся «C:\\Windows\\System32\\compact.exe». Поэтому с
    keep=True заголовок возвращается, пока окно живо.
    """
    user32 = ctypes.windll.user32
    deadline = time.time() + timeout
    while True:
        windows = window_titles(pid)
        if windows:
            handle = windows[0][0]
            break
        if time.time() >= deadline or (alive is not None and not alive()):
            return None
        time.sleep(delay)
    user32.SetWindowTextW(ctypes.c_void_p(handle), ctypes.c_wchar_p(title))
    while keep and user32.IsWindow(ctypes.c_void_p(handle)):
        text = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(ctypes.c_void_p(handle), text, 512)
        if text.value != title:
            user32.SetWindowTextW(ctypes.c_void_p(handle),
                                   ctypes.c_wchar_p(title))
        time.sleep(0.1)
    return handle


def start_console(arguments, folder, title):
    """Запустить программу в своём окне, свёрнутым, с заголовком title.

    Заголовок ставит фоновый поток через SetWindowTextW, а не команда
    «title ... & ...»: имена папок с «&», «^» или «%OS%» ломали разбор
    командной строки, а снаружи имя попадает в окно как есть.
    """
    proc = subprocess.Popen(arguments,
                             cwd=folder,
                             creationflags=subprocess.CREATE_NEW_CONSOLE,
                             startupinfo=quiet_startupinfo())
    threading.Thread(target=set_window_title, args=(proc.pid, title),
                     kwargs=dict(keep=True, alive=lambda: proc.poll() is None),
                     daemon=True).start()
    return proc


def start_compact(full_path, force):
    """Запустить compact в отдельном окне: свёрнутым, с полным путём в заголовке.

    compact не понимает путь с пробелом ни как "/S:C:\\a b", ни как
    /S:"C:\\a b" — отвечает «Invalid drive specification», — поэтому окно
    открывается уже в нужной папке (cwd), а compact получает "/S:." и обходит
    её рекурсивно.
    """
    return start_console(compact_command(".", force),  # compact сжимает «/S:.» — эту папку
                         full_path, compact_window_title(full_path))


# пометки папки на экране подтверждения: по ним видно ход сжатия
TAG_WAITING = "[    ]"
TAG_RUNNING = "[ >> ]"
TAG_DONE = "[ OK ]"
TAG_FAILED = "[ !! ]"
TAG_STYLE = {TAG_WAITING: 0, TAG_RUNNING: 8, TAG_DONE: 2, TAG_FAILED: 1}


def draw_selection(stdscr, selected, opts, infos=None, tags=None,
                   progress="", hint=None, title=None):
    """Экран «Вы выбрали следующие папки» — он же экран хода сжатия.

    tags — {имя папки: пометка TAG_*}: пока сжатие идёт, у каждой папки
    видно, чего она ждёт; progress — строка «сжато N из M, освобождено…»,
    hint — нижняя подсказка. Без tags это просто подтверждение выбора.
    """
    stdscr.clear()
    h, w = stdscr.getmaxyx()
    put_centered(stdscr, 1, title or "Вы выбрали следующие папки:", attr(2))

    column = w // ITEMS_PER_ROW
    for i, folder in enumerate(selected):
        tag = (tags or {}).get(folder, "")
        name = str(folder) if opts["full_paths"] else folder
        indent = len(tag) + 2 if tag else 0
        row = (i // ITEMS_PER_ROW) + 3
        col = (i % ITEMS_PER_ROW) * column + 2
        if tag:
            put(stdscr, row, col, tag, attr(TAG_STYLE.get(tag, 0)))
        start = col + indent
        room = min(column - indent, w - start - 1)      # не упираться в край экрана
        put(stdscr, row, start,
            folder_label(name, (infos or {}).get(folder), max(4, room)))

    mode = "Последовательно" if opts["hdd"] else "Параллельно"
    put_hint(stdscr, h - 4, "Режим: %s   Сжатие принудительно: %s"
             % (mode, "да" if opts["force"] else "нет"))
    put_hint(stdscr, h - 3, progress)
    put_hint(stdscr, h - 2, hint or "Нажмите 'R' для возврата или Enter для запуска.")
    stdscr.refresh()


def display_selected(stdscr, selected, opts, infos=None):
    """Экран подтверждения: выбранные папки, их размеры и подсказки.

    infos — {(имя папки): (файлов, на диске, логический)}; с -n его нет.
    """
    draw_selection(stdscr, selected, opts, infos)
    return stdscr.getch()


def compact_folders(stdscr, base_path, selected, opts, infos=None):
    """Сжать выбранные папки, показывая ход и итог на экране подтверждения.

    В режиме -hdd каталоги обрабатываются по одному, программа ждёт каждый;
    без него все compact запускаются сразу. Возвращает список
    (путь, код возврата, освобождено ГиБ).

    Экран не гаснет и в меню сама программа не уходит: у каждой папки
    пометка «ждёт / сжимается / сжато / ошибка», внизу — сколько сделано,
    сколько освобождено и сколько прошло, а по окончании итог и «Enter —
    к выбору папок». Иначе человек, нажавший Enter, не узнал бы,
    закончилось ли сжатие. Размеры сжатых папок функция тут же пишет
    в infos, так что полный счёт для меню после сжатия не нужен.
    Esc перестаёт обновлять экран, но сжатие доводится до конца,
    и итог всё равно показывается.

    infos — {имя папки: (файлов, на диске, логический)}; сюда же
    ложатся размеры уже сжатых папок.

    Запуск сразу попадает в очередь недожатых (%TEMP%\CompactTui) и
    уходит оттуда только при удачном завершении: прерванное сжатие остаётся
    в очереди и видно в меню оранжевым.
    """
    items = [(name, name if os.path.isdir(name) else os.path.join(base_path, name))
             for name in selected]
    tags = {name: TAG_WAITING for name, _path in items}
    results = []
    started = time.time()

    def freed():
        return round(sum(saved for _path, _code, saved in results), 3)

    def errors():
        return sum(1 for _path, code, _saved in results if code != 0)

    def draw(progress, hint, title="Сжимаем следующие папки:"):
        draw_selection(stdscr, selected, opts, infos, tags=tags, title=title,
                       progress=progress, hint=hint)

    def progress_line():
        return "Сжато %d из %d, освобождено %.3f ГиБ, прошло %d с" % (
            len(results), len(items), freed(), int(time.time() - started))

    def key():
        """Нажатие без ожидания: -1, если ничего не нажато."""
        try:
            return stdscr.getch()
        except curses.error:
            return -1

    def finish(proc, name, full_path, before):
        info = folder_info(full_path)      # «на диске» и файлы — за ту же прогулку
        after = round(info[1] / (1024 ** 3), 3)
        if infos is not None:
            infos[name] = info             # меню покажет новые размеры без счёта
        saved = round(before - after, 3)
        results.append((full_path, proc.returncode, saved))
        record_history(full_path, proc.returncode, before, after)
        if proc.returncode == 0:
            clear_pending(full_path)      # дожато; иначе папка остаётся в очереди
        write_file("%s\tна диске %.3f -> %.3f ГиБ\t-%0.3f\tкод %s"
                   % (full_path, before, after, saved, proc.returncode))
        return saved

    def launch(full_path):
        record_pending(full_path)      # в очередь до запуска: прерванное
        return start_compact(full_path, opts["force"])  # останется оранжевым

    def launch_failed(name, full_path, error):
        tags[name] = TAG_FAILED
        write_file("%s\tОШИБКА ЗАПУСКА\t%s" % (full_path, error))
        results.append((full_path, -1, 0.0))

    def watch(proc, name, full_path, before):
        """Ждать compact, обновляя пометку и строку хода."""
        tags[name] = TAG_RUNNING
        stdscr.nodelay(True)
        watching = True
        try:
            while proc.poll() is None:
                if watching:
                    draw(progress_line(), "Esc — не ждать: сжатие доводится до конца")
                    if key() in QUIT_KEYS:
                        watching = False
                time.sleep(POLL_SECONDS)
        finally:
            stdscr.nodelay(False)
        finish(proc, name, full_path, before)
        tags[name] = TAG_DONE if proc.returncode == 0 else TAG_FAILED
        draw(progress_line(), "Esc — не ждать: сжатие доводится до конца")

    draw(progress_line(), "Esc — не ждать: сжатие доводится до конца")

    if opts["hdd"]:
        for name, full_path in items:
            before = get_folder_physical_size(full_path)
            try:
                proc = launch(full_path)
            except OSError as error:
                launch_failed(name, full_path, error)
                continue
            watch(proc, name, full_path, before)
    else:
        running = []
        for name, full_path in items:
            before = get_folder_physical_size(full_path)
            try:
                proc = launch(full_path)
            except OSError as error:
                launch_failed(name, full_path, error)
                continue
            tags[name] = TAG_RUNNING
            running.append((proc, name, full_path, before))
        stdscr.nodelay(True)
        try:
            while running:
                draw(progress_line(), "Esc — не ждать: сжатие доводится до конца")
                key()                      # все процессы уже идут: клавиши не мешают
                for entry in [item for item in running if item[0].poll() is not None]:
                    proc, name, full_path, before = entry
                    finish(proc, name, full_path, before)
                    tags[name] = TAG_DONE if proc.returncode == 0 else TAG_FAILED
                    running.remove(entry)
                if running:
                    time.sleep(POLL_SECONDS)
        finally:
            stdscr.nodelay(False)

    summary = ("Готово: сжато %d из %d, освобождено %.3f ГиБ, ошибок %d, прошло %d с"
               % (len(results) - errors(), len(items), freed(), errors(),
                  int(time.time() - started)))
    draw(summary, "Enter — к выбору папок")
    try:
        stdscr.getch()                     # дать прочитать итог, а не мигнуть им
    except curses.error:
        pass
    return results


def menu(stdscr, folders, selected_folders, opts, base_path, infos=None,
         history=None, pending=None, dismissed=None):
    """Меню выбора папок. Возвращает список выбранных папок или None.

    infos — (файлов, на диске, логический размер) по папкам меню,
    history — ключи уже сжатых папок (зелёные), pending — ключи
    поставленных в очередь и не дожатых (оранжевые), dismissed — зелёный,
    снятый Shift+G (ключ: размер на диске в момент очистки).
    Shift+G стирает историю, очередь и угаданный по размеру зелёный, затем
    перекрашивает меню.
    """
    set_cursor(False)
    current_index = 0
    infos = list(infos if infos is not None else [None] * len(folders))
    history = history or {}
    pending = pending or {}
    dismissed = dismissed or {}
    notice = ""

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        column = w // ITEMS_PER_ROW

        put_hint(stdscr, h - 5, "{Пробел для выбора, Enter для подтверждения}")
        put_hint(stdscr, h - 4, "{'A' для выбора/отмены всех, Shift+Q Выход}")
        put_hint(stdscr, h - 3, "['F' Сжать принудительно: %s] ['D' Режим HDD: %s]"
                 % ("True" if opts["force"] else "False",
                    "True" if opts["hdd"] else "False"))
        put_hint(stdscr, h - 2, notice or "{[файлов|на диске]} зелёная — сжата · "
                 "оранжевая — не дожата · Shift+G — очистить историю")
        notice = ""
        put_centered(stdscr, 1, "[%s]" % base_path, attr(4))
        put_centered(stdscr, 2, "Выберите папки", attr(2))

        visible = 0
        for idx, folder in enumerate(folders):
            row = idx // ITEMS_PER_ROW
            col = (idx % ITEMS_PER_ROW) * column
            if row + 4 >= h - 5:
                put_hint(stdscr, h - 6, "... и ещё %d папок" % (len(folders) - idx))
                break
            visible += 1
            info = infos[idx] if idx < len(infos) else None
            checkbox = "[x]" if selected_folders[idx] else "[ ]"
            full_path = os.path.join(base_path, folder)
            name = str(full_path) if opts["full_paths"] else folder
            label = checkbox + " " + folder_label(name, info, column)
            state = folder_state(full_path, info, history, pending, dismissed)
            line_attr = 0
            if state == "pending":
                line_attr = attr(8)       # оранжевый (жёлтый curses): не дожата
            elif state == "compressed":
                line_attr = attr(2)       # зелёный: папку уже сжимали
            if idx == current_index:
                line_attr |= curses.A_REVERSE
            put(stdscr, row + 4, col + 2, label, line_attr)

        if current_index >= visible:
            current_index = max(0, visible - 1)

        key = stdscr.getch()

        if key == curses.KEY_LEFT:
            if current_index % ITEMS_PER_ROW > 0:
                current_index -= 1
        elif key == curses.KEY_RIGHT:
            if current_index % ITEMS_PER_ROW < ITEMS_PER_ROW - 1 and current_index + 1 < len(folders):
                current_index += 1
        elif key == curses.KEY_UP:
            if current_index >= ITEMS_PER_ROW:
                current_index -= ITEMS_PER_ROW
        elif key == curses.KEY_DOWN:
            if current_index + ITEMS_PER_ROW < len(folders):
                current_index += ITEMS_PER_ROW
        elif key == curses.KEY_RESIZE:
            continue
        elif key in [ord(' '), ord('x'), ord('X'), ord('ч'), ord('Ч')]:
            selected_folders[current_index] = not selected_folders[current_index]
        elif key in [ord('a'), ord('A'), ord('ф'), ord('Ф')]:
            if any(selected_folders):
                selected_folders[:] = [False] * len(folders)
            else:
                selected_folders[:] = [True] * len(folders)
        elif key in [ord('g'), ord('G'), ord('п'), ord('П')]:
            had = clear_history()
            # зелёный по размеру на диске — тоже наша подсветка: без этого
            # Shift+G выглядел нерабочим, папки оставались зелёными
            dismiss_compressed(dict(
                (os.path.join(base_path, folder),
                 infos[idx] if idx < len(infos) else None)
                for idx, folder in enumerate(folders)))
            history = load_history()
            pending = load_pending()
            dismissed = load_dismissed()
            notice = ("История сжатых папок очищена: зелёная подсветка снята" if had
                      else "История и очередь были пусты — зелёная подсветка снята")
        elif key in QUIT_KEYS:
            raise SystemExit(0)
        elif key in BACK_KEYS:
            return None
        elif key in [ord('f'), ord('F'), ord('а'), ord('А')]:
            opts["force"] = not opts["force"]
        elif key in [ord('d'), ord('D'), ord('в'), ord('В')]:
            opts["hdd"] = not opts["hdd"]
        elif key in ENTER_KEYS:
            if any(selected_folders):
                break
            put(stdscr, h - 2, 0, "Ничего не выбрано. Нажмите любую клавишу, чтобы продолжить...")
            stdscr.refresh()
            stdscr.getch()

    return [folder for idx, folder in enumerate(folders) if selected_folders[idx]]


# --------------------------------------------------------------------------- ввод пути
QUOTES = "\"'`«»“”„"


def normalize_path(text):
    """Привести введённый путь к виду, который понимает os.listdir.

    Перетаскивание папки в консоль и «копировать как путь» дают путь в
    кавычках, поэтому обрамляющие кавычки любого вида отбрасываются.
    Существующая папка остаётся как есть (только нормализуется): в её имени
    могут быть «%OS%» или «~», и разворачивать их нельзя. Если папки по
    такому пути нет, раскрываются %ПЕРЕМЕННЫЕ% и ~.
    """
    s = "" if text is None else str(text).strip()
    while len(s) >= 2 and s[0] in QUOTES and s[-1] in QUOTES:
        s = s[1:-1].strip()
    s = s.strip(QUOTES + " \t")
    if s and os.path.isdir(s):
        return os.path.normpath(s)     # папка есть — %OS% и ~ в имени не разворачиваем
    s = os.path.expandvars(s)
    s = os.path.expanduser(s)
    if s and os.path.isdir(s):
        s = os.path.normpath(s)
    return s


def resolve_path(text):
    """Разобрать ввод пользователя: (путь, подпапки, жалоба).

    Жалоба пустая, если путь годится для меню; иначе это текст для строки
    подсказки, а путь остаётся тем, что ввёл пользователь — его можно править.
    """
    path = normalize_path(text)
    if not path:
        return path, [], "Путь пустой — введите, например, D:\\data"
    if os.path.exists(path) and not os.path.isdir(path):
        return path, [], "Это файл, а нужна папка: %s" % path
    if not os.path.isdir(path):
        return path, [], "Папка не найдена: %s" % path
    try:
        os.listdir(path)
    except OSError as exc:
        return path, [], "Нет доступа к папке %s (%s)" % (path, exc.strerror or exc)
    folders = get_folders(path)
    if not folders:
        return path, [], "В папке нет подпапок: %s" % path
    return path, folders, ""


def read_key(stdscr):
    """Одно нажатие: строка — печатаемый символ, int — служебная клавиша."""
    get_wch = getattr(stdscr, "get_wch", None)
    if get_wch is not None:
        try:
            return get_wch()
        except curses.error:
            return -1
    key = stdscr.getch()
    if 32 <= key < 256:
        return chr(key)          # без get_wch считаем байт за символ
    return key


def read_line(stdscr, y, x, prompt="", initial="", max_len=512):
    """Простой редактор строки: печать, ←/→, Home/End, Delete, Backspace.

    Возвращает введённый текст (Enter) или None (Esc). Курсор ходит по уже
    набранному тексту, поэтому опечатку в пути можно поправить, а не
    набирать путь заново; длинная строка прокручивается по горизонтали.
    """
    text = list(initial or "")
    pos = len(text)
    while True:
        _, w = stdscr.getmaxyx()
        avail = max(1, w - x - len(prompt) - 1)
        start = 0
        if pos > avail - 1:
            start = pos - (avail - 1)
        shown = "".join(text[start:start + avail])
        try:
            stdscr.move(y, 0)
            stdscr.clrtoeol()
        except curses.error:
            pass
        put(stdscr, y, x, prompt + shown)
        try:
            stdscr.move(y, min(w - 1, x + len(prompt) + (pos - start)))
            stdscr.refresh()
        except curses.error:
            pass

        key = read_key(stdscr)
        if isinstance(key, str) and len(key) == 1 and not key.isprintable():
            # get_wch() отдаёт управляющие клавиши строкой, а сравниваем мы
            # их с кодами curses — приводим строку к числу.
            key = ord(key)
        if key in ENTER_KEYS or key in ("\r", "\n"):
            return "".join(text)
        if key in (27, "\x1b"):
            return None
        if key == curses.KEY_RESIZE:
            continue
        if key == curses.KEY_LEFT:
            pos = max(0, pos - 1)
        elif key == curses.KEY_RIGHT:
            pos = min(len(text), pos + 1)
        elif key in (curses.KEY_HOME, 1):          # Home / Ctrl+A
            pos = 0
        elif key in (curses.KEY_END, 5):           # End / Ctrl+E
            pos = len(text)
        elif key in (curses.KEY_BACKSPACE, 8, 127):
            if pos:
                del text[pos - 1]
                pos -= 1
        elif key in (curses.KEY_DC, 4):            # Delete / Ctrl+D
            if pos < len(text):
                del text[pos]
        elif key == 21:                            # Ctrl+U — стереть слева
            del text[:pos]
            pos = 0
        elif key == 11:                            # Ctrl+K — стереть справа
            del text[pos:]
        elif isinstance(key, str) and key.isprintable() and len(text) < max_len:
            text.insert(pos, key)
            pos += 1


def ask_path(stdscr, initial=""):
    """Спросить путь к каталогу, пока он не окажется каталогом с подпапками."""
    set_cursor(True)
    text = normalize_path(initial) if initial else ""
    message = ""
    while True:
        stdscr.clear()
        put_hint(stdscr, 0, message or "Введите путь к папке:")
        put_hint(stdscr, 1, "Enter — принять")
        try:
            stdscr.move(2, 0)
            stdscr.clrtoeol()
            stdscr.refresh()
        except curses.error:
            pass

        typed = read_line(stdscr, 2, 0, "> ", text)
        if typed is None:
            raise SystemExit(0)
        text = typed
        path, folders, message = resolve_path(typed)
        if folders:
            set_cursor(False)
            return path, folders
# --------------------------------------------------------------------------- основной цикл
def main(stdscr, opts):
    init_colors()
    if opts["logo"]:
        display_logo(stdscr)
    stdscr.clear()

    if opts["path"]:
        path = normalize_path(opts["path"])
        folders = get_folders(path)
        if not folders:
            path, folders = ask_path(stdscr, initial=opts["path"])
    else:
        path, folders = ask_path(stdscr)

    selected_folders = [opts["select_all"]] * len(folders)
    infos = scan_infos(stdscr, path, folders, opts)

    while True:
        selected = menu(stdscr, folders, selected_folders, opts, path,
                        infos, load_history(), load_pending(), load_dismissed())
        if not selected:
            continue
        while True:
            result_key = display_selected(stdscr, selected, opts,
                                          dict(zip(folders, infos)))
            if result_key in BACK_KEYS:
                break
            if result_key in ENTER_KEYS:
                info_map = dict(zip(folders, infos))
                compact_folders(stdscr, path, selected, opts, info_map)
                set_cursor(False)
                infos = [info_map.get(folder) for folder in folders]  # пересчитал только сжатые
                break                     # итог прочитан: обратно к выбору папок


def cli(argv=None):
    """Точка входа: разобрать флаги, проверить права, запустить curses."""
    argv = sys.argv[1:] if argv is None else argv
    opts = parse_args(argv)

    if opts["help"]:
        print_help()
        return 0

    if not is_admin():
        relaunch_as_admin()
        return 0

    try:
        os.remove(LOG_PATH)
    except OSError:
        pass

    curses.wrapper(main, opts)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
