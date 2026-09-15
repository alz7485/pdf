import sys
import os
import json
import subprocess
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from PySide6.QtGui import QImage


def get_app_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()


def normalize_path(path):
    try:
        return str(Path(path).resolve())
    except Exception:
        return str(path)


def get_temp_dir():
    temp_dir = APP_DIR / 'temp'
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


def is_temp_pdf_path(path):
    if not path:
        return False
    try:
        return Path(normalize_path(path)).parent == get_temp_dir().resolve()
    except Exception:
        return False


def make_temp_pdf_path(source_path):
    source = Path(source_path)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
    candidate = get_temp_dir() / f'{source.stem}_temp_{stamp}.pdf'
    n = 1
    while candidate.exists():
        candidate = get_temp_dir() / f'{source.stem}_temp_{stamp}_{n}.pdf'
        n += 1
    return candidate


def open_temp_folder():
    temp_dir = get_temp_dir()
    try:
        os.startfile(str(temp_dir))
    except AttributeError:
        subprocess.Popen(['explorer', str(temp_dir)])


def safe_json_load(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def safe_json_save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def fitz_pixmap_to_qimage(pix):
    if pix.n == 1:
        fmt = QImage.Format_Grayscale8
    elif pix.alpha:
        fmt = QImage.Format_RGBA8888
    else:
        fmt = QImage.Format_RGB888
    return QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()


def render_page_image(pdf_path, page_index, dpi=120, rotation=0,
                      include_annotations=True):
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_index)
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        if rotation:
            matrix = matrix.prerotate(rotation)
        pix = page.get_pixmap(
            matrix=matrix,
            alpha=False,
            annots=bool(include_annotations),
        )
        return fitz_pixmap_to_qimage(pix)
    finally:
        doc.close()


def paper_class_from_rect(rect):
    short_side = min(rect.width, rect.height)
    long_side = max(rect.width, rect.height)
    standards = {
        'A4': (595.28, 841.89),
        'A3': (841.89, 1190.55),
        'A2': (1190.55, 1683.78),
        'A1': (1683.78, 2383.94),
        'A0': (2383.94, 3370.39),
    }
    best, best_error = None, 999.0
    for name, (s, l) in standards.items():
        err = max(abs(short_side - s) / s, abs(long_side - l) / l)
        if err < best_error:
            best_error = err
            best = name
    if best_error <= 0.05:
        return best
    a3s, a3l = standards['A3']
    if short_side >= a3s * 0.95 or long_side >= a3l * 0.95:
        return 'A3+'
    return 'OTHER'


def parse_page_spec(text, page_count):
    text = (text or '').replace(' ', '').replace('、', ',')
    if not text:
        return []
    result = set()
    for part in text.split(','):
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            if not a.isdigit() or not b.isdigit():
                raise ValueError(f'ページ指定が不正です: {part}')
            start, end = int(a), int(b)
            if start > end:
                start, end = end, start
            for n in range(start, end + 1):
                if 1 <= n <= page_count:
                    result.add(n - 1)
        else:
            if not part.isdigit():
                raise ValueError(f'ページ指定が不正です: {part}')
            n = int(part)
            if 1 <= n <= page_count:
                result.add(n - 1)
    return sorted(result)


@dataclass
class PageEntry:
    source_path: str
    source_index: int
    source_rotation: int
    extra_rotation: int = 0
    favorite: bool = False
    annotations: list = field(default_factory=list)
    inserted: bool = False
    annotations_loaded: bool = False

    @property
    def final_rotation(self):
        return (self.source_rotation + self.extra_rotation) % 360



def reveal_file_in_explorer(path):
    """
    Windows Explorer でPDFの場所を表示する。

    Returns:
        'selected'       : ファイルが存在し、Explorerで選択表示した
        'file_missing'   : ファイルは無いが親フォルダを開いた
        'folder_missing' : 親フォルダも存在しない
        'error'          : Explorer起動に失敗した
    """
    path = normalize_path(path)
    target = Path(path)
    parent = target.parent

    if target.is_file():
        try:
            if os.name == 'nt':
                # Explorer の /select は「/select,」と対象パスを別引数にする。
                # 1文字列に連結すると、空白・日本語・UNCパス等で Explorer が
                # 対象を正しく解釈せず「ドキュメント」等へ飛ぶことがある。
                target_str = os.path.normpath(str(target))
                subprocess.Popen(['explorer.exe', '/select,', target_str])
            else:
                subprocess.Popen(['xdg-open', str(parent)])
            return 'selected'
        except Exception:
            return 'error'

    if parent.is_dir():
        try:
            if os.name == 'nt':
                os.startfile(str(parent))
            else:
                subprocess.Popen(['xdg-open', str(parent)])
            return 'file_missing'
        except Exception:
            return 'error'

    return 'folder_missing'
