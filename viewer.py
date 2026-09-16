import sys
import os
import re
import json
import tempfile
from pathlib import Path
from datetime import date
from collections import OrderedDict
import gc

import fitz
from PySide6.QtCore import Qt, QSize, QRectF, Signal, QThread, QEvent, QTimer, QMimeData
from PySide6.QtGui import QImage, QPixmap, QPainter, QTransform, QColor, QPen, QDrag, QFont, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QToolButton, QComboBox, QHBoxLayout, QVBoxLayout, QGridLayout,
    QSplitter, QFileDialog, QMessageBox, QSpinBox, QCheckBox, QDialog,
    QDialogButtonBox, QGroupBox, QLineEdit, QButtonGroup, QScrollArea,
    QAbstractItemView, QListView, QInputDialog, QGraphicsView, QGraphicsScene,
    QFormLayout, QFrame, QTextEdit, QPlainTextEdit, QDoubleSpinBox,
    QTabWidget, QTextBrowser, QProgressDialog, QStackedWidget, QSizePolicy,
    QSystemTrayIcon, QMenu, QStyle
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# editor / print_dialog は起動時に読み込まない。
# 実際に編集・印刷を開いた時だけ import してコールドスタートを軽くする。
from pdf_core import (
    APP_DIR, PageEntry, normalize_path, get_temp_dir, is_temp_pdf_path, make_temp_pdf_path,
    open_temp_folder, safe_json_load, safe_json_save, render_page_image,
    paper_class_from_rect, parse_page_spec,
)
from ui_common import (
    APP_STYLESHEET, BUTTON_HEIGHT, BUTTON_WIDTH, framed_label_style,
    make_tool_button, style_push_button, reveal_pdf_location,
)

SETTINGS_FILE = APP_DIR / 'settings.json'
FAVORITES_FILE = APP_DIR / 'favorites.json'
LIST_DIR = APP_DIR / 'preset'
LIST_DIR.mkdir(parents=True, exist_ok=True)
APP_NAME = 'PDFビューア'
SINGLE_INSTANCE_SERVER = 'FastPDF_SingleInstance_v1'


def shift_pressed_at_launch():
    """Shiftを押しながら起動した時だけ、別プロセスのWindowを許可する。"""
    if os.name == 'nt':
        try:
            import ctypes
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
        except Exception:
            pass
    try:
        return bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
    except Exception:
        return False


def command_line_pdf_paths():
    return [
        normalize_path(a)
        for a in sys.argv[1:]
        if str(a).lower().endswith('.pdf') and Path(a).is_file()
    ]


def send_paths_to_existing_instance(paths, timeout_ms=700):
    """既存FastPDFへPDFパスを送信。送信できた時だけTrue。"""
    socket = QLocalSocket()
    socket.connectToServer(SINGLE_INSTANCE_SERVER)
    if not socket.waitForConnected(timeout_ms):
        return False
    try:
        payload = json.dumps(
            {'paths': [normalize_path(p) for p in paths]},
            ensure_ascii=False
        ).encode('utf-8') + b'\n'
        socket.write(payload)
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
    finally:
        socket.disconnectFromServer()
    return True


class SingleInstanceServer(QLocalServer):
    """関連付け起動されたPDFを、先に開いているMainWindowへ渡すローカルIPC。"""

    def __init__(self, window=None, parent=None):
        super().__init__(parent)
        self.window = window
        self._clients = set()
        self._buffers = {}
        self._queued_paths = []
        self.newConnection.connect(self._accept_connections)

    def set_window(self, window):
        self.window = window
        if self.window is not None and self._queued_paths:
            queued = list(self._queued_paths)
            self._queued_paths.clear()
            QTimer.singleShot(0, lambda p=queued: self.window.open_external_pdf_paths(p))

    def _accept_connections(self):
        while self.hasPendingConnections():
            sock = self.nextPendingConnection()
            if sock is None:
                continue
            self._clients.add(sock)
            self._buffers[sock] = b''
            sock.readyRead.connect(lambda s=sock: self._read_client(s))
            sock.disconnected.connect(lambda s=sock: self._drop_client(s))
            QTimer.singleShot(0, lambda s=sock: self._read_client(s))

    def _drop_client(self, sock):
        # 最後に改行なしデータが残っていても可能な範囲で処理する。
        tail = self._buffers.pop(sock, b'')
        if tail.strip():
            self._process_payload_bytes(tail)
        self._clients.discard(sock)
        try:
            sock.deleteLater()
        except Exception:
            pass

    def _deliver_paths(self, paths):
        if not paths:
            return
        if self.window is None:
            for path in paths:
                if path not in self._queued_paths:
                    self._queued_paths.append(path)
            return
        self.window.open_external_pdf_paths(paths)

    def _process_payload_bytes(self, payload):
        try:
            data = json.loads(payload.decode('utf-8', errors='strict'))
            paths = data.get('paths', []) if isinstance(data, dict) else []
            paths = [
                normalize_path(p) for p in paths
                if p and str(p).lower().endswith('.pdf') and Path(p).is_file()
            ]
            self._deliver_paths(paths)
        except Exception:
            pass

    def _read_client(self, sock):
        if sock is None or sock.bytesAvailable() <= 0:
            return
        buf = self._buffers.get(sock, b'') + bytes(sock.readAll())
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            if line.strip():
                self._process_payload_bytes(line)
        self._buffers[sock] = buf


class SettingsManager:
    DEFAULTS = {
        'resolution_mode': 'auto',
        'zoom_percent': 100,
        'fit_on_open': True,
        'center_page': True,
        'cache_pages': 7,
        'preload_pages': 2,
        'background_preload': True,
    }

    def __init__(self):
        self.data = dict(self.DEFAULTS)
        loaded = safe_json_load(SETTINGS_FILE, {})
        if isinstance(loaded, dict):
            self.data.update(loaded)

    def save(self):
        safe_json_save(SETTINGS_FILE, self.data)

    def dpi_for_pdf(self, pdf_path, page_count):
        mode = self.data.get('resolution_mode', 'auto')
        if mode == 'low':
            return 90
        if mode == 'standard':
            return 150
        if mode == 'high':
            return 220
        try:
            size_mb = Path(pdf_path).stat().st_size / (1024 * 1024)
        except Exception:
            size_mb = 0
        if size_mb >= 100 or page_count >= 800:
            return 80
        if size_mb >= 50 or page_count >= 400:
            return 95
        if size_mb >= 20 or page_count >= 200:
            return 120
        return 150


class FavoritesManager:
    def __init__(self):
        self.data = {}
        self.dirty = False
        self.reload()

    def reload(self):
        data = safe_json_load(FAVORITES_FILE, {})
        self.data = data if isinstance(data, dict) else {}
        self.dirty = False
        return True

    def save(self):
        ok = safe_json_save(FAVORITES_FILE, self.data)
        if ok:
            self.dirty = False
        return ok

    def pages(self, pdf_path):
        key = normalize_path(pdf_path)
        entry = self.data.get(key, {})
        values = entry.get('favorites', []) if isinstance(entry, dict) else []
        out = set()
        for v in values:
            try:
                n = int(v)
                if n >= 0:
                    out.add(n)
            except Exception:
                pass
        return out

    def set_pages(self, pdf_path, pages):
        key = normalize_path(pdf_path)
        self.data[key] = {'favorites': sorted(set(int(x) for x in pages if int(x) >= 0))}
        self.dirty = True

    def toggle(self, pdf_path, page_index):
        pages = self.pages(pdf_path)
        if page_index in pages:
            pages.remove(page_index)
            state = False
        else:
            pages.add(page_index)
            state = True
        self.set_pages(pdf_path, pages)
        return state

    def clear(self, pdf_path):
        self.set_pages(pdf_path, set())


class PresetManager:
    EMPTY_NAME = '新しいリスト'

    def __init__(self):
        LIST_DIR.mkdir(parents=True, exist_ok=True)

    def names(self):
        return sorted((p.stem for p in LIST_DIR.glob('*.json')), key=str.lower)

    def path_for(self, name):
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', name).strip()
        return LIST_DIR / f'{safe_name}.json'

    def load(self, name):
        if not name or name == self.EMPTY_NAME:
            return []
        data = safe_json_load(self.path_for(name), {})
        files = data.get('files', []) if isinstance(data, dict) else []
        return [normalize_path(p) for p in files if isinstance(p, str)]

    def save(self, name, files):
        return safe_json_save(self.path_for(name), {
            'name': name,
            'files': [normalize_path(p) for p in files]
        })

    def delete(self, name):
        if not name or name == self.EMPTY_NAME:
            return False
        try:
            p = self.path_for(name)
            if p.exists():
                p.unlink()
            return True
        except Exception:
            return False


class RenderWorker(QThread):
    rendered = Signal(str, int, int, int, object)
    failed = Signal(str, int, str)

    def __init__(self, pdf_path, page_index, dpi, generation):
        super().__init__()
        self.pdf_path = pdf_path
        self.page_index = page_index
        self.dpi = dpi
        self.generation = generation

    def run(self):
        try:
            if self.isInterruptionRequested():
                return
            image = render_page_image(self.pdf_path, self.page_index, self.dpi)
            # トレイ待機へ移行した場合、完成した大きな画像をUI側へ戻さない。
            if self.isInterruptionRequested():
                return
            self.rendered.emit(self.pdf_path, self.page_index, self.dpi, self.generation, image)
        except Exception as e:
            if not self.isInterruptionRequested():
                self.failed.emit(self.pdf_path, self.page_index, str(e))


class PDFListWidget(QListWidget):
    pdfFilesDropped = Signal(list)
    orderChanged = Signal()
    revealRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            files = [u.toLocalFile() for u in event.mimeData().urls()]
            if any(Path(f).is_dir() or str(f).lower().endswith('.pdf') for f in files):
                event.acceptProposedAction()
                return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
            valid = [p for p in paths if Path(p).is_dir() or p.lower().endswith('.pdf')]
            if valid:
                self.pdfFilesDropped.emit(valid)
                event.acceptProposedAction()
                return
        super().dropEvent(event)
        QTimer.singleShot(0, self.orderChanged.emit)

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.RightButton:
            # 右クリックでは複数選択/現在選択を変更しない。
            event.accept()
            return
        if item is None:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                path = item.data(Qt.UserRole)
                if path:
                    self.revealRequested.emit(normalize_path(path))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            window = self.window()
            if hasattr(window, 'remove_selected_pdf'):
                window.remove_selected_pdf()
                return
        super().keyPressEvent(event)


class SettingsDialog(QDialog):
    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings = settings_manager
        self.setWindowTitle('設定')
        self.setMinimumWidth(420)
        root = QVBoxLayout(self)

        display_group = QGroupBox('表示')
        form = QFormLayout(display_group)
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem('自動', 'auto')
        self.resolution_combo.addItem('低', 'low')
        self.resolution_combo.addItem('標準', 'standard')
        self.resolution_combo.addItem('高', 'high')
        idx = self.resolution_combo.findData(self.settings.data.get('resolution_mode', 'auto'))
        self.resolution_combo.setCurrentIndex(max(0, idx))
        form.addRow('表示解像度', self.resolution_combo)

        self.zoom_spin = QSpinBox()
        self.zoom_spin.setRange(25, 400)
        self.zoom_spin.setSuffix(' %')
        self.zoom_spin.setValue(int(self.settings.data.get('zoom_percent', 100)))
        form.addRow('ズーム倍率', self.zoom_spin)

        self.fit_check = QCheckBox('起動時にフィット表示')
        self.fit_check.setChecked(bool(self.settings.data.get('fit_on_open', True)))
        form.addRow('', self.fit_check)
        self.center_check = QCheckBox('ページ中央表示')
        self.center_check.setChecked(bool(self.settings.data.get('center_page', True)))
        form.addRow('', self.center_check)

        speed_group = QGroupBox('高速化')
        speed_form = QFormLayout(speed_group)
        self.cache_spin = QSpinBox()
        self.cache_spin.setRange(1, 50)
        self.cache_spin.setValue(int(self.settings.data.get('cache_pages', 7)))
        speed_form.addRow('キャッシュページ数', self.cache_spin)
        self.preload_spin = QSpinBox()
        self.preload_spin.setRange(0, 10)
        self.preload_spin.setValue(int(self.settings.data.get('preload_pages', 2)))
        speed_form.addRow('先読みページ数', self.preload_spin)
        self.background_check = QCheckBox('バックグラウンド先読み')
        self.background_check.setChecked(bool(self.settings.data.get('background_preload', True)))
        speed_form.addRow('', self.background_check)

        root.addWidget(display_group)
        root.addWidget(speed_group)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self):
        self.settings.data.update({
            'resolution_mode': self.resolution_combo.currentData(),
            'zoom_percent': self.zoom_spin.value(),
            'fit_on_open': self.fit_check.isChecked(),
            'center_page': self.center_check.isChecked(),
            'cache_pages': self.cache_spin.value(),
            'preload_pages': self.preload_spin.value(),
            'background_preload': self.background_check.isChecked(),
        })
        self.settings.save()
        super().accept()





class HelpDialog(QDialog):
    """4ページ構成の操作説明ダイアログ。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('操作説明')
        self.resize(820, 680)

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)

        pages = [
            ('🏠 メインUI', self._main_help()),
            ('⚙️ 設定', self._settings_help()),
            ('✏️ 編集', self._manager_help()),
            ('🖨️ 印刷', self._print_help()),
        ]

        for title, html in pages:
            browser = QTextBrowser()
            browser.setOpenExternalLinks(False)
            browser.setHtml(html)
            tabs.addTab(browser, title)

        close_btn = QPushButton('閉じる')
        close_btn.setToolTip('操作説明を閉じます')
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn, 0, Qt.AlignRight)

    @staticmethod
    def _base(title, body):
        return f"""
        <html><head><style>
        body {{ font-family: "Yu Gothic UI", "Meiryo"; font-size: 14px; line-height: 1.55; }}
        h1 {{ font-size: 22px; margin-bottom: 12px; }}
        h2 {{ font-size: 17px; margin-top: 22px; border-bottom: 1px solid #999; padding-bottom: 5px; }}
        table {{ border-collapse: collapse; width: 100%; margin: 8px 0 16px 0; }}
        th, td {{ border: 1px solid #aaa; padding: 7px 9px; vertical-align: top; }}
        th {{ font-weight: bold; }}
        .key {{ font-family: Consolas, monospace; font-weight: bold; }}
        .note {{ padding: 9px; border: 1px solid #aaa; border-radius: 5px; }}
        </style></head><body>
        <h1>{title}</h1>{body}</body></html>
        """

    def _main_help(self):
        return self._base('🏠 メインUI', """
        <h2>基本操作</h2>
        <table>
          <tr><th>操作</th><th>動作</th></tr>
          <tr><td class="key">↑ / ↓</td><td>PDFリストの前 / 次へ移動します。複数選択中は上端の選択行を基準に1つ上 / 下のPDFへ移動し、単一選択へ戻ります。</td></tr>
          <tr><td class="key">← / →</td><td>前 / 次のページへ移動</td></tr>
          <tr><td>マウスホイール</td><td>ページ送り</td></tr>
          <tr><td class="key">Ctrl + ホイール</td><td>拡大 / 縮小</td></tr>
          <tr><td>中ボタンドラッグ</td><td>拡大したページをパン移動</td></tr>
          <tr><td class="key">Space</td><td>現在ページのお気に入り ⭐ を切り替え。変更は即時保存されます。</td></tr>
          <tr><td class="key">Delete</td><td>PDFリストで選択中のPDFをリストから削除します。PDFファイル本体は削除しません。</td></tr>
        </table>

        <h2>PDFリスト</h2>
        <table>
          <tr><td>PDFをドラッグ＆ドロップ</td><td>PDFリストへ追加</td></tr>
          <tr><td>フォルダをドラッグ＆ドロップ</td><td>フォルダ内のPDFを追加。サブフォルダにPDFがある場合は含めるか確認します。</td></tr>
          <tr><td>複数選択</td><td>複数PDFを選ぶとプレビューは表示せず、🖨️からまとめて印刷できます。</td></tr>
          <tr><td>✏️</td><td>現在のPDFリストを編集UIへ渡します。選択PDFがあれば上端の選択PDFから編集を開始します。</td></tr>
          <tr><td>➕ / ➖</td><td>PDF追加 / 選択PDFをリストから削除</td></tr>
          <tr><td>右クリック</td><td>格納フォルダを開き、存在するPDFはExplorerで選択表示します。ファイルやフォルダが無い場合は案内を表示します。</td></tr>
        </table>

        <h2>上部ボタン</h2>
        <table>
          <tr><td>🔼 / 🔽</td><td>PDFリストを表示 / 非表示</td></tr>
          <tr><td>プリセット</td><td>PDFリストの組み合わせを切り替えます。「新しいリスト」は保存されない一時リストです。</td></tr>
          <tr><td>💾 / 🗑️（プリセット側）</td><td>現在のPDFリストをプリセット保存 / 選択中プリセットを削除</td></tr>
          <tr><td>⚙️</td><td>表示・キャッシュなどの設定を開く</td></tr>
          <tr><td>🖨️</td><td>印刷設定と印刷プレビューを開く</td></tr>
          <tr><td>📄</td><td>ページ全体を画面にフィット</td></tr>
          <tr><td>➖ / ➕</td><td>表示倍率を10%ずつ変更</td></tr>
          <tr><td>100%</td><td>現在の表示倍率</td></tr>
          <tr><td>↶ / ↷</td><td>現在ページの表示だけを左 / 右へ90°回転します。ビューア側ではPDF本体を変更しません。</td></tr>
          <tr><td>❔</td><td>この操作説明を開きます。</td></tr>
        </table>

        <h2>お気に入り</h2>
        <table>
          <tr><td>⭐</td><td>現在ページのお気に入り状態</td></tr>
          <tr><td>🌟</td><td>お気に入り移動モード。ONではページ移動時にお気に入りページだけをたどります。</td></tr>
          <tr><td>🆑</td><td>現在PDFのお気に入りをすべて解除</td></tr>
        </table>

        <div class="note">ビューアは非破壊です。PDF本体の削除・挿入・並べ替え・回転・朱書き保存は ✏️ 編集UIで行います。</div>
        """)

    def _settings_help(self):
        return self._base('⚙️ 設定', """
        <h2>表示</h2>
        <table>
          <tr><td>表示解像度</td><td>PDF表示時の解像度を設定します。大きな図面PDFでは低めにすると表示が軽くなります。</td></tr>
          <tr><td>ズーム関連</td><td>通常表示時の倍率や起動時のフィット表示を調整します。</td></tr>
        </table>

        <h2>キャッシュ / 高速化</h2>
        <table>
          <tr><td>キャッシュ</td><td>描画済みページを再利用してページ切替を高速化します。</td></tr>
          <tr><td>先読み</td><td>現在ページの前後を先に描画して、ページ送り時の待ち時間を減らします。</td></tr>
        </table>

        <div class="note">表示設定は閲覧速度に関係しますが、PDF本体の内容や印刷用PDFデータは変更しません。</div>
        """)

    def _manager_help(self):
        return self._base('✏️ 編集', """
        <h2>編集UIの考え方</h2>
        <p>編集UIでは、ページ構成の変更と朱書き編集を行います。ビューアとは分離されており、編集内容は保存するまで元PDFへ確定しません。</p>

        <h2>PDFリスト / 一時保存</h2>
        <table>
          <tr><td>左のPDFリスト</td><td>ビューアから渡されたPDF一覧。PDF名をクリックすると編集対象を切り替えます。</td></tr>
          <tr><td>⠿</td><td>PDF全体をサムネイルへドラッグして、全ページを挿入できます。</td></tr>
          <tr><td>💾</td><td>現在の編集内容を元PDFへ保存</td></tr>
          <tr><td>📥</td><td>現在の編集内容を別名保存</td></tr>
          <tr><td>🧪</td><td>一時PDFとして保存します。通常PDFからは新しい一時PDFを作成し、一時PDF編集中は同じ一時PDFを更新します。</td></tr>
          <tr><td>📂</td><td>一時保存フォルダを開きます。</td></tr>
        </table>

        <h2>表示モード</h2>
        <table>
          <tr><td>📄</td><td>単一ページ編集モード。朱書きの作成・選択・移動・サイズ変更・回転を行います。</td></tr>
          <tr><td>🗂️</td><td>サムネイルモード。ページの複数選択、並べ替え、挿入、削除、回転を行います。</td></tr>
          <tr><td>サムネイルをダブルクリック</td><td>そのページを単一ページモードで開きます。</td></tr>
          <tr><td>単一ページで図形以外をダブルクリック</td><td>サムネイルモードへ戻ります。</td></tr>
        </table>

        <h2>ページ編集</h2>
        <table>
          <tr><td>➕</td><td>別PDFからページを挿入します。挿入後は追加したページ群が選択され、サムネイル側へフォーカスが移るため、そのままDelete等を使用できます。</td></tr>
          <tr><td>🗑️ / Delete</td><td>選択ページを削除。削除前に確認します。</td></tr>
          <tr><td>↶ / ↷</td><td>選択ページを左 / 右へ90°回転</td></tr>
          <tr><td>🔄</td><td>選択ページを180°回転</td></tr>
          <tr><td>⭐</td><td>選択ページのお気に入りを切り替え</td></tr>
          <tr><td>🆑</td><td>現在PDFのお気に入りをすべて解除</td></tr>
          <tr><td>Ctrl + ホイール</td><td>サムネイルの大きさを60%～180%で変更</td></tr>
          <tr><td>ドラッグ＆ドロップ</td><td>サムネイルの並べ替え、外部PDFや左PDFリストからのページ挿入に使用できます。</td></tr>
        </table>

        <h2>朱書きツール</h2>
        <table>
          <tr><td>➚</td><td>選択ツール</td></tr>
          <tr><td>T□</td><td>白背景＋枠付きテキスト</td></tr>
          <tr><td>T</td><td>背景なしテキスト</td></tr>
          <tr><td>○</td><td>楕円</td></tr>
          <tr><td>□</td><td>四角</td></tr>
          <tr><td>─</td><td>線</td></tr>
          <tr><td>→</td><td>矢印</td></tr>
          <tr><td>色見本ボタン</td><td>クリックするとカラーパレットを開きます。未選択時は次に作る朱書きの色を変更し、オブジェクト選択中は選択中の朱書きへ色を一括適用します。ボタン内の色見本が現在色です。</td></tr>
          <tr><td>■</td><td>四角・楕円の内部を白塗り / 白塗り解除。選択中の図形があればその図形を切り替え、未選択なら次に作る四角・楕円の既定値を切り替えます。</td></tr>
          <tr><td>❌</td><td>選択中の朱書きを削除</td></tr>
          <tr><td>↶U / ↷R</td><td>朱書き操作のUndo / Redo</td></tr>
        </table>

        <h2>朱書きの選択と編集</h2>
        <table>
          <tr><td>図形作成直後</td><td>作成した図形が自動選択され、そのまま移動・サイズ変更・回転できます。作成ツール自体は維持されます。</td></tr>
          <tr><td>空白をクリック</td><td>選択解除。元の作成ツールが再び有効になります。</td></tr>
          <tr><td>Ctrl + クリック</td><td>現在の作成ツールに関係なく既存朱書きを追加選択 / 選択解除できます。</td></tr>
          <tr><td>複数選択してドラッグ</td><td>選択中の朱書きをまとめて移動します。</td></tr>
          <tr><td>単一選択</td><td>サイズ変更ハンドルと回転ハンドルを表示します。回転は5°単位でスナップします。</td></tr>
          <tr><td>テキストをダブルクリック</td><td>テキスト内容を編集します。</td></tr>
        </table>

        <h2>既存PDF注釈</h2>
        <p>ビューア・サムネイル・印刷ではPDF注釈を解析せず、通常のPDF表示として描画します。単一ページ編集モードで現在ページを開いた時だけ、対応する標準PDF注釈（四角・円・線・FreeText等）を編集可能な朱書きオブジェクトへ遅延変換します。</p>
        <p>対応外の注釈は背景上では見えますが、FastPDFの編集オブジェクトには変換されません。</p>

        <div class="note">大量ページPDFでも全ページの注釈を一括解析しないため、編集しているページだけ処理する設計です。</div>
        """)

    def _print_help(self):
        return self._base('🖨️ 印刷', """
        <h2>印刷サイズ</h2>
        <table>
          <tr><td>実サイズ</td><td>A4原稿はA4、その他はA3を基本として出力</td></tr>
          <tr><td>A4</td><td>対象ページをA4へ収めて印刷</td></tr>
          <tr><td>A3</td><td>対象ページをA3へ収めて印刷</td></tr>
        </table>

        <h2>サイズ対象</h2>
        <table>
          <tr><td>指定なし</td><td>ページサイズで絞り込まない</td></tr>
          <tr><td>A4のみ</td><td>A4判定ページのみ</td></tr>
          <tr><td>A3のみ</td><td>A3以上を対象。A4は除外</td></tr>
          <tr><td>A2,A1のみ</td><td>A2またはA1判定ページのみ</td></tr>
        </table>

        <h2>印刷対象</h2>
        <table>
          <tr><td>全て</td><td>PDF全体</td></tr>
          <tr><td>現在ページ</td><td>現在表示中のページ。複数PDF印刷では各PDFで最後に表示していたページが対象です。</td></tr>
          <tr><td>⭐</td><td>⭐を付けたページ</td></tr>
          <tr><td>ページ指定</td><td>例：1-5,8,10-20。複数PDF時は同じ指定を各PDFへ適用します。ページ指定以外では入力文字をグレー表示します。</td></tr>
        </table>

        <h2>印刷色</h2>
        <table>
          <tr><td>自動</td><td>デフォルト。元PDFページを軽量判定し、ページごとにカラー / モノクロを自動決定します。プレビュー下部に「自動→カラー」「自動→モノクロ」を表示します。</td></tr>
          <tr><td>カラー</td><td>対象ページをすべてカラーで描画・印刷します。</td></tr>
          <tr><td>モノクロ</td><td>対象ページをすべてグレースケール化して印刷します。</td></tr>
        </table>

        <h2>プレビュー</h2>
        <p>上部中央の 📄 / ➖ / ➕ / 倍率表示でプレビュー倍率を操作できます。🔎は現在のプレビューページだけを一時的に高解像度で再描画します。ホイールでページ送り、Ctrl+ホイールで拡大縮小、中ボタンドラッグでパン移動できます。</p>
        <p>下部には元PDF内のページ位置、元サイズ→出力サイズ、ファイル名、印刷対象内のページ位置を表示します。</p>

        <h2>余白</h2>
        <p>上・下・左・右をmm単位で個別に指定できます。「デフォルトに戻す」で4方向を5 mmへ戻せます。PDFページは指定した余白とヘッダー / フッター領域を除いた範囲へ収まるように縮小して印刷します。</p>

        <h2>ヘッダー / フッター</h2>
        <p>ヘッダー / フッター欄へ自由入力できます。入力したい欄をクリックしてから、共通ボタンの「ファイル名」「ページ」「総ページ」「日付」を押すとカーソル位置へ挿入します。</p>
        <p>同じ情報がその入力欄にすでに入っている状態でもう一度同じボタンを押すと、その情報だけを削除します。「削除」は現在コマンド受付中のヘッダー / フッター入力欄を空にします。ボタン名や背景色は使用状態によって変化しません。</p>
        <p>ヘッダー / フッターは文字が入っている場合だけ印刷し、共通のフォントサイズを6～24 ptで指定できます。配置は左 / 中央 / 右から選べます。</p>

        <h2>印刷解像度</h2>
        <p>100 / 150 / 200 / 300 DPIから選択できます。低いDPIほど高速、高いDPIほど高精細です。</p>

        <h2>印刷 / キャンセル</h2>
        <p>印刷設定画面の右下に、大きな「キャンセル」と「🖨️ 印刷」ボタンを配置しています。「🖨️ 印刷」が既定ボタンです。</p>

        <h2>複数PDF</h2>
        <p>ビューアのPDFリストで複数選択した状態から🖨️を開くと、選択PDFをまとめて印刷できます。印刷条件は各PDFに対して個別に適用されます。</p>

        <div class="note">PDF注釈は印刷時に編集オブジェクトへ変換せず、PDFの通常レンダリング結果としてそのまま印刷します。</div>
        """)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = SettingsManager()
        self.favorites = FavoritesManager()
        self.presets = PresetManager()
        self.pdf_paths = []
        self.current_path = None
        self.current_doc = None
        self.current_page = 0
        # PDFごとの現在ページをセッション中だけ記憶する
        self.page_positions = {}
        # 未保存のページ回転。表示へ即時反映し、💾/📥保存時にPDFへ確定する。
        self.page_view_rotations = {}
        self.current_dpi = 150
        self.zoom_percent = int(self.settings.data.get('zoom_percent', 100))
        self.fit_mode = bool(self.settings.data.get('fit_on_open', True))
        self.cache = OrderedDict()
        self.pending_keys = set()
        self.workers = []
        self.generation = 0
        self.favorite_mode = False
        self.current_preset = PresetManager.EMPTY_NAME
        self._preset_combo_updating = False
        self.multi_selection_active = False
        # PDFごとの印刷設定をアプリ実行中だけ保持。JSON等には保存しない。
        self.print_session_settings = {}
        # 現在ページだけの一時高解像度表示指定。
        self.page_detail_dpi = {}
        # 編集モード表示中のダイアログ。
        # 関連付け等から外部PDFが届いた場合は、ビューアへ戻さず
        # この編集画面のPDFリストへ追加する。
        self._active_editor_dialog = None

        self._panning = False
        self._pan_start_pos = None
        self._pan_start_h = 0
        self._pan_start_v = 0

        # × は終了ではなく「軽量トレイ待機」。
        # 完全終了はトレイメニューの「終了」だけで行う。
        self._allow_real_close = False
        self._tray_waiting = False

        self.setWindowTitle(APP_NAME)
        self.resize(1450, 920)
        self.setAcceptDrops(True)
        self.build_ui()
        self.refresh_preset_combo()
        self.setup_tray_icon()
        QApplication.instance().installEventFilter(self)
        self.statusBar().showMessage('↑↓ PDF切替 / ←→・ホイール ページ移動 / Ctrl+ホイール 拡大縮小 / 中ボタンドラッグ パン / Space ⭐')

    def setup_tray_icon(self):
        """FastPDFを軽量待機させるためのトレイアイコンを作る。"""
        self.tray_icon = QSystemTrayIcon(self)
        icon = self.windowIcon()
        if icon.isNull():
            icon = QApplication.style().standardIcon(QStyle.SP_FileIcon)
        self.tray_icon.setIcon(icon)
        self.tray_icon.setToolTip('FastPDF')

        menu = QMenu()
        show_action = QAction('表示', self)
        show_action.triggered.connect(self.restore_from_tray)
        exit_action = QAction('終了', self)
        exit_action.triggered.connect(self.quit_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.restore_from_tray()

    def restore_from_tray(self):
        """トレイから空のFastPDFを再表示する。"""
        self._tray_waiting = False
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _release_session_for_tray(self):
        """×時にPDF由来の重いメモリとセッション情報だけを解放する。"""
        self.generation += 1

        # 実行中レンダーは中断要求。完了後の画像はRenderWorker側でUIへ返さない。
        for worker in list(self.workers):
            try:
                worker.requestInterruption()
            except Exception:
                pass

        self.close_current_doc()

        # QImage / QPixmap とPDF単位の一時情報をすべて破棄。
        self.cache.clear()
        self.pending_keys.clear()
        self.page_detail_dpi.clear()
        self.page_view_rotations.clear()
        self.page_positions.clear()
        self.print_session_settings.clear()

        self.current_path = None
        self.current_page = 0
        self.multi_selection_active = False
        self.favorite_mode = False

        # ×で空にするのは「現在セッションのリスト」だけ。
        # 名前付きプリセットJSONへ空リストを自動保存しない。
        self.pdf_paths.clear()
        self.pdf_list.blockSignals(True)
        try:
            self.pdf_list.clear()
        finally:
            self.pdf_list.blockSignals(False)
        self.current_preset = PresetManager.EMPTY_NAME
        self.refresh_preset_combo(PresetManager.EMPTY_NAME)

        # 表示中Pixmapも明示的に外してから空表示へ戻す。
        self.page_label.setPixmap(QPixmap())
        self.clear_view()
        self.update_status()
        self.page_manager_btn.setEnabled(False)
        self.statusBar().showMessage('トレイで待機中')

        # Python側で参照が切れた大きな画像を早めに回収する。
        gc.collect()

    def enter_tray_wait(self):
        if self._tray_waiting:
            self.hide()
            return
        self._tray_waiting = True
        self._release_session_for_tray()
        self.hide()

    def quit_from_tray(self):
        """トレイメニューからのみFastPDFを完全終了する。"""
        self._allow_real_close = True
        self._tray_waiting = False
        try:
            self.tray_icon.hide()
        except Exception:
            pass
        self.close()
        QApplication.instance().quit()

    def emoji_button(self, text, tooltip, slot, checkable=False):
        return make_tool_button(text, tooltip, slot, checkable)


    def toolbar_separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setFixedHeight(26)
        return line

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        top_bar = QFrame()
        top_bar.setFrameShape(QFrame.StyledPanel)
        top = QHBoxLayout(top_bar)
        top.setContentsMargins(8, 5, 8, 5)
        top.setSpacing(6)

        self.list_toggle = self.emoji_button('🔼', 'PDFリストの表示 / 非表示を切り替えます', self.toggle_list_panel, True)
        self.list_toggle.setChecked(True)

        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(90)
        self.preset_combo.setMaximumWidth(150)
        self.preset_combo.setMinimumHeight(32)
        self.preset_combo.currentTextChanged.connect(self.on_preset_changed)
        self.preset_combo.setToolTip('使用するPDFリストプリセットを選択します')

        self.preset_save = self.emoji_button('💾', '現在のPDFリストをプリセットとして保存します', self.save_current_preset)
        self.preset_delete = self.emoji_button('🗑️', '選択中のリストプリセットを削除します', self.delete_current_preset)

        self.page_manager_btn = self.emoji_button('✏️', '選択したPDFを編集します', self.open_page_manager)
        self.settings_btn = self.emoji_button('⚙️', '表示解像度・ズーム・キャッシュなどの設定を開きます', self.open_settings)
        self.print_btn = self.emoji_button('🖨️', '印刷設定と印刷プレビューを開きます', self.open_print)
        self.help_btn = self.emoji_button('❔', 'このツールの操作説明を開きます', self.open_help)

        self.fit_btn = self.emoji_button('📄', '現在ページ全体を表示領域に収まる大きさで表示します', self.fit_page)
        self.zoom_out_btn = self.emoji_button('➖', '表示倍率を10%縮小します（Ctrl + ホイール下でも操作できます）', lambda: self.change_zoom(-10))
        self.zoom_label = QLabel(f'{self.zoom_percent}%')
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_label.setToolTip('現在の表示倍率を表示します')
        self.zoom_label.setFixedWidth(62)
        self.zoom_label.setMinimumHeight(30)
        self.zoom_label.setStyleSheet(
            'QLabel { background: #ffffff; color: #202020; padding: 3px 6px; border: 1px solid #888; border-radius: 6px; font-weight: 600; }'
        )
        self.zoom_in_btn = self.emoji_button('➕', '表示倍率を10%拡大します（Ctrl + ホイール上でも操作できます）', lambda: self.change_zoom(10))
        self.detail_view_btn = self.emoji_button(
            '🔎',
            '現在ページだけを高解像度で再描画します。PDF本体や他ページには影響しません',
            self.refresh_current_page_high_quality
        )
        self.rotate_left_view_btn = self.emoji_button(
            '↶',
            '現在のページを表示上だけ左へ90度回転します。PDF本体には保存されません',
            lambda: self.rotate_current_page_view(-90)
        )
        self.rotate_right_view_btn = self.emoji_button(
            '↷',
            '現在のページを表示上だけ右へ90度回転します。PDF本体には保存されません',
            lambda: self.rotate_current_page_view(90)
        )

        # 左：PDFリスト幅内にリスト/プリセット操作を収める
        self.list_controls = QWidget()
        self.list_controls.setObjectName('viewerListControls')
        self.list_controls.setStyleSheet(
            """
            QWidget#viewerListControls {
                background: transparent;
                border: none;
            }
            QWidget#viewerListControls QComboBox {
                border: 1px solid #888;
                border-radius: 6px;
                padding: 2px 6px;
                background: #ffffff;
                color: #202020;
            }
            QWidget#viewerListControls QComboBox QAbstractItemView {
                background: #ffffff;
                color: #202020;
                selection-background-color: #dcecff;
                selection-color: #202020;
                outline: 0;
            }
            """
        )
        self.list_controls.setFixedWidth(270)
        list_controls_layout = QHBoxLayout(self.list_controls)
        list_controls_layout.setContentsMargins(0, 0, 0, 0)
        list_controls_layout.setSpacing(4)
        list_controls_layout.addWidget(self.list_toggle)
        list_controls_layout.addWidget(self.preset_combo, 1)
        list_controls_layout.addWidget(self.preset_save)
        list_controls_layout.addWidget(self.preset_delete)
        top.addWidget(self.list_controls)

        # プリセット群の直後に仕切り、その右へ管理ボタン群
        top.addSpacing(6)
        top.addWidget(self.toolbar_separator())
        top.addSpacing(6)

        top.addWidget(self.settings_btn)
        top.addWidget(self.print_btn)

        # 表示・拡大縮小は中央寄せ
        top.addStretch(1)
        top.addWidget(self.fit_btn)
        top.addWidget(self.zoom_out_btn)
        top.addWidget(self.zoom_in_btn)
        top.addWidget(self.zoom_label)
        top.addWidget(self.detail_view_btn)
        top.addWidget(self.rotate_left_view_btn)
        top.addWidget(self.rotate_right_view_btn)
        top.addStretch(1)

        # 左側の操作群が大きいので、中央位置を保つための右側スペーサ
        self.top_balance_spacer = QWidget()
        self.top_balance_spacer.setFixedWidth(365)
        top.addWidget(self.top_balance_spacer)

        # 操作説明はツールバー最右端に独立配置
        top.addSpacing(6)
        top.addWidget(self.help_btn)

        root.addWidget(top_bar)

        self.splitter = QSplitter(Qt.Horizontal)

        self.list_panel = QWidget()
        list_layout = QVBoxLayout(self.list_panel)
        list_layout.setContentsMargins(2, 2, 4, 2)

        list_header = QHBoxLayout()
        list_header.addWidget(QLabel('PDF'))
        list_header.addStretch(1)
        list_header.addWidget(self.page_manager_btn)
        list_header.addWidget(self.emoji_button('➕', 'PDFを追加', self.open_files))
        list_header.addWidget(self.emoji_button('➖', '選択PDFをリストから削除', self.remove_selected_pdf))
        list_layout.addLayout(list_header)

        self.pdf_list = PDFListWidget()
        self.pdf_list.pdfFilesDropped.connect(self.add_pdf_files)
        self.pdf_list.orderChanged.connect(self.on_list_order_changed)
        self.pdf_list.revealRequested.connect(
            lambda path: reveal_pdf_location(self, path)
        )
        self.pdf_list.currentRowChanged.connect(self.on_list_row_changed)
        self.pdf_list.itemSelectionChanged.connect(self.on_list_selection_changed)
        list_layout.addWidget(self.pdf_list, 1)

        self.splitter.addWidget(self.list_panel)

        self.page_label = QLabel('PDFまたはフォルダをドラッグ＆ドロップしてください')
        self.page_label.setAlignment(Qt.AlignCenter)
        self.page_label.setStyleSheet('QLabel { background: #303030; color: #e8e8e8; }')

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.page_label)
        self.scroll.viewport().installEventFilter(self)
        self.scroll.viewport().setMouseTracking(True)

        self.splitter.addWidget(self.scroll)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([270, 1100])
        root.addWidget(self.splitter, 1)

        self.bottom_bar = QFrame()
        self.bottom_bar.setFrameShape(QFrame.StyledPanel)
        bottom = QHBoxLayout(self.bottom_bar)
        bottom.setContentsMargins(8, 4, 8, 4)
        bottom.setSpacing(6)

        self.favorite_indicator = QLabel('')
        self.favorite_indicator.setFixedWidth(30)
        self.favorite_indicator.setAlignment(Qt.AlignCenter)
        self.favorite_indicator.setStyleSheet('font-size: 20px;')

        self.favorite_mode_btn = self.emoji_button('🌟', 'お気に入りページだけを←→で移動するモードを切り替えます', self.toggle_favorite_mode, True)
        self.clear_favorite_btn = self.emoji_button('🆑', '現在PDFのお気に入りをすべてクリア', self.clear_favorites)

        self.current_page_edit = QLineEdit('0')
        self.current_page_edit.setAlignment(Qt.AlignCenter)
        self.current_page_edit.setFixedWidth(58)
        self.current_page_edit.setMinimumHeight(30)
        self.current_page_edit.setToolTip('ページ番号を入力して Enter でそのページへ移動します')
        self.current_page_edit.returnPressed.connect(self.jump_to_typed_page)
        self.current_page_edit.editingFinished.connect(self.restore_page_edit_if_invalid)
        self.current_page_edit.setStyleSheet(
            'QLineEdit { font-size: 14px; font-weight: 600; padding: 2px 6px; }'
        )

        self.total_page_label = QLabel('/ 0')
        self.total_page_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.total_page_label.setMinimumWidth(54)
        self.total_page_label.setStyleSheet(
            'QLabel { font-size: 14px; font-weight: 600; }'
        )

        self.first_page_btn = self.emoji_button('⏪', '先頭ページへ移動します', self.first_page)
        self.prev_btn = self.emoji_button('◀️', '前ページへ移動します（←キー / ホイール上）', self.previous_page)
        self.next_btn = self.emoji_button('▶️', '次ページへ移動します（→キー / ホイール下）', self.next_page)
        self.last_page_btn = self.emoji_button('⏩', '最終ページへ移動します', self.last_page)

        # [🌟] [🆑] [⭐] [現在ページ] / 総ページ   [⏪] [◀️] [▶️] [⏩]
        bottom.addStretch(1)
        bottom.addWidget(self.favorite_mode_btn)
        bottom.addWidget(self.clear_favorite_btn)
        bottom.addSpacing(8)
        bottom.addWidget(self.favorite_indicator)
        bottom.addWidget(self.current_page_edit)
        bottom.addWidget(self.total_page_label)
        bottom.addSpacing(8)
        bottom.addWidget(self.first_page_btn)
        bottom.addWidget(self.prev_btn)
        bottom.addWidget(self.next_btn)
        bottom.addWidget(self.last_page_btn)
        bottom.addStretch(1)

        root.addWidget(self.bottom_bar)

    def is_text_input(self, widget):
        return isinstance(widget, (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QPlainTextEdit))

    def eventFilter(self, obj, event):
        if obj is self.scroll.viewport():
            if event.type() == QEvent.Resize:
                if self.fit_mode and self.current_path:
                    QTimer.singleShot(0, self.apply_current_image_scale)

            if event.type() == QEvent.Wheel and self.current_path:
                delta = event.angleDelta().y()
                if delta == 0:
                    return True

                if event.modifiers() & Qt.ControlModifier:
                    self.change_zoom(10 if delta > 0 else -10)
                else:
                    if delta > 0:
                        self.previous_page()
                    else:
                        self.next_page()
                return True

            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.MiddleButton:
                self._panning = True
                self._pan_start_pos = event.position().toPoint()
                self._pan_start_h = self.scroll.horizontalScrollBar().value()
                self._pan_start_v = self.scroll.verticalScrollBar().value()
                self.scroll.viewport().setCursor(Qt.ClosedHandCursor)
                return True

            if event.type() == QEvent.MouseMove and self._panning and self._pan_start_pos is not None:
                pos = event.position().toPoint()
                delta = pos - self._pan_start_pos
                self.scroll.horizontalScrollBar().setValue(self._pan_start_h - delta.x())
                self.scroll.verticalScrollBar().setValue(self._pan_start_v - delta.y())
                return True

            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.MiddleButton:
                self._panning = False
                self._pan_start_pos = None
                self.scroll.viewport().unsetCursor()
                return True

        if event.type() == QEvent.KeyPress and QApplication.activeWindow() is self:
            focus = QApplication.focusWidget()
            if self.is_text_input(focus):
                return super().eventFilter(obj, event)

            key = event.key()
            if key == Qt.Key_Up:
                self.move_file_selection(-1)
                return True
            if key == Qt.Key_Down:
                self.move_file_selection(1)
                return True
            if key == Qt.Key_Left:
                self.previous_page()
                return True
            if key == Qt.Key_Right:
                self.next_page()
                return True
            if key == Qt.Key_Space:
                self.toggle_current_favorite()
                return True

        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith('.pdf') for u in event.mimeData().urls()):
            event.acceptProposedAction(); return
        event.ignore()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile().lower().endswith('.pdf')]
        if paths:
            self.add_pdf_files(paths)
            event.acceptProposedAction()

    def refresh_preset_combo(self, select_name=None):
        self._preset_combo_updating = True
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem(PresetManager.EMPTY_NAME)
        for name in self.presets.names():
            self.preset_combo.addItem(name)
        wanted = select_name or self.current_preset or PresetManager.EMPTY_NAME
        idx = self.preset_combo.findText(wanted)
        self.preset_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.preset_combo.blockSignals(False)
        self._preset_combo_updating = False

    def on_preset_changed(self, name):
        if self._preset_combo_updating:
            return

        if name == self.current_preset:
            return

        self.load_preset(name)

    def load_preset(self, name):
        self.close_current_doc()
        self.cache.clear()

        # ページ位置はプリセット情報には含めない。
        # プリセットを開き直したときは、全PDFを1ページ目から開始する。
        self.page_positions.clear()

        self.current_path = None
        self.current_page = 0
        self.current_preset = name or PresetManager.EMPTY_NAME
        self.pdf_paths = []
        self.pdf_list.blockSignals(True)
        self.pdf_list.clear()
        if self.current_preset != PresetManager.EMPTY_NAME:
            for path in self.presets.load(self.current_preset):
                if Path(path).is_file():
                    self.pdf_paths.append(path)
                    item = QListWidgetItem()
                    self.style_pdf_list_item(item, path)
                    self.pdf_list.addItem(item)
        self.pdf_list.blockSignals(False)
        if self.pdf_paths:
            self.pdf_list.setCurrentRow(0)
            self.open_pdf(self.pdf_paths[0], True)
        else:
            self.clear_view()

    def save_current_preset(self):
        name = self.current_preset
        if name == PresetManager.EMPTY_NAME:
            name, ok = QInputDialog.getText(self, 'プリセット', 'プリセット名')
            if not ok or not name.strip(): return
            name = name.strip()
        if self.presets.save(name, self.pdf_paths):
            self.current_preset = name
            self.refresh_preset_combo(name)
            self.statusBar().showMessage(f'プリセット保存: {name}', 1500)
        else:
            QMessageBox.warning(self, 'プリセット', '保存できませんでした。')

    def auto_save_preset(self):
        if self.current_preset != PresetManager.EMPTY_NAME:
            self.presets.save(self.current_preset, self.pdf_paths)

    def delete_current_preset(self):
        name = self.current_preset
        if name == PresetManager.EMPTY_NAME:
            return

        if QMessageBox.question(
            self,
            'プリセット削除',
            f'「{name}」を削除しますか？'
        ) != QMessageBox.Yes:
            return

        self.presets.delete(name)
        self.current_preset = PresetManager.EMPTY_NAME
        self.refresh_preset_combo(PresetManager.EMPTY_NAME)
        self.load_preset(PresetManager.EMPTY_NAME)

    def open_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, 'PDF', '', 'PDF (*.pdf)')
        if files: self.add_pdf_files(files)

    def style_pdf_list_item(self, item, path):
        path = normalize_path(path)
        item.setData(Qt.UserRole, path)
        item.setToolTip(path)
        if is_temp_pdf_path(path):
            item.setText('🧪 ' + Path(path).name)
            item.setForeground(QColor('#d48a00'))
            item.setToolTip(
                '一時保存ファイル\n'
                f'{path}\n'
                '不要になったら編集画面の 📂 temp から削除してください。'
            )
        else:
            item.setText('📄 ' + Path(path).name)

    def rebuild_main_pdf_list_from_paths(self, selected_path=None):
        selected_path = normalize_path(selected_path) if selected_path else None
        self.pdf_list.blockSignals(True)
        try:
            self.pdf_list.clear()
            for path in self.pdf_paths:
                item = QListWidgetItem()
                self.style_pdf_list_item(item, path)
                self.pdf_list.addItem(item)

            if selected_path:
                for row in range(self.pdf_list.count()):
                    item = self.pdf_list.item(row)
                    if normalize_path(item.data(Qt.UserRole)) == selected_path:
                        self.pdf_list.setCurrentRow(row)
                        item.setSelected(True)
                        self.pdf_list.scrollToItem(item)
                        break
        finally:
            self.pdf_list.blockSignals(False)

        self.page_manager_btn.setEnabled(bool(self.pdf_paths))

    def update_pdf_list_item_text(self, path):
        path = normalize_path(path)
        for i in range(self.pdf_list.count()):
            item = self.pdf_list.item(i)
            if normalize_path(item.data(Qt.UserRole)) == path:
                self.style_pdf_list_item(item, path)
                return

    def refresh_pdf_list_modified_marks(self):
        for path in self.pdf_paths:
            self.update_pdf_list_item_text(path)

    def add_pdf_files(self, paths):
        expanded = []
        folders = []

        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                folders.append(p)
            elif str(p).lower().endswith('.pdf') and p.is_file():
                expanded.append(normalize_path(p))

        include_subfolders = False
        folders_with_subdirs = [
            f for f in folders
            if any(child.is_dir() for child in f.iterdir())
        ]
        if folders_with_subdirs:
            result = QMessageBox.question(
                self,
                'サブフォルダ',
                'ドロップしたフォルダ内にサブフォルダがあります。\n\n'
                'サブフォルダ内のPDFも登録しますか？',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            include_subfolders = result == QMessageBox.Yes

        for folder in folders:
            iterator = folder.rglob('*.pdf') if include_subfolders else folder.glob('*.pdf')
            expanded.extend(normalize_path(p) for p in iterator if p.is_file())

        # 重複を除きつつ入力順を維持
        seen = set()
        ordered = []
        for path in expanded:
            key = normalize_path(path)
            if key not in seen:
                seen.add(key)
                ordered.append(key)

        added = False
        for path in ordered:
            if path not in self.pdf_paths:
                self.pdf_paths.append(path)
                item = QListWidgetItem()
                self.style_pdf_list_item(item, path)
                self.pdf_list.addItem(item)
                added = True

        if not added:
            return
        self.auto_save_preset()
        if self.current_path is None and self.pdf_paths:
            self.pdf_list.setCurrentRow(0)

    def remove_selected_pdf(self):
        items = self.pdf_list.selectedItems()
        if not items:
            return

        rows = sorted((self.pdf_list.row(item) for item in items), reverse=True)
        paths = [normalize_path(self.pdf_list.item(r).data(Qt.UserRole)) for r in rows]
        removed_current = self.current_path in paths
        for path in paths:
            self.page_positions.pop(path, None)

        for row in rows:
            self.pdf_list.takeItem(row)

        self.pdf_paths = [
            normalize_path(self.pdf_list.item(i).data(Qt.UserRole))
            for i in range(self.pdf_list.count())
        ]
        self.auto_save_preset()

        if not self.pdf_paths:
            self.close_current_doc()
            self.current_path = None
            self.multi_selection_active = False
            self.clear_view()
            return

        target = min(min(rows), len(self.pdf_paths)-1)
        self.pdf_list.setCurrentRow(target)
        self.pdf_list.item(target).setSelected(True)
        if removed_current:
            self.open_pdf(self.pdf_paths[target], False)

    def on_list_order_changed(self):
        self.pdf_paths = [self.pdf_list.item(i).data(Qt.UserRole) for i in range(self.pdf_list.count())]
        self.auto_save_preset()

    def on_list_row_changed(self, row):
        QTimer.singleShot(0, self.on_list_selection_changed)

    def on_list_selection_changed(self):
        items = self.pdf_list.selectedItems()
        count = len(items)
        self.page_manager_btn.setEnabled(bool(self.pdf_paths))

        if count > 1:
            self.multi_selection_active = True
            self.generation += 1
            self.page_label.clear()
            self.page_label.setText(
                f'{count}件のPDFを選択中\n\nプレビューは表示しません。\n🖨️で選択したPDFをまとめて印刷できます。'
            )
            self.page_label.resize(700, 500)
            self.current_page_edit.setText('-')
            self.total_page_label.setText('')
            self.favorite_indicator.setText('')
            self.setWindowTitle(f'{APP_NAME} - {count}件選択')
            return

        self.multi_selection_active = False
        if count == 1:
            path = normalize_path(items[0].data(Qt.UserRole))
            if self.current_path != path or self.current_doc is None:
                self.open_pdf(path, False)
            else:
                self.render_current_page()
                self.update_status()
        elif not self.pdf_paths:
            self.clear_view()

    def move_file_selection(self, delta):
        """
        ↑↓でPDFを切り替える。

        複数選択時:
        - リスト上で最も上にある選択ファイルを基準にする。
        - その行から ↑ / ↓ へ1件移動する。
        - 複数選択は解除し、移動先だけを1件選択する。
        """
        if not self.pdf_paths or self.pdf_list.count() <= 0:
            return

        selected_rows = sorted({
            self.pdf_list.row(item)
            for item in self.pdf_list.selectedItems()
            if self.pdf_list.row(item) >= 0
        })

        if selected_rows:
            base_row = selected_rows[0]
        else:
            base_row = self.pdf_list.currentRow()
            if base_row < 0:
                base_row = 0

        target = max(
            0,
            min(self.pdf_list.count() - 1, base_row + int(delta))
        )

        # ↑↓操作では常に単一選択に戻す。
        self.pdf_list.blockSignals(True)
        try:
            self.pdf_list.clearSelection()
            self.pdf_list.setCurrentRow(target)
            item = self.pdf_list.item(target)
            if item is not None:
                item.setSelected(True)
                self.pdf_list.scrollToItem(item)
        finally:
            self.pdf_list.blockSignals(False)

        # signalを止めているため、移動先PDFを明示的に表示する。
        item = self.pdf_list.item(target)
        if item is not None:
            path = item.data(Qt.UserRole)
            if path:
                self.open_pdf(
                    normalize_path(path),
                    reset_page=False
                )
                self.update_status()

    def close_current_doc(self):
        if self.current_doc is not None:
            try: self.current_doc.close()
            except Exception: pass
        self.current_doc = None

    def open_pdf(self, path, reset_page=True):
        path = normalize_path(path)
        if self.current_path == path and self.current_doc is not None:
            return

        # 現在開いているPDFのページ位置を記憶
        if self.current_path and self.current_doc is not None:
            self.page_positions[self.current_path] = self.current_page

        self.close_current_doc()
        # PDF切替ではLRUキャッシュを保持し、戻った時の表示を高速化する。
        # 上限はcache_pages設定で管理される。
        self.generation += 1

        try:
            doc = fitz.open(path)
            if doc.needs_pass:
                QMessageBox.warning(self, 'PDF', '暗号化PDFには対応していません。')
                doc.close()
                return
            if doc.page_count <= 0:
                QMessageBox.warning(self, 'PDF', 'ページがありません。')
                doc.close()
                return
        except Exception as e:
            QMessageBox.critical(self, 'PDF', f'開けません。\n{e}')
            return

        self.current_doc = doc
        self.current_path = path

        # 初回だけ先頭。いったん開いたPDFは前回見ていたページへ戻す。
        if not reset_page and path in self.page_positions:
            self.current_page = max(
                0,
                min(doc.page_count - 1, self.page_positions[path])
            )
        else:
            self.current_page = 0

        self.page_positions[path] = self.current_page
        self.current_dpi = self.settings.dpi_for_pdf(path, doc.page_count)
        self.zoom_percent = int(self.settings.data.get('zoom_percent', 100))
        self.fit_mode = bool(self.settings.data.get('fit_on_open', True))
        self.render_current_page()
        self.update_status()

    def clear_view(self):
        self.page_label.clear()
        self.page_label.setText('PDFをドラッグ＆ドロップしてください')
        self.page_label.resize(700, 500)
        self.current_page_edit.setText('0')
        self.total_page_label.setText('/ 0')
        self.favorite_indicator.setText('')
        self.setWindowTitle(APP_NAME)

    def cache_key(self, path, page, dpi):
        return (path, page, dpi)

    def cache_get(self, key):
        if key not in self.cache: return None
        image = self.cache.pop(key)
        self.cache[key] = image
        return image

    def cache_put(self, key, image):
        if key in self.cache: self.cache.pop(key)
        self.cache[key] = image
        max_count = max(1, int(self.settings.data.get('cache_pages', 7)))
        while len(self.cache) > max_count:
            self.cache.popitem(last=False)

    def current_page_render_dpi(self):
        if not self.current_path:
            return self.current_dpi
        return int(self.page_detail_dpi.get(
            (self.current_path, self.current_page),
            self.current_dpi
        ))

    def refresh_current_page_high_quality(self):
        """現在ページだけを一時的に高解像度で再描画する。"""
        if not self.current_doc or not self.current_path:
            return
        # 通常設定が既に高解像度ならそれを下げず、最低300 DPIを確保する。
        detail_dpi = max(300, int(self.current_dpi))
        self.page_detail_dpi[(self.current_path, self.current_page)] = detail_dpi
        self.render_current_page()
        self.statusBar().showMessage(
            f'現在ページを高解像度表示へ更新しました（{detail_dpi} DPI・一時表示）',
            3000
        )

    def render_current_page(self):
        if not self.current_doc or not self.current_path: return
        render_dpi = self.current_page_render_dpi()
        key = self.cache_key(self.current_path, self.current_page, render_dpi)
        cached = self.cache_get(key)
        if cached is not None:
            self.apply_image(cached, render_dpi); self.prefetch_neighbors(); return
        self.page_label.setPixmap(QPixmap())
        self.page_label.setText('読み込み中…')
        self.start_render(self.current_path, self.current_page, render_dpi, self.generation)
        self.prefetch_neighbors()

    def start_render(self, path, page, dpi, generation):
        key = self.cache_key(path, page, dpi)
        if key in self.cache or key in self.pending_keys: return
        self.pending_keys.add(key)
        worker = RenderWorker(path, page, dpi, generation)
        worker.rendered.connect(self.on_rendered)
        worker.failed.connect(self.on_render_failed)
        worker.finished.connect(lambda w=worker, k=key: self.cleanup_worker(w, k))
        self.workers.append(worker)
        worker.start()

    def cleanup_worker(self, worker, key):
        self.pending_keys.discard(key)
        if worker in self.workers: self.workers.remove(worker)
        worker.deleteLater()

    def on_rendered(self, path, page, dpi, generation, image):
        self.cache_put((path, page, dpi), image)
        if (
            not self.multi_selection_active
            and generation == self.generation
            and path == self.current_path
            and page == self.current_page
            and dpi == self.current_page_render_dpi()
        ):
            self.apply_image(image, dpi)

    def on_render_failed(self, path, page, message):
        if path == self.current_path and page == self.current_page:
            self.page_label.setText(f'表示できません\n{message}')

    def prefetch_neighbors(self):
        if not self.current_doc or not self.settings.data.get('background_preload', True): return
        distance = int(self.settings.data.get('preload_pages', 2))
        for d in range(1, distance + 1):
            for idx in (self.current_page - d, self.current_page + d):
                if 0 <= idx < self.current_doc.page_count:
                    self.start_render(self.current_path, idx, self.current_dpi, self.generation)

    def apply_image(self, image, dpi=None):
        self._current_qimage = image
        self._current_image_dpi = int(dpi or self.current_dpi)
        self.apply_current_image_scale()

    def apply_current_image_scale(self):
        image = getattr(self, '_current_qimage', None)
        if image is None or image.isNull():
            return

        pix = QPixmap.fromImage(image)

        # 現在のPDF・現在ページだけに表示回転を適用
        rotation = 0
        if self.current_path is not None:
            rotation = self.page_view_rotations.get(
                (self.current_path, self.current_page),
                0
            )
        if rotation:
            pix = pix.transformed(
                QTransform().rotate(rotation),
                Qt.SmoothTransformation
            )

        if self.fit_mode:
            viewport = self.scroll.viewport().size()
            scaled = pix.scaled(max(100, viewport.width() - 20), max(100, viewport.height() - 20), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            image_dpi = max(1, int(getattr(self, '_current_image_dpi', self.current_dpi)))
            scale = (self.zoom_percent / 100.0) * (96.0 / image_dpi)
            scaled = pix.scaled(max(1, int(pix.width() * scale)), max(1, int(pix.height() * scale)), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        # 白背景のPDFでも表示領域との境界が分かるよう、
        # pixmapサイズを変えず外周を内側へ描画する。
        if not scaled.isNull():
            bordered = QPixmap(scaled)
            p = QPainter(bordered)
            border_pen = QPen(QColor('#666666'))
            border_pen.setWidth(2)
            p.setPen(border_pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(
                1,
                1,
                max(0, bordered.width() - 2),
                max(0, bordered.height() - 2)
            )
            p.end()
            scaled = bordered

        self.page_label.setText('')
        self.page_label.setPixmap(scaled)
        self.page_label.resize(scaled.size())
        self.zoom_label.setText('FIT' if self.fit_mode else f'{self.zoom_percent}%')
        self.update_status()

    def fit_page(self):
        if not self.current_path: return
        self.fit_mode = True
        self.apply_current_image_scale()

    def change_zoom(self, delta):
        if not self.current_path: return
        self.fit_mode = False
        self.zoom_percent = max(25, min(400, self.zoom_percent + delta))
        self.apply_current_image_scale()

    def rotate_current_page_view(self, delta):
        if not self.current_doc or not self.current_path:
            return

        key = (self.current_path, self.current_page)
        current = self.page_view_rotations.get(key, 0)
        new_rotation = (current + delta) % 360

        if new_rotation == 0:
            self.page_view_rotations.pop(key, None)
        else:
            self.page_view_rotations[key] = new_rotation

        # 表示上だけ回転。PDF本体には一切反映しない。
        self.apply_current_image_scale()
        direction = '左' if delta < 0 else '右'
        self.statusBar().showMessage(
            f'現在ページを表示上だけ{direction}へ90°回転しました。',
            3000
        )

    def previous_page(self): self.move_page(-1)
    def next_page(self): self.move_page(1)

    def first_page(self):
        if not self.current_doc:
            return
        if self.current_page != 0:
            self.current_page = 0
            self.generation += 1
            self.render_current_page()
            self.update_status()

    def last_page(self):
        if not self.current_doc:
            return
        target = max(0, self.current_doc.page_count - 1)
        if self.current_page != target:
            self.current_page = target
            self.generation += 1
            self.render_current_page()
            self.update_status()

    def jump_to_typed_page(self):
        if not self.current_doc:
            return
        try:
            page_no = int(self.current_page_edit.text().strip())
        except ValueError:
            self.restore_page_edit_if_invalid()
            return

        page_no = max(1, min(self.current_doc.page_count, page_no))
        target = page_no - 1
        self.current_page_edit.setText(str(page_no))

        if target != self.current_page:
            self.current_page = target
            self.generation += 1
            self.render_current_page()
            self.update_status()

        self.setFocus()

    def restore_page_edit_if_invalid(self):
        if not self.current_doc:
            self.current_page_edit.setText('0')
            return
        txt = self.current_page_edit.text().strip()
        if not txt.isdigit():
            self.current_page_edit.setText(str(self.current_page + 1))

    def move_page(self, delta):
        if not self.current_doc: return
        if self.favorite_mode:
            favs = sorted(p for p in self.favorites.pages(self.current_path) if 0 <= p < self.current_doc.page_count)
            if favs:
                if delta > 0:
                    later = [p for p in favs if p > self.current_page]
                    target = later[0] if later else favs[-1]
                else:
                    earlier = [p for p in favs if p < self.current_page]
                    target = earlier[-1] if earlier else favs[0]
            else:
                target = max(0, min(self.current_doc.page_count - 1, self.current_page + delta))
        else:
            target = max(0, min(self.current_doc.page_count - 1, self.current_page + delta))
        if target != self.current_page:
            self.current_page = target
            self.generation += 1
            self.render_current_page()
            self.update_status()

    def toggle_current_favorite(self):
        if not self.current_path:
            return
        state = self.favorites.toggle(self.current_path, self.current_page)
        self.favorite_indicator.setText('⭐' if state else '')

        if self.favorites.save():
            self.statusBar().showMessage(
                '⭐ 登録しました' if state else '⭐ 解除しました',
                1600
            )
        else:
            QMessageBox.warning(
                self,
                'お気に入り',
                'favorites.json を保存できませんでした。'
            )

    def toggle_favorite_mode(self, checked):
        self.favorite_mode = bool(checked)
        self.statusBar().showMessage('🌟 お気に入りモード' if self.favorite_mode else '通常ページモード', 1200)

    def clear_favorites(self):
        if not self.current_path:
            return
        if QMessageBox.question(
            self,
            '🆑',
            '現在のPDFのお気に入りをすべてクリアしますか？'
        ) != QMessageBox.Yes:
            return

        self.favorites.clear(self.current_path)
        if not self.favorites.save():
            QMessageBox.warning(
                self,
                'お気に入り',
                'favorites.json を保存できませんでした。'
            )

        self.favorite_indicator.setText('')
        self.update_status()
        self.statusBar().showMessage(
            'お気に入りをクリアしました',
            1800
        )

    def update_status(self):
        if not self.current_doc:
            self.current_page_edit.setText('0')
            self.total_page_label.setText('/ 0')
            self.favorite_indicator.setText('')
            return
        self.page_positions[self.current_path] = self.current_page
        self.current_page_edit.setText(str(self.current_page + 1))
        self.total_page_label.setText(f'/ {self.current_doc.page_count}')
        self.favorite_indicator.setText('⭐' if self.current_page in self.favorites.pages(self.current_path) else '')
        self.setWindowTitle(f'{APP_NAME} - {Path(self.current_path).name}')

    def toggle_list_panel(self, checked):
        self.list_panel.setVisible(bool(checked))
        self.list_toggle.setText('🔼' if checked else '🔽')

    def open_help(self):
        dlg = HelpDialog(self)
        dlg.setModal(True)
        dlg.exec()

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.Accepted:
            self.cache.clear()
            if self.current_doc and self.current_path:
                self.current_dpi = self.settings.dpi_for_pdf(self.current_path, self.current_doc.page_count)
                self.zoom_percent = int(self.settings.data.get('zoom_percent', 100))
                self.fit_mode = bool(self.settings.data.get('fit_on_open', True))
                self.generation += 1
                self.render_current_page()
            self.scroll.setAlignment(Qt.AlignCenter if self.settings.data.get('center_page', True) else (Qt.AlignLeft | Qt.AlignTop))

    def on_editor_pdf_list_changed(self, paths):
        """
        編集画面で追加されたPDFリストを即時反映する。
        PDF編集内容の保存とは別で、リスト構成だけを更新する。
        """
        current = normalize_path(self.current_path) if self.current_path else None

        ordered = []
        seen = set()
        for raw in paths:
            path = normalize_path(raw)
            if (
                path
                and path.lower().endswith('.pdf')
                and Path(path).is_file()
                and not is_temp_pdf_path(path)
                and path not in seen
            ):
                seen.add(path)
                ordered.append(path)

        self.pdf_paths = ordered
        self.auto_save_preset()
        self.rebuild_main_pdf_list_from_paths(current)
        self.statusBar().showMessage(
            f'PDFリスト更新: {len(self.pdf_paths)}件',
            1500
        )

    def open_page_manager(self):
        if not self.pdf_paths:
            return

        # 選択状態に関係なく編集モードへ入れる。
        # 複数選択時はリスト上で最も先頭の選択PDFを初期対象にする。
        selected_rows = sorted(
            self.pdf_list.row(item)
            for item in self.pdf_list.selectedItems()
        )

        if selected_rows:
            initial_row = selected_rows[0]
        else:
            initial_row = self.pdf_list.currentRow()
            if initial_row < 0:
                initial_row = 0

        initial_row = max(
            0, min(initial_row, len(self.pdf_paths) - 1)
        )
        edit_path = normalize_path(self.pdf_paths[initial_row])

        if self.current_path == edit_path and self.current_doc is not None:
            start_page = self.current_page
        else:
            start_page = self.page_positions.get(edit_path, 0)

        # 編集UIが元PDFを安全に上書きできるようビューア側のハンドルを閉じる。
        if self.current_path and self.current_doc is not None:
            self.page_positions[self.current_path] = self.current_page
        self.close_current_doc()

        # 編集モジュールはここで初めて読み込む。
        # PDF関連付けからの通常ビューア起動では editor.py を読み込まない。
        try:
            from editor import PageManagerDialog

            dlg = PageManagerDialog(
                edit_path,
                self.favorites,
                None,
                start_page=start_page,
                pdf_paths=list(self.pdf_paths)
            )
            dlg.savedPdf.connect(self.on_page_manager_saved)
            dlg.pdfListChanged.connect(self.on_editor_pdf_list_changed)
        except Exception as e:
            self.show()
            self.raise_()
            self.activateWindow()

            QMessageBox.critical(
                self,
                '編集モード',
                '編集画面を開けませんでした。\n\n'
                f'{type(e).__name__}: {e}'
            )

            # 閉じたビューア側PDFを元に戻す。
            if Path(edit_path).is_file():
                self.open_pdf(edit_path, reset_page=False)
                if self.current_doc:
                    self.current_page = min(
                        start_page,
                        max(0, self.current_doc.page_count - 1)
                    )
                    self.render_current_page()
                    self.update_status()
            return

        # 編集ダイアログの生成成功後にビューアを隠す。
        # exec() はネストしたイベントループを動かすため、編集モード中でも
        # QLocalServer は外部から渡されたPDFを受け取れる。
        # その受け皿として、現在の編集ダイアログを保持しておく。
        self._active_editor_dialog = dlg
        self.hide()

        try:
            dlg.exec()
        finally:
            # 以後の外部PDFは通常どおりビューア側で受ける。
            if self._active_editor_dialog is dlg:
                self._active_editor_dialog = None
            self.show()
            self.raise_()
            self.activateWindow()

            # 編集UI側で追加された通常PDFリストを最終同期する。
            # 一時保存リストはビューアへ持ち込まない。
            self.pdf_paths = [
                normalize_path(p) for p in dlg.pdf_paths
                if p and Path(p).is_file() and not is_temp_pdf_path(p)
            ]
            self.auto_save_preset()

            # 編集側のお気に入り情報をディスクから再読込。
            self.favorites.reload()

            # 一時PDFを表示中に戻った場合も、ビューアには通常PDFだけを戻す。
            return_path = dlg.viewer_return_path()
            if return_path:
                return_path = normalize_path(return_path)
            self.rebuild_main_pdf_list_from_paths(return_path)
            return_page = max(0, int(dlg.current_index))

            self.cache.clear()
            self.current_path = None

            if return_path and Path(return_path).is_file():
                # メインリストにも同じPDFを選択状態にする。
                target_row = -1
                for i in range(self.pdf_list.count()):
                    item = self.pdf_list.item(i)
                    if normalize_path(item.data(Qt.UserRole)) == return_path:
                        target_row = i
                        break

                if target_row >= 0:
                    self.pdf_list.blockSignals(True)
                    self.pdf_list.clearSelection()
                    self.pdf_list.setCurrentRow(target_row)
                    self.pdf_list.item(target_row).setSelected(True)
                    self.pdf_list.blockSignals(False)
                    self.pdf_list.scrollToItem(
                        self.pdf_list.item(target_row)
                    )

                self.open_pdf(return_path, reset_page=False)
                if self.current_doc:
                    self.current_page = min(
                        return_page,
                        max(0, self.current_doc.page_count - 1)
                    )
                    self.render_current_page()
                    self.update_status()
            else:
                self.clear_view()


    def on_page_manager_saved(self, path):
        path = normalize_path(path)
        self.cache.clear()

        # temp は編集UI専用。一時ファイルはビューアのリストへ追加しない。
        if is_temp_pdf_path(path):
            return

        if path not in self.pdf_paths:
            self.add_pdf_files([path])

        self.update_pdf_list_item_text(path)
        self.statusBar().showMessage(
            f'編集PDFを保存しました: {Path(path).name}',
            4000
        )

    def open_external_pdf_paths(self, paths):
        """関連付け等で別プロセスから渡されたPDFを受け取る。

        通常時:
            ビューアのPDFリストへ追加し、先頭PDFを開く。
        編集モード中:
            ビューアへ戻さず、編集画面左側の通常PDFリストへ追加する。
            現在の編集対象は切り替えない。
        """
        valid = []
        seen = set()
        for raw in paths:
            if not raw:
                continue
            path = normalize_path(raw)
            if (
                path
                and path.lower().endswith('.pdf')
                and Path(path).is_file()
                and path not in seen
            ):
                seen.add(path)
                valid.append(path)

        if not valid:
            return

        # 編集モード中は、その編集画面のPDFリストへ追加するだけ。
        # add_pdfs_to_source_list() は既存仕様として編集対象を変更しない。
        dlg = self._active_editor_dialog
        if dlg is not None and dlg.isVisible():
            dlg.add_pdfs_to_source_list(valid)
            dlg.show()
            if dlg.isMinimized():
                dlg.showNormal()
            dlg.raise_()
            dlg.activateWindow()
            return

        # トレイ待機中なら、Qt/PyMuPDFを再起動せず新しいセッションとして復帰。
        self._tray_waiting = False
        self.add_pdf_files(valid)
        target = valid[0]
        if target not in self.pdf_paths:
            return

        row = self.pdf_paths.index(target)
        self.pdf_list.clearSelection()
        self.pdf_list.setCurrentRow(row)
        item = self.pdf_list.item(row)
        if item is not None:
            item.setSelected(True)
        self.open_pdf(target, reset_page=False)
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_print(self):
        # 印刷モジュールも印刷ボタンを押した時だけ読み込む。
        # QtPrintSupport と印刷プレビュー一式を通常起動から外す。
        try:
            from print_dialog import PrintDialog
        except Exception as e:
            QMessageBox.critical(
                self,
                '印刷',
                '印刷画面を開くためのモジュールを読み込めませんでした。\n\n'
                f'{type(e).__name__}: {e}'
            )
            return

        selected = self.pdf_list.selectedItems()
        if len(selected) > 1:
            paths = [
                normalize_path(item.data(Qt.UserRole))
                for item in selected
            ]
            # リスト上の順番にそろえる
            order = {p: i for i, p in enumerate(self.pdf_paths)}
            paths.sort(key=lambda p: order.get(p, 10**9))

            # 各PDFで最後に表示していたページを個別に渡す。
            current_pages = {}
            for path in paths:
                if path == self.current_path:
                    current_pages[path] = self.current_page
                else:
                    current_pages[path] = self.page_positions.get(path, 0)

            PrintDialog(
                paths,
                current_pages,
                self.favorites,
                self,
                settings_store=self.print_session_settings
            ).exec()
            return

        if not self.current_path:
            return
        PrintDialog(
            self.current_path,
            self.current_page,
            self.favorites,
            self,
            settings_store=self.print_session_settings
        ).exec()

    def closeEvent(self, event):
        # 通常の×はプロセスを終了しない。PDF/画像/リスト/印刷設定を解放して
        # 最小限のQtプロセス＋トレイ＋単一起動IPCだけを残す。
        if not self._allow_real_close:
            event.ignore()
            self.enter_tray_wait()
            return

        self.close_current_doc()
        for worker in list(self.workers):
            try:
                worker.requestInterruption()
                worker.wait(300)
            except Exception:
                pass
        event.accept()


def main():
    app = QApplication(sys.argv)
    # MainWindowを×で隠してもイベントループを維持し、関連付け起動を高速に受ける。
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(APP_STYLESHEET)

    args = command_line_pdf_paths()
    force_new_window = shift_pressed_at_launch()

    # 通常起動は既存Windowへ渡して、このプロセスを終了する。
    # Shiftを押しながら起動した場合だけ、別Windowとしてそのまま続行する。
    if not force_new_window and send_paths_to_existing_instance(args):
        return 0

    # 通常起動の最初のプロセスは、重いMainWindow構築より先に受付口を確保する。
    # Explorerが複数PDFに対してほぼ同時にexeを起動しても、後続はここへパスだけ渡す。
    single_server = None
    if not force_new_window:
        single_server = SingleInstanceServer(None, app)
        if not single_server.listen(SINGLE_INSTANCE_SERVER):
            # 同時起動で他プロセスが先に受付を確保した可能性を最優先で確認。
            if send_paths_to_existing_instance(args, timeout_ms=1000):
                return 0

            # 異常終了で名前だけ残った場合のみ掃除して再試行。
            QLocalServer.removeServer(SINGLE_INSTANCE_SERVER)
            if not single_server.listen(SINGLE_INSTANCE_SERVER):
                if send_paths_to_existing_instance(args, timeout_ms=1000):
                    return 0
                single_server = None

    window = MainWindow()
    if single_server is not None:
        single_server.set_window(window)

    window.show()
    if args:
        QTimer.singleShot(0, lambda p=list(args): window.open_external_pdf_paths(p))

    # ローカルサーバーがGCされないよう参照を保持。
    app._fastpdf_single_server = single_server
    return app.exec()


if __name__ == '__main__':
    main()
