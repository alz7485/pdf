import tempfile
from pathlib import Path
from datetime import date
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import fitz
from PySide6.QtCore import Qt, QSize, QRectF, QEvent, QMarginsF, QTimer
from PySide6.QtGui import QImage, QPixmap, QPainter, QPageSize, QPageLayout, QColor, QPen, QFont
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QComboBox, QHBoxLayout, QVBoxLayout,
    QGridLayout, QMessageBox, QSpinBox, QDialog, QGroupBox, QLineEdit, QButtonGroup,
    QGraphicsView, QGraphicsScene, QFormLayout, QDoubleSpinBox, QProgressDialog
)
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

from pdf_core import normalize_path, render_page_image, paper_class_from_rect, parse_page_spec
from ui_common import BUTTON_HEIGHT, BUTTON_WIDTH, framed_label_style, style_push_button

class PrintDialog(QDialog):
    def __init__(self, pdf_path, current_page, favorites_manager, parent=None, settings_store=None):
        super().__init__(parent)
        raw_paths = pdf_path if isinstance(pdf_path, (list, tuple)) else [pdf_path]
        self.source_paths = [normalize_path(p) for p in raw_paths]
        self.multi_file = len(self.source_paths) > 1

        # 単一PDFでは int、複数PDFでは {path: page_index} を受け取る。
        if isinstance(current_page, dict):
            self.current_pages = {
                normalize_path(path): max(0, int(page))
                for path, page in current_page.items()
            }
        else:
            page_index = max(0, int(current_page))
            self.current_pages = {
                path: page_index
                for path in self.source_paths
            }

        self.favorites = favorites_manager
        # 印刷設定は永続保存せず、MainWindowから渡されたセッション辞書だけに保持する。
        # キーは正規化PDFパス。複数選択時に設定を変更した場合は全選択PDFへ同じ設定を反映する。
        self.settings_store = settings_store if isinstance(settings_store, dict) else {}
        self._settings_ready = False
        self._settings_changed = False
        self._temp_pdf_path = None
        self.source_map = []
        self.source_page_counts = {}

        if self.multi_file:
            merged = fitz.open()
            try:
                for path in self.source_paths:
                    src = fitz.open(path)
                    try:
                        self.source_page_counts[path] = src.page_count
                        for i in range(src.page_count):
                            self.source_map.append((path, i))
                        merged.insert_pdf(src)
                    finally:
                        src.close()
                tmp = tempfile.NamedTemporaryFile(prefix='fastpdf_print_', suffix='.pdf', delete=False)
                tmp.close()
                self._temp_pdf_path = tmp.name
                merged.save(self._temp_pdf_path, garbage=3, deflate=True)
            finally:
                merged.close()
            self.pdf_path = self._temp_pdf_path
        else:
            self.pdf_path = self.source_paths[0]
            src = fitz.open(self.pdf_path)
            try:
                self.source_page_counts[self.pdf_path] = src.page_count
                self.source_map = [(self.pdf_path, i) for i in range(src.page_count)]
            finally:
                src.close()

        self.doc = fitz.open(self.pdf_path)
        self.preview_pages = []
        self.preview_pos = 0
        self.preview_zoom_percent = 100
        self.preview_fit_mode = True
        self._preview_panning = False
        self._preview_cache = OrderedDict()
        # 印刷プレビューだけの一時高解像度指定。PDF本体や印刷設定には保存しない。
        self._preview_detail_dpi = {}
        self._print_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='FastPDFPrint')
        self._preview_pan_start_pos = None
        self._preview_pan_start_h = 0
        self._preview_pan_start_v = 0
        self.setWindowTitle('印刷')
        self.resize(1180, 840)
        self.build_ui()
        # UI生成後、最初のプレビューを描く前に前回の一時設定を復元する。
        self.load_session_print_settings()
        self.connect_print_setting_tracking()
        self._settings_ready = True
        self.refresh_preview()
        # ダイアログの実サイズが確定した直後にFITをかけ直す。
        # __init__ 中のfitInView()だけでは、表示前のviewportサイズで計算される場合がある。
        QTimer.singleShot(0, self.fit_preview)

    def closeEvent(self, event):
        # 単一PDFは現在値を保持。複数PDFは実際に設定変更があった時だけ全選択PDFへ反映する。
        if len(self.source_paths) == 1 or self._settings_changed:
            self.store_session_print_settings()
        try:
            self._print_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self.doc.close()
        except Exception:
            pass
        if self._temp_pdf_path:
            try:
                Path(self._temp_pdf_path).unlink(missing_ok=True)
            except Exception:
                pass
        event.accept()

    def _style_preview_button(self, button):
        return style_push_button(button, height=BUTTON_HEIGHT)


    def make_toggle(self, text, group, checked=False):
        b = QPushButton(text)
        b.setCheckable(True)
        b.setChecked(checked)
        b.setMinimumHeight(BUTTON_HEIGHT)
        self._style_preview_button(b)
        group.addButton(b)
        b.clicked.connect(self.refresh_preview)
        return b

    def build_ui(self):
        root = QHBoxLayout(self)
        left = QWidget()
        left.setMinimumWidth(360)
        left.setMaximumWidth(400)
        left_layout = QVBoxLayout(left)
        left_layout.setSpacing(5)

        # 左側だけ QWidget の標準レイアウト余白が追加されると、
        # 右側の「印刷プレビュー」より上端が下がって見えるため除去する。
        left_layout.setContentsMargins(0, 0, 0, 0)

        # 上段：出力する用紙サイズ
        mode_box = QGroupBox('印刷サイズ')
        mode_layout = QHBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)

        # デフォルトは左端の「実サイズ」
        self.mode_actual = self.make_toggle('実サイズ', self.mode_group, True)
        self.mode_a4 = self.make_toggle('A4', self.mode_group)
        self.mode_a3 = self.make_toggle('A3', self.mode_group)

        self.mode_actual.setToolTip('A4原稿はA4、それ以外はA3を基本に印刷します')
        self.mode_a4.setToolTip('対象ページをA4用紙へ収まるように印刷します')
        self.mode_a3.setToolTip('対象ページをA3用紙へ収まるように印刷します')

        mode_layout.addWidget(self.mode_actual)
        mode_layout.addWidget(self.mode_a4)
        mode_layout.addWidget(self.mode_a3)

        # 中段：元ページサイズによる対象絞り込み
        size_filter_box = QGroupBox('サイズ対象')
        size_filter_layout = QHBoxLayout(size_filter_box)
        self.size_filter_group = QButtonGroup(self)
        self.size_filter_group.setExclusive(True)

        # デフォルトは左端の「指定なし」
        self.filter_none = self.make_toggle('指定なし', self.size_filter_group, True)
        self.filter_a4_only = self.make_toggle('A4のみ', self.size_filter_group)
        self.filter_a3_only = self.make_toggle('A3のみ', self.size_filter_group)
        self.filter_a2_a1_only = self.make_toggle('A2,A1のみ', self.size_filter_group)

        self.filter_none.setToolTip('元ページサイズで絞り込みません')
        self.filter_a4_only.setToolTip('元サイズがA4のページだけを対象にします')
        self.filter_a3_only.setToolTip('A4を除くA3以上のページを対象にします')
        self.filter_a2_a1_only.setToolTip('元サイズがA2またはA1のページだけを対象にします')

        size_filter_layout.addWidget(self.filter_none)
        size_filter_layout.addWidget(self.filter_a4_only)
        size_filter_layout.addWidget(self.filter_a3_only)
        size_filter_layout.addWidget(self.filter_a2_a1_only)

        # 下段：ページ範囲
        target_box = QGroupBox('印刷対象')
        target_layout = QGridLayout(target_box)
        self.target_group = QButtonGroup(self)
        self.target_group.setExclusive(True)

        # デフォルトは左端の「全ページ」
        self.target_all = self.make_toggle('全て', self.target_group, True)
        self.target_current = self.make_toggle('現在ページ', self.target_group)
        self.target_fav = self.make_toggle('⭐️', self.target_group)
        self.target_spec = self.make_toggle('ページ指定', self.target_group)

        target_layout.addWidget(self.target_all, 0, 0)
        target_layout.addWidget(self.target_current, 0, 1)
        target_layout.addWidget(self.target_fav, 0, 2)
        target_layout.addWidget(self.target_spec, 0, 3)

        self.spec_edit = QLineEdit()
        self.spec_edit.setPlaceholderText('例: 1-5,8,10-20')
        self.spec_edit.textChanged.connect(self.refresh_preview)
        target_layout.addWidget(self.spec_edit, 1, 0, 1, 4)
        for button in (self.target_all, self.target_current, self.target_fav, self.target_spec):
            button.clicked.connect(self.update_page_spec_state)
        self.update_page_spec_state()

        # 印刷色。デフォルトは「自動」。
        # 自動では元PDFページを軽量判定し、ページごとにカラー/モノクロを決める。
        color_box = QGroupBox('印刷色')
        color_layout = QHBoxLayout(color_box)
        self.color_group = QButtonGroup(self)
        self.color_group.setExclusive(True)

        self.auto_color_btn = self.make_toggle(
            '自動',
            self.color_group,
            True
        )
        self.color_btn = self.make_toggle(
            'カラー',
            self.color_group
        )
        self.mono_btn = self.make_toggle(
            'モノクロ',
            self.color_group
        )

        self.auto_color_btn.setToolTip(
            '元PDFページの色を自動判定し、ページごとにカラー/モノクロを決定します'
        )
        self.color_btn.setToolTip(
            'すべての対象ページをカラーでプレビュー・印刷します'
        )
        self.mono_btn.setToolTip(
            'すべての対象ページをモノクロでプレビュー・印刷します'
        )

        color_layout.addWidget(self.auto_color_btn)
        color_layout.addWidget(self.color_btn)
        color_layout.addWidget(self.mono_btn)

        printer_box = QGroupBox('プリンター')
        printer_form = QFormLayout(printer_box)
        self.printer_combo = QComboBox()
        default_name = QPrinterInfo.defaultPrinter().printerName()
        for p in QPrinterInfo.availablePrinters():
            self.printer_combo.addItem(p.printerName())
        idx = self.printer_combo.findText(default_name)
        if idx >= 0:
            self.printer_combo.setCurrentIndex(idx)
        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 99)
        self.copies_spin.setValue(1)

        self.print_resolution_combo = QComboBox()
        self.print_resolution_combo.addItem('高速 100 DPI', 100)
        self.print_resolution_combo.addItem('標準 150 DPI', 150)
        self.print_resolution_combo.addItem('高品質 200 DPI', 200)
        self.print_resolution_combo.addItem('最高品質 300 DPI', 300)
        self.print_resolution_combo.setCurrentIndex(1)
        self.print_resolution_combo.setToolTip(
            '低いDPIほど印刷データ生成が速くなります。図面の細線や小さい文字は低DPIで粗くなる場合があります。'
        )

        printer_form.addRow('プリンター', self.printer_combo)
        printer_form.addRow('部数', self.copies_spin)
        printer_form.addRow('印刷解像度', self.print_resolution_combo)

        # ----------------------------------------------------
        # 余白
        # ----------------------------------------------------
        margin_box = QGroupBox('余白')
        margin_layout = QGridLayout(margin_box)

        def make_margin_spin(value=5.0):
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 50.0)
            spin.setDecimals(1)
            spin.setSingleStep(1.0)
            spin.setSuffix(' mm')
            spin.setValue(value)
            spin.setFixedWidth(92)
            spin.valueChanged.connect(self.refresh_preview)
            return spin

        self.margin_top_spin = make_margin_spin(5.0)
        self.margin_bottom_spin = make_margin_spin(5.0)
        self.margin_left_spin = make_margin_spin(5.0)
        self.margin_right_spin = make_margin_spin(5.0)

        margin_layout.addWidget(QLabel('上'), 0, 0)
        margin_layout.addWidget(self.margin_top_spin, 0, 1)
        margin_layout.addWidget(QLabel('下'), 0, 2)
        margin_layout.addWidget(self.margin_bottom_spin, 0, 3)
        self.margin_reset_btn = QPushButton('デフォルト')
        self.margin_reset_btn.setToolTip('上下左右の余白をデフォルトの5 mmへ戻します')
        self.margin_reset_btn.setFixedHeight(BUTTON_HEIGHT)
        self._style_preview_button(self.margin_reset_btn)
        self.margin_reset_btn.clicked.connect(self.reset_margins)
        margin_layout.addWidget(self.margin_reset_btn, 0, 4, Qt.AlignRight)

        margin_layout.addWidget(QLabel('左'), 1, 0)
        margin_layout.addWidget(self.margin_left_spin, 1, 1)
        margin_layout.addWidget(QLabel('右'), 1, 2)
        margin_layout.addWidget(self.margin_right_spin, 1, 3)

        # ----------------------------------------------------
        # ヘッダー / フッター
        # ----------------------------------------------------
        hf_box = QGroupBox('ヘッダー / フッター')
        hf_layout = QGridLayout(hf_box)
        hf_layout.setHorizontalSpacing(5)
        hf_layout.setVerticalSpacing(5)

        self.header_edit = QLineEdit()
        self.header_edit.setPlaceholderText('ヘッダー（自由入力）')
        self.footer_edit = QLineEdit()
        self.footer_edit.setPlaceholderText('フッター（自由入力）')

        # 配置は従来機能を残しつつ、横幅を取らないコンパクト表示にする。
        self.header_align = QComboBox()
        self.footer_align = QComboBox()
        for combo in (self.header_align, self.footer_align):
            combo.addItem('左', 'left')
            combo.addItem('中央', 'center')
            combo.addItem('右', 'right')
            combo.setCurrentIndex(1)
            combo.setFixedWidth(68)
            combo.currentIndexChanged.connect(self.refresh_preview)

        self.header_edit.textChanged.connect(self.refresh_preview)
        self.footer_edit.textChanged.connect(self.refresh_preview)
        self.header_edit.installEventFilter(self)
        self.footer_edit.installEventFilter(self)
        self._active_hf_edit = self.header_edit

        hf_layout.addWidget(QLabel('ヘッダー'), 0, 0)
        hf_layout.addWidget(self.header_edit, 0, 1)
        hf_layout.addWidget(self.header_align, 0, 2)
        hf_layout.addWidget(QLabel('フッター'), 1, 0)
        hf_layout.addWidget(self.footer_edit, 1, 1)
        hf_layout.addWidget(self.footer_align, 1, 2)

        # 共通挿入ボタン。見た目は常に同じで、最後に編集した欄へ作用する。
        token_row = QHBoxLayout()
        token_row.setSpacing(4)
        self.hf_token_buttons = []
        for label, token in (
            ('ファイル名', '{filename}'),
            ('ページ', '{page}'),
            ('総ページ', '{pages}'),
            ('日付', '{date}'),
        ):
            button = QPushButton(label)
            button.setCheckable(False)
            button.setFixedHeight(BUTTON_HEIGHT)
            self._style_preview_button(button)
            button.clicked.connect(
                lambda checked=False, t=token: self.toggle_header_footer_token(t)
            )
            token_row.addWidget(button)
            self.hf_token_buttons.append(button)

        self.hf_clear_btn = QPushButton('削除')
        self.hf_clear_btn.setCheckable(False)
        self.hf_clear_btn.setFixedHeight(BUTTON_HEIGHT)
        self.hf_clear_btn.setToolTip('現在コマンド受付中のヘッダー / フッター入力欄を空にします')
        self._style_preview_button(self.hf_clear_btn)
        self.hf_clear_btn.clicked.connect(self.clear_active_header_footer)
        token_row.addWidget(self.hf_clear_btn)
        hf_layout.addLayout(token_row, 2, 0, 1, 3)

        font_row = QHBoxLayout()
        font_row.addWidget(QLabel('フォントサイズ'))
        self.hf_font_size_spin = QSpinBox()
        self.hf_font_size_spin.setRange(1, 24)
        self.hf_font_size_spin.setValue(6)
        self.hf_font_size_spin.setSuffix(' pt')
        self.hf_font_size_spin.setFixedWidth(82)
        self.hf_font_size_spin.valueChanged.connect(self.refresh_preview)
        font_row.addWidget(self.hf_font_size_spin)
        font_row.addStretch(1)
        hf_layout.addLayout(font_row, 3, 0, 1, 3)
        hf_layout.setColumnStretch(1, 1)

        tip = (
            '自由入力できます。入力欄をクリックしてから下のボタンを押すと、'
            'カーソル位置へ情報を挿入します。すでに同じ情報が入っている場合は削除します。'
        )
        self.header_edit.setToolTip(tip)
        self.footer_edit.setToolTip(tip)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.summary_label.setMinimumHeight(92)
        self.summary_label.setStyleSheet(
            'QLabel {'
            '  background: rgba(90, 110, 130, 28);'
            '  border: 1px solid rgba(120, 140, 160, 95);'
            '  border-radius: 8px;'
            '  padding: 9px 11px;'
            '  font-size: 13px;'
            '  line-height: 1.35;'
            '}'
        )
        # 下部アクションは通常ツールボタンより大きくし、
        # 「何を押すボタンか」が一目で分かるよう文字付きにする。
        cancel_btn = QPushButton('キャンセル')
        cancel_btn.setToolTip('印刷せずに閉じます')
        cancel_btn.setFixedSize(118, 44)
        self._style_preview_button(cancel_btn)
        cancel_btn.clicked.connect(self.reject)

        print_btn = QPushButton('🖨️  印刷')
        print_btn.setToolTip('現在の設定で印刷します')
        print_btn.setFixedSize(150, 44)
        print_btn.setDefault(True)
        print_btn.setStyleSheet(
            'QPushButton {'
            '  background: white;'
            '  border: 2px solid #5b8fd9;'
            '  border-radius: 7px;'
            '  padding: 4px 16px;'
            '  font-size: 14px;'
            '  font-weight: 700;'
            '}'
            'QPushButton:hover { background: #eef5ff; }'
            'QPushButton:pressed { background: #dcecff; }'
        )
        print_btn.clicked.connect(self.do_print)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 6, 0, 0)
        bottom.setSpacing(10)
        bottom.addStretch(1)
        bottom.addWidget(cancel_btn)
        bottom.addWidget(print_btn)

        left_layout.addWidget(mode_box)
        left_layout.addWidget(size_filter_box)
        left_layout.addWidget(target_box)
        left_layout.addWidget(color_box)
        left_layout.addWidget(printer_box)
        left_layout.addWidget(margin_box)
        left_layout.addWidget(hf_box)
        left_layout.addWidget(self.summary_label)
        left_layout.addStretch(1)
        left_layout.addLayout(bottom)
        root.addWidget(left, 0)

        preview_box = QGroupBox('印刷プレビュー')
        preview_layout = QVBoxLayout(preview_box)
        self.scene = QGraphicsScene(self)
        self.preview_view = QGraphicsView(self.scene)
        self.preview_view.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.preview_view.viewport().installEventFilter(self)
        self.preview_view.viewport().setMouseTracking(True)

        # 下段：左=ページ情報 / 中央=ページ送り / 右=拡大縮小
        nav = QHBoxLayout()
        nav.setSpacing(4)

        self.preview_info_label = QLabel('[ - / - ]  [ - → - ]  [ - ]')
        self.preview_info_label.setMinimumWidth(300)
        self.preview_info_label.setFixedHeight(BUTTON_HEIGHT)
        self.preview_info_label.setStyleSheet(framed_label_style())

        self.first_preview = QPushButton('⏪')
        self.prev_preview = QPushButton('◀️')
        self.next_preview = QPushButton('▶️')
        self.last_preview = QPushButton('⏩')

        # プレビュー下部は横幅を取りすぎないようコンパクトにする。
        for button in (
            self.first_preview,
            self.prev_preview,
            self.next_preview,
            self.last_preview,
        ):
            button.setFixedSize(BUTTON_WIDTH, BUTTON_HEIGHT)
            self._style_preview_button(button)

        self.preview_page_label = QLabel('0 / 0')
        self.preview_page_label.setFixedSize(68, BUTTON_HEIGHT)
        self.preview_page_label.setAlignment(Qt.AlignCenter)
        self.preview_page_label.setStyleSheet(framed_label_style())

        self.first_preview.setToolTip('印刷対象の先頭ページへ移動します')
        self.prev_preview.setToolTip('前の印刷対象ページへ移動します')
        self.next_preview.setToolTip('次の印刷対象ページへ移動します')
        self.last_preview.setToolTip('印刷対象の最終ページへ移動します')

        self.first_preview.clicked.connect(self.first_preview_page)
        self.prev_preview.clicked.connect(lambda: self.move_preview(-1))
        self.next_preview.clicked.connect(lambda: self.move_preview(1))
        self.last_preview.clicked.connect(self.last_preview_page)

        self.preview_fit_btn = QPushButton('📄')
        self.preview_zoom_out_btn = QPushButton('➖')
        self.preview_zoom_in_btn = QPushButton('➕')
        self.preview_detail_btn = QPushButton('🔎')
        self.preview_zoom_label = QLabel('100%')

        for button in (
            self.preview_fit_btn,
            self.preview_zoom_out_btn,
            self.preview_zoom_in_btn,
            self.preview_detail_btn,
        ):
            button.setFixedSize(BUTTON_WIDTH, BUTTON_HEIGHT)
            self._style_preview_button(button)

        self.preview_zoom_label.setAlignment(Qt.AlignCenter)
        self.preview_zoom_label.setFixedSize(58, BUTTON_HEIGHT)
        self.preview_zoom_label.setStyleSheet(framed_label_style())

        self.preview_fit_btn.setToolTip('印刷プレビュー全体を表示領域にフィットします')
        self.preview_zoom_out_btn.setToolTip('印刷プレビューを10%縮小します（Ctrl + ホイール下）')
        self.preview_zoom_in_btn.setToolTip('印刷プレビューを10%拡大します（Ctrl + ホイール上）')
        self.preview_detail_btn.setToolTip('現在のプレビューページだけを高解像度で再描画します')
        self.preview_zoom_label.setToolTip('印刷プレビューの表示倍率です')

        self.preview_fit_btn.clicked.connect(self.fit_preview)
        self.preview_zoom_out_btn.clicked.connect(lambda: self.change_preview_zoom(-10))
        self.preview_zoom_in_btn.clicked.connect(lambda: self.change_preview_zoom(10))
        self.preview_detail_btn.clicked.connect(self.refresh_current_preview_high_quality)

        # 上段：ビューアと同じ並びで拡大縮小操作を中央配置。
        preview_zoom_bar = QHBoxLayout()
        preview_zoom_bar.setContentsMargins(0, 0, 0, 0)
        preview_zoom_bar.setSpacing(4)
        preview_zoom_bar.addStretch(1)
        preview_zoom_bar.addWidget(self.preview_fit_btn)
        preview_zoom_bar.addWidget(self.preview_zoom_out_btn)
        preview_zoom_bar.addWidget(self.preview_zoom_in_btn)
        preview_zoom_bar.addWidget(self.preview_zoom_label)
        preview_zoom_bar.addWidget(self.preview_detail_btn)
        preview_zoom_bar.addStretch(1)

        nav.addWidget(self.preview_info_label)
        nav.addStretch(1)
        # 下段はページ表示 → 先頭 → 前 → 次 → 最終だけにする。
        nav.addWidget(self.preview_page_label)
        nav.addSpacing(6)
        nav.addWidget(self.first_preview)
        nav.addWidget(self.prev_preview)
        nav.addWidget(self.next_preview)
        nav.addWidget(self.last_preview)
        nav.addStretch(1)

        preview_layout.addLayout(preview_zoom_bar)
        preview_layout.addWidget(self.preview_view, 1)
        preview_layout.addLayout(nav)
        root.addWidget(preview_box, 1)

    def _set_checked_value(self, mapping, value, default_key):
        button = mapping.get(value) or mapping.get(default_key)
        if button is not None:
            button.setChecked(True)

    def capture_print_settings(self):
        """現在の印刷UI状態を、セッション保持用の単純dictへ変換する。"""
        if self.target_current.isChecked():
            target = 'current'
        elif self.target_fav.isChecked():
            target = 'favorite'
        elif self.target_spec.isChecked():
            target = 'spec'
        else:
            target = 'all'

        return {
            'mode': self.selected_mode(),
            'size_filter': self.selected_size_filter(),
            'target': target,
            'page_spec': self.spec_edit.text(),
            'color_mode': self.selected_color_mode(),
            'printer': self.printer_combo.currentText(),
            'copies': int(self.copies_spin.value()),
            'print_resolution': int(self.print_resolution_combo.currentData() or 150),
            'margins': [
                float(self.margin_top_spin.value()),
                float(self.margin_bottom_spin.value()),
                float(self.margin_left_spin.value()),
                float(self.margin_right_spin.value()),
            ],
            'header_text': self.header_edit.text(),
            'header_align': self.header_align.currentData() or 'center',
            'footer_text': self.footer_edit.text(),
            'footer_align': self.footer_align.currentData() or 'center',
            'hf_font_size': int(self.hf_font_size_spin.value()),
        }

    def apply_print_settings(self, state):
        if not isinstance(state, dict):
            return

        widgets = [
            self.mode_actual, self.mode_a4, self.mode_a3,
            self.filter_none, self.filter_a4_only, self.filter_a3_only, self.filter_a2_a1_only,
            self.target_all, self.target_current, self.target_fav, self.target_spec,
            self.auto_color_btn, self.color_btn, self.mono_btn,
            self.spec_edit, self.printer_combo, self.copies_spin, self.print_resolution_combo,
            self.margin_top_spin, self.margin_bottom_spin, self.margin_left_spin, self.margin_right_spin,
            self.header_edit, self.footer_edit, self.header_align, self.footer_align,
            self.hf_font_size_spin,
        ]
        old = [(w, w.blockSignals(True)) for w in widgets]
        try:
            self._set_checked_value(
                {'actual': self.mode_actual, 'a4': self.mode_a4, 'a3': self.mode_a3},
                state.get('mode'), 'actual'
            )
            self._set_checked_value(
                {
                    'none': self.filter_none, 'a4_only': self.filter_a4_only,
                    'a3_only': self.filter_a3_only, 'a2_a1_only': self.filter_a2_a1_only,
                },
                state.get('size_filter'), 'none'
            )
            self._set_checked_value(
                {
                    'all': self.target_all, 'current': self.target_current,
                    'favorite': self.target_fav, 'spec': self.target_spec,
                },
                state.get('target'), 'all'
            )
            self._set_checked_value(
                {'auto': self.auto_color_btn, 'color': self.color_btn, 'mono': self.mono_btn},
                state.get('color_mode'), 'auto'
            )

            self.spec_edit.setText(str(state.get('page_spec', '')))

            printer_name = str(state.get('printer', '') or '')
            idx = self.printer_combo.findText(printer_name)
            if idx >= 0:
                self.printer_combo.setCurrentIndex(idx)
            self.copies_spin.setValue(max(1, min(99, int(state.get('copies', 1)))))

            dpi = int(state.get('print_resolution', 150))
            idx = self.print_resolution_combo.findData(dpi)
            if idx >= 0:
                self.print_resolution_combo.setCurrentIndex(idx)

            margins = state.get('margins', [5.0, 5.0, 5.0, 5.0])
            if not isinstance(margins, (list, tuple)) or len(margins) != 4:
                margins = [5.0, 5.0, 5.0, 5.0]
            for spin, value in zip(
                (self.margin_top_spin, self.margin_bottom_spin, self.margin_left_spin, self.margin_right_spin),
                margins
            ):
                spin.setValue(float(value))

            self.header_edit.setText(str(state.get('header_text', '')))
            self.footer_edit.setText(str(state.get('footer_text', '')))
            for combo, value in (
                (self.header_align, state.get('header_align', 'center')),
                (self.footer_align, state.get('footer_align', 'center')),
            ):
                idx = combo.findData(value)
                combo.setCurrentIndex(idx if idx >= 0 else combo.findData('center'))
            self.hf_font_size_spin.setValue(
                max(1, min(24, int(state.get('hf_font_size', 6))))
            )
        finally:
            for w, previous in old:
                w.blockSignals(previous)
        self.update_page_spec_state()

    def load_session_print_settings(self):
        """選択PDFの先頭ファイルに一時設定があれば、初回プレビュー前に復元する。"""
        if not self.source_paths:
            return
        state = self.settings_store.get(self.source_paths[0])
        if isinstance(state, dict):
            self.apply_print_settings(state)

    def store_session_print_settings(self):
        if not self.source_paths:
            return
        state = self.capture_print_settings()
        # コピーを分けて持たせ、後から1ファイルだけ変更しても他へ波及しないようにする。
        for path in self.source_paths:
            copied = dict(state)
            copied['margins'] = list(state.get('margins', []))
            self.settings_store[path] = copied

    def on_print_setting_changed(self, *args):
        if not self._settings_ready:
            return
        self._settings_changed = True
        self.store_session_print_settings()

    def connect_print_setting_tracking(self):
        # build_ui中の初期値セットを変更扱いにしないため、復元後に接続する。
        for button in (
            self.mode_actual, self.mode_a4, self.mode_a3,
            self.filter_none, self.filter_a4_only, self.filter_a3_only, self.filter_a2_a1_only,
            self.target_all, self.target_current, self.target_fav, self.target_spec,
            self.auto_color_btn, self.color_btn, self.mono_btn,
        ):
            button.clicked.connect(self.on_print_setting_changed)
        for edit in (self.spec_edit, self.header_edit, self.footer_edit):
            edit.textChanged.connect(self.on_print_setting_changed)
        for combo in (self.printer_combo, self.print_resolution_combo, self.header_align, self.footer_align):
            combo.currentIndexChanged.connect(self.on_print_setting_changed)
        for spin in (
            self.copies_spin, self.margin_top_spin, self.margin_bottom_spin,
            self.margin_left_spin, self.margin_right_spin, self.hf_font_size_spin,
        ):
            spin.valueChanged.connect(self.on_print_setting_changed)

    def selected_color_mode(self):
        if self.mono_btn.isChecked():
            return 'mono'
        if self.color_btn.isChecked():
            return 'color'
        return 'auto'

    @staticmethod
    def image_has_meaningful_color(image):
        """
        レンダリング画像に「実質的な色」が含まれるかを軽量判定する。

        - 小さく縮小して判定するため高速
        - R/G/B の差が小さいピクセルはグレー系として扱う
        - スキャン由来の微小な色ノイズだけでカラー判定しにくいよう、
          一定数以上の有彩色ピクセルが必要
        """
        if image is None or image.isNull():
            return False

        sample = image.scaled(
            180,
            180,
            Qt.KeepAspectRatio,
            Qt.FastTransformation
        ).convertToFormat(QImage.Format_RGB32)

        colored = 0
        total = max(1, sample.width() * sample.height())
        required = max(8, int(total * 0.0005))

        for y in range(sample.height()):
            for x in range(sample.width()):
                c = sample.pixelColor(x, y)
                r, g, b = c.red(), c.green(), c.blue()

                max_v = max(r, g, b)
                min_v = min(r, g, b)
                chroma = max_v - min_v

                # グレー/黒/白のアンチエイリアスやJPEGノイズを除外。
                if chroma < 14:
                    continue

                # ほぼ白に近い微小な色味はスキャンノイズの可能性が高い。
                if max_v > 245 and chroma < 24:
                    continue

                colored += 1
                if colored >= required:
                    return True

        return False

    def resolved_color_mode_for_image(self, image):
        mode = self.selected_color_mode()
        if mode == 'auto':
            return (
                'color'
                if self.image_has_meaningful_color(image)
                else 'mono'
            )
        return mode

    def selected_mode(self):
        """出力用紙サイズ。"""
        if self.mode_a4.isChecked():
            return 'a4'
        if self.mode_a3.isChecked():
            return 'a3'
        return 'actual'

    def selected_size_filter(self):
        """元ページサイズによる対象絞り込み。"""
        if self.filter_a4_only.isChecked():
            return 'a4_only'
        if self.filter_a3_only.isChecked():
            return 'a3_only'
        if self.filter_a2_a1_only.isChecked():
            return 'a2_a1_only'
        return 'none'

    def base_target_pages(self):
        """複数PDFでも、対象条件を各ファイルのページ番号へ個別適用する。"""
        result = []

        spec_pages_by_path = {}
        if self.target_spec.isChecked():
            for path in self.source_paths:
                count = self.source_page_counts.get(path, 0)
                spec_pages_by_path[path] = set(
                    parse_page_spec(self.spec_edit.text(), count)
                )

        for merged_index, (source_path, source_page) in enumerate(self.source_map):
            if self.target_current.isChecked():
                # 複数選択時は、各PDFで最後に表示していたページを個別に対象とする。
                current_for_source = self.current_pages.get(source_path, 0)
                if source_page == current_for_source:
                    result.append(merged_index)
            elif self.target_fav.isChecked():
                if source_page in self.favorites.pages(source_path):
                    result.append(merged_index)
            elif self.target_spec.isChecked():
                if source_page in spec_pages_by_path.get(source_path, set()):
                    result.append(merged_index)
            else:
                result.append(merged_index)

        return result

    def page_matches_mode(self, page_index):
        """元ページサイズの絞り込みだけを判定する。"""
        size_filter = self.selected_size_filter()

        if size_filter == 'none':
            return True

        page = self.doc.load_page(page_index)
        cls = paper_class_from_rect(page.rect)

        if size_filter == 'a4_only':
            return cls == 'A4'

        if size_filter == 'a3_only':
            # 既存仕様：A4を除き、A3以上を対象。
            return cls in ('A3', 'A2', 'A1', 'A0', 'A3+')

        if size_filter == 'a2_a1_only':
            return cls in ('A2', 'A1')

        return True

    def compute_preview_pages(self):
        try:
            return [
                page_index
                for page_index in self.base_target_pages()
                if self.page_matches_mode(page_index)
            ]
        except ValueError:
            return []

    def refresh_preview(self):
        self.preview_pages = self.compute_preview_pages()
        if self.preview_pos >= len(self.preview_pages):
            self.preview_pos = max(0, len(self.preview_pages) - 1)
        self.update_summary()
        self.draw_preview()

    def update_page_spec_state(self):
        active = bool(self.target_spec.isChecked())
        self.spec_edit.setReadOnly(not active)
        self.spec_edit.setStyleSheet(
            'QLineEdit { color: #202020; }'
            if active else
            'QLineEdit { color: #9a9a9a; background: #f2f2f2; }'
        )

    def reset_margins(self):
        for spin in (
            self.margin_top_spin,
            self.margin_bottom_spin,
            self.margin_left_spin,
            self.margin_right_spin,
        ):
            spin.blockSignals(True)
            spin.setValue(5.0)
            spin.blockSignals(False)
        self.refresh_preview()
        self.on_print_setting_changed()

    def clear_active_header_footer(self):
        edit = self._active_hf_edit
        if edit not in (self.header_edit, self.footer_edit):
            edit = self.header_edit
            self._active_hf_edit = edit
        edit.clear()
        edit.setFocus(Qt.OtherFocusReason)

    def toggle_header_footer_token(self, token):
        edit = self._active_hf_edit
        if edit not in (self.header_edit, self.footer_edit):
            edit = self.header_edit
            self._active_hf_edit = edit

        text = edit.text()
        token = str(token)
        if token in text:
            # 2回目は、その入力欄にある同じ情報だけを消す。
            cursor = edit.cursorPosition()
            new_text = text.replace(token, '')
            edit.setText(new_text)
            edit.setCursorPosition(min(cursor, len(new_text)))
        else:
            edit.insert(token)
        edit.setFocus(Qt.OtherFocusReason)

    def header_footer_band_mm(self):
        # 文字サイズに応じて帯を確保。従来9ptでは約7mm。
        return max(7.0, float(self.hf_font_size_spin.value()) * 0.45)

    def print_margins_mm(self):
        return (
            float(self.margin_left_spin.value()),
            float(self.margin_top_spin.value()),
            float(self.margin_right_spin.value()),
            float(self.margin_bottom_spin.value()),
        )

    @staticmethod
    def alignment_flag(value):
        if value == 'left':
            return Qt.AlignLeft
        if value == 'right':
            return Qt.AlignRight
        return Qt.AlignHCenter

    def format_header_footer_text(self, template, page_index):
        if not template:
            return ''

        try:
            source_path, source_page = self.source_map[page_index]
        except Exception:
            source_path, source_page = self.pdf_path, page_index

        source_count = self.source_page_counts.get(
            source_path,
            self.doc.page_count
        )
        values = {
            'filename': Path(source_path).stem,
            'page': str(source_page + 1),
            'pages': str(source_count),
            'date': date.today().isoformat(),
        }

        result = str(template)
        for key, value in values.items():
            result = result.replace('{' + key + '}', value)
        return result

    def header_footer_state(self):
        header_text = self.header_edit.text()
        footer_text = self.footer_edit.text()
        return {
            'header_enabled': bool(header_text.strip()),
            'header_text': header_text,
            'header_align': self.header_align.currentData() or 'center',
            'footer_enabled': bool(footer_text.strip()),
            'footer_text': footer_text,
            'footer_align': self.footer_align.currentData() or 'center',
            'font_size': int(self.hf_font_size_spin.value()),
        }

    def update_summary(self):
        size_labels = {
            'actual': '実サイズ',
            'a4': 'A4',
            'a3': 'A3',
        }
        filter_labels = {
            'none': '指定なし',
            'a4_only': 'A4のみ',
            'a3_only': 'A3のみ',
            'a2_a1_only': 'A2,A1のみ',
        }

        size = size_labels.get(self.selected_mode(), '実サイズ')
        size_filter = filter_labels.get(
            self.selected_size_filter(),
            '指定なし'
        )
        color_labels = {
            'auto': '自動',
            'color': 'カラー',
            'mono': 'モノクロ',
        }
        color_label = color_labels.get(
            self.selected_color_mode(),
            '自動'
        )
        prefix = f'選択PDF：{len(self.source_paths)}件\n' if self.multi_file else ''
        ml, mt, mr, mb = self.print_margins_mm()
        hf = self.header_footer_state()
        header_label = 'ON' if hf['header_enabled'] else 'OFF'
        footer_label = 'ON' if hf['footer_enabled'] else 'OFF'

        self.summary_label.setText(
            f'印刷内容\n'
            f'{prefix}'
            f'対象ページ　{len(self.preview_pages)}ページ\n'
            f'出力サイズ　{size}　　印刷色　{color_label}\n'
            f'余白　　　　上{mt:g} / 下{mb:g} / 左{ml:g} / 右{mr:g} mm\n'
            f'ヘッダー　　{header_label}　　フッター　{footer_label}'
        )

    def output_paper_for_page(self, page_index):
        mode = self.selected_mode()

        if mode == 'a3':
            return 'A3'

        if mode == 'a4':
            return 'A4'

        # 実サイズ：既存仕様を維持。A4原稿はA4、その他はA3。
        page = self.doc.load_page(page_index)
        cls = paper_class_from_rect(page.rect)
        return 'A4' if cls == 'A4' else 'A3'

    def refresh_current_preview_high_quality(self):
        """現在の印刷プレビューページだけを一時的に高精細化する。

        固定の高DPIにするとA1/A0で巨大画像になり得るため、
        PDFの実ページ寸法から「長辺およそ4500px」を目安にDPIを決める。
        """
        if not self.preview_pages:
            return
        page_index = self.preview_pages[self.preview_pos]
        page = self.doc.load_page(page_index)
        long_pt = max(float(page.rect.width), float(page.rect.height), 1.0)
        detail_dpi = int(round(4500.0 * 72.0 / long_pt))
        detail_dpi = max(120, min(300, detail_dpi))
        selected_print_dpi = int(self.print_resolution_combo.currentData() or 150)
        detail_dpi = max(detail_dpi, min(300, selected_print_dpi))
        self._preview_detail_dpi[page_index] = detail_dpi

        # 高精細画像をそのままQGraphicsPixmapItemへ持たせ、
        # scene上ではitemのscaleで縮小する。これにより拡大時も元画素を保持できる。
        self.draw_preview()

    def draw_preview(self):
        self.scene.clear()
        if not self.preview_pages:
            self.scene.addText('印刷対象ページがありません')
            self.preview_page_label.setText('0 / 0')
            self.preview_info_label.setText('(-) -→- -')
            self.preview_view.resetTransform()
            self.preview_zoom_label.setText('100%')
            return

        page_index = self.preview_pages[self.preview_pos]
        page = self.doc.load_page(page_index)

        color_mode = self.selected_color_mode()
        preview_dpi = int(self._preview_detail_dpi.get(page_index, 80))
        cache_key = (page_index, preview_dpi, color_mode)
        image = self._preview_cache.get(cache_key)
        resolved_color_mode = color_mode

        if image is None:
            try:
                original_image = render_page_image(
                    self.pdf_path,
                    page_index,
                    preview_dpi
                )
                resolved_color_mode = self.resolved_color_mode_for_image(
                    original_image
                )

                image = original_image
                if (
                    image is not None
                    and resolved_color_mode == 'mono'
                ):
                    image = image.convertToFormat(
                        QImage.Format_Grayscale8
                    )

                self._preview_cache[cache_key] = image
                self._preview_cache.move_to_end(cache_key)
                while len(self._preview_cache) > 12:
                    self._preview_cache.popitem(last=False)
            except Exception:
                image = None
        elif color_mode == 'auto':
            # キャッシュ済み画像だけでは元の色情報が失われる可能性があるため、
            # 表示ラベル用の自動判定は元画像を軽量再取得して確定する。
            try:
                check_image = render_page_image(
                    self.pdf_path,
                    page_index,
                    45
                )
                resolved_color_mode = self.resolved_color_mode_for_image(
                    check_image
                )
            except Exception:
                resolved_color_mode = 'color'
        pix = QPixmap.fromImage(image) if image is not None else QPixmap()

        paper = self.output_paper_for_page(page_index)
        if paper == 'A3':
            paper_w, paper_h = 297.0, 420.0
        elif paper == 'A4':
            paper_w, paper_h = 210.0, 297.0
        else:
            paper_w = min(page.rect.width, page.rect.height)
            paper_h = max(page.rect.width, page.rect.height)

        if page.rect.width > page.rect.height:
            paper_w, paper_h = paper_h, paper_w

        rect = QRectF(0, 0, paper_w * 1.4, paper_h * 1.4)
        item = self.scene.addRect(rect)
        item.setBrush(Qt.white)
        preview_border = QPen(QColor('#666666'))
        preview_border.setWidth(2)
        item.setPen(preview_border)

        # 実印刷と同じ考え方で、余白とヘッダー/フッター領域を
        # プレビューにも反映する。
        ml, mt, mr, mb = self.print_margins_mm()
        sx = rect.width() / max(1.0, paper_w)
        sy = rect.height() / max(1.0, paper_h)

        left_px = ml * sx
        right_px = mr * sx
        top_px = mt * sy
        bottom_px = mb * sy

        hf = self.header_footer_state()
        text_band_mm = self.header_footer_band_mm()
        header_band = text_band_mm * sy if hf['header_enabled'] else 0.0
        footer_band = text_band_mm * sy if hf['footer_enabled'] else 0.0

        content_target = QRectF(
            rect.x() + left_px,
            rect.y() + top_px + header_band,
            max(1.0, rect.width() - left_px - right_px),
            max(
                1.0,
                rect.height()
                - top_px
                - bottom_px
                - header_band
                - footer_band
            )
        )

        if not pix.isNull():
            # ここでpixmap自体を小さく作り直すと、高DPIで再描画しても
            # 画素を捨ててしまい🔎の効果がなくなる。元pixmapを保持したまま
            # QGraphicsItemのscaleだけで用紙内へ収める。
            scale_factor = min(
                content_target.width() / max(1.0, float(pix.width())),
                content_target.height() / max(1.0, float(pix.height()))
            )
            shown_w = pix.width() * scale_factor
            shown_h = pix.height() * scale_factor
            pitem = self.scene.addPixmap(pix)
            pitem.setScale(scale_factor)
            pitem.setPos(
                content_target.x()
                + (content_target.width() - shown_w) / 2,
                content_target.y()
                + (content_target.height() - shown_h) / 2
            )

        preview_font = QFont()
        preview_font.setPointSize(hf['font_size'])

        if hf['header_enabled']:
            header_rect = QRectF(
                rect.x() + left_px,
                rect.y() + top_px,
                max(1.0, rect.width() - left_px - right_px),
                max(1.0, header_band)
            )
            header_text = self.format_header_footer_text(
                hf['header_text'],
                page_index
            )
            t = self.scene.addText(header_text, preview_font)
            br = t.boundingRect()
            align = hf['header_align']
            if align == 'left':
                tx = header_rect.left()
            elif align == 'right':
                tx = header_rect.right() - br.width()
            else:
                tx = header_rect.center().x() - br.width() / 2
            ty = header_rect.center().y() - br.height() / 2
            t.setPos(tx, ty)

        if hf['footer_enabled']:
            footer_rect = QRectF(
                rect.x() + left_px,
                rect.bottom() - bottom_px - footer_band,
                max(1.0, rect.width() - left_px - right_px),
                max(1.0, footer_band)
            )
            footer_text = self.format_header_footer_text(
                hf['footer_text'],
                page_index
            )
            t = self.scene.addText(footer_text, preview_font)
            br = t.boundingRect()
            align = hf['footer_align']
            if align == 'left':
                tx = footer_rect.left()
            elif align == 'right':
                tx = footer_rect.right() - br.width()
            else:
                tx = footer_rect.center().x() - br.width() / 2
            ty = footer_rect.center().y() - br.height() / 2
            t.setPos(tx, ty)

        cls = paper_class_from_rect(page.rect)
        source_path, source_page = self.source_map[page_index]
        filename = Path(source_path).stem
        source_count = self.source_page_counts.get(source_path, 0)

        # 左下は常に「その元PDF内」でのページ番号 / 総ページ数。
        color_info = ''
        if color_mode == 'auto':
            auto_label = (
                'カラー'
                if resolved_color_mode == 'color'
                else 'モノクロ'
            )
            color_info = f'  [ 自動→{auto_label} ]'

        self.preview_info_label.setText(
            f'[ {source_page + 1} / {source_count} ]  '
            f'[ {cls} → {paper} ]  '
            f'[ {filename} ]'
            f'{color_info}'
        )

        # 中央のページ表示は印刷プレビュー対象全体での位置。
        self.preview_page_label.setText(
            f'{self.preview_pos + 1} / {len(self.preview_pages)}'
        )

        self.scene.setSceneRect(
            self.scene.itemsBoundingRect().adjusted(-10, -10, 10, 10)
        )

        if self.preview_fit_mode:
            self.fit_preview()
        else:
            self.apply_preview_zoom()

    def move_preview(self, delta):
        if not self.preview_pages:
            return
        self.preview_pos = max(
            0,
            min(len(self.preview_pages) - 1, self.preview_pos + delta)
        )
        self.draw_preview()

    def first_preview_page(self):
        if not self.preview_pages:
            return
        self.preview_pos = 0
        self.draw_preview()

    def last_preview_page(self):
        if not self.preview_pages:
            return
        self.preview_pos = len(self.preview_pages) - 1
        self.draw_preview()

    def fit_preview(self):
        if not self.preview_pages:
            return
        self.preview_fit_mode = True
        self.preview_view.resetTransform()
        self.preview_view.fitInView(
            self.scene.sceneRect(),
            Qt.KeepAspectRatio
        )
        self.preview_zoom_percent = 100
        self.preview_zoom_label.setText('FIT')

    def apply_preview_zoom(self):
        if not self.preview_pages:
            return
        self.preview_view.resetTransform()
        scale = self.preview_zoom_percent / 100.0
        self.preview_view.scale(scale, scale)
        self.preview_zoom_label.setText(
            f'{self.preview_zoom_percent}%'
        )

    def change_preview_zoom(self, delta):
        if not self.preview_pages:
            return
        self.preview_fit_mode = False
        self.preview_zoom_percent = max(
            20,
            min(500, self.preview_zoom_percent + delta)
        )
        self.apply_preview_zoom()

    def eventFilter(self, obj, event):
        if obj in (getattr(self, 'header_edit', None), getattr(self, 'footer_edit', None)):
            if event.type() == QEvent.FocusIn:
                self._active_hf_edit = obj
            return super().eventFilter(obj, event)

        if obj is self.preview_view.viewport():
            if event.type() == QEvent.Wheel:
                delta = event.angleDelta().y()
                if delta == 0:
                    return True

                if event.modifiers() & Qt.ControlModifier:
                    self.change_preview_zoom(10 if delta > 0 else -10)
                else:
                    self.move_preview(-1 if delta > 0 else 1)
                return True

            if (
                event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.MiddleButton
            ):
                self._preview_panning = True
                self._preview_pan_start_pos = event.position().toPoint()
                self._preview_pan_start_h = (
                    self.preview_view.horizontalScrollBar().value()
                )
                self._preview_pan_start_v = (
                    self.preview_view.verticalScrollBar().value()
                )
                self.preview_view.viewport().setCursor(
                    Qt.ClosedHandCursor
                )
                return True

            if (
                event.type() == QEvent.MouseMove
                and self._preview_panning
                and self._preview_pan_start_pos is not None
            ):
                pos = event.position().toPoint()
                delta = pos - self._preview_pan_start_pos

                self.preview_view.horizontalScrollBar().setValue(
                    self._preview_pan_start_h - delta.x()
                )
                self.preview_view.verticalScrollBar().setValue(
                    self._preview_pan_start_v - delta.y()
                )
                return True

            if (
                event.type() == QEvent.MouseButtonRelease
                and event.button() == Qt.MiddleButton
            ):
                self._preview_panning = False
                self._preview_pan_start_pos = None
                self.preview_view.viewport().unsetCursor()
                return True

        return super().eventFilter(obj, event)

    def make_page_size(self, paper_name):
        return QPageSize(QPageSize.A3 if paper_name == 'A3' else QPageSize.A4)

    def do_print(self):
        """PDFを実プリンターへ送る。

        Windowsのプリンタードライバーでは、1つのQPrinterジョブ開始後に
        A4/A3や縦横を切り替えてもDEVMODE側へ反映されない機種がある。
        そのため、連続する同一レイアウト単位でジョブを分割し、各ジョブの
        QPainter.begin()前に用紙サイズ・向き・解像度を確定する。

        また、従来はプリンターの高DPI座標サイズまでQPixmapを事前拡大して
        いたため非常に重かった。現在はレンダリング済みQImageを直接QPainterへ
        渡して拡大描画し、巨大な中間Pixmapを作らない。
        """
        self.store_session_print_settings()
        if not self.preview_pages:
            QMessageBox.warning(self, '印刷', '印刷対象ページがありません。')
            return

        printer_name = self.printer_combo.currentText().strip()
        if not printer_name:
            QMessageBox.warning(self, '印刷', 'プリンターが選択されていません。')
            return

        total = len(self.preview_pages)
        render_dpi = int(self.print_resolution_combo.currentData() or 150)

        # ----------------------------------------------------
        # ページごとの物理用紙と向きを確定
        # ----------------------------------------------------
        page_layouts = []
        for page_index in self.preview_pages:
            page = self.doc.load_page(page_index)
            paper = self.output_paper_for_page(page_index)
            if not paper:
                raise RuntimeError('出力用紙サイズを判定できませんでした。')
            orientation = (
                QPageLayout.Landscape
                if float(page.rect.width) > float(page.rect.height)
                else QPageLayout.Portrait
            )
            page_layouts.append((page_index, paper, orientation))

        # 連続する同じ「用紙＋向き」は1つのスプールジョブにまとめる。
        # A4/A3または縦横が変わる箇所だけジョブを分けることで、
        # Windowsドライバーの既定用紙への固定を避ける。
        groups = []
        for page_index, paper, orientation in page_layouts:
            key = (paper, orientation)
            if not groups or groups[-1]['key'] != key:
                groups.append({'key': key, 'pages': []})
            groups[-1]['pages'].append(page_index)

        progress = QProgressDialog(
            '印刷を準備しています...',
            'キャンセル',
            0,
            total,
            self
        )
        progress.setWindowTitle('印刷中')
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        progress.setMinimumWidth(430)
        progress.show()
        QApplication.processEvents()

        selected_color = self.selected_color_mode()
        copies = self.copies_spin.value()
        completed = 0
        cancelled = False
        active_printer = None
        active_painter = None

        # ページ画像のレンダリングは1ページ先行させる。
        next_future = None
        if self.preview_pages:
            next_future = self._print_executor.submit(
                render_page_image,
                self.pdf_path,
                self.preview_pages[0],
                render_dpi
            )

        def build_printer(paper, orientation):
            printer = QPrinter(QPrinter.HighResolution)
            printer.setPrinterName(printer_name)
            if not printer.isValid():
                raise RuntimeError(f'プリンター「{printer_name}」を使用できません。')

            # UIの100/150/200/300 DPIは「PDFを画像化する解像度」にだけ使う。
            # 実プリンターへ setResolution() で150 DPI等を強制すると、
            # その解像度を受け付けないWindowsドライバーではQPainter.begin()が
            # 失敗することがあるため、プリンターはドライバーのネイティブ解像度を使う。
            printer.setCopyCount(copies)

            try:
                printer.setPaperSource(QPrinter.Auto)
            except Exception:
                pass

            if selected_color == 'mono':
                try:
                    printer.setColorMode(QPrinter.GrayScale)
                except Exception:
                    pass
            else:
                try:
                    printer.setColorMode(QPrinter.Color)
                except Exception:
                    pass

            # 用紙サイズと向きを、ジョブ開始前に個別に設定する。
            # 一部のWindowsドライバーは setPageLayout() の戻り値をFalseにしても
            # 実際には設定を受け付けるため、戻り値だけを理由に印刷を中断しない。
            page_size = self.make_page_size(paper)
            try:
                printer.setPageSize(page_size)
            except Exception:
                pass
            try:
                printer.setPageOrientation(orientation)
            except Exception:
                pass

            # Qt/ドライバーによっては上の個別設定よりPageLayoutの方が効くため、
            # こちらもベストエフォートで設定する。ただしFalseでも中断しない。
            try:
                layout = QPageLayout(
                    page_size,
                    orientation,
                    QMarginsF(0, 0, 0, 0),
                    QPageLayout.Millimeter
                )
                printer.setPageLayout(layout)
            except Exception:
                pass

            return printer

        try:
            global_seq = 0
            for group_index, group in enumerate(groups):
                QApplication.processEvents()
                if progress.wasCanceled():
                    cancelled = True
                    break

                paper, orientation = group['key']
                ori_text = '横' if orientation == QPageLayout.Landscape else '縦'
                progress.setLabelText(
                    f'印刷ジョブを準備中...  {paper} {ori_text}\n'
                    f'{completed} / {total} ページ完了'
                )
                QApplication.processEvents()

                active_printer = build_printer(paper, orientation)
                active_painter = QPainter()
                if not active_painter.begin(active_printer):
                    raise RuntimeError(
                        f'プリンターを開始できませんでした。\n'
                        f'用紙: {paper} {ori_text}'
                    )

                try:
                    for in_group_seq, page_index in enumerate(group['pages']):
                        QApplication.processEvents()
                        if progress.wasCanceled():
                            cancelled = True
                            try:
                                active_printer.abort()
                            except Exception:
                                pass
                            break

                        # 同じ用紙・向きのジョブ内だけnewPageを使う。
                        # レイアウト切替は一切行わない。
                        if in_group_seq > 0:
                            if not active_printer.newPage():
                                raise RuntimeError(
                                    f'{global_seq + 1}ページ目の印刷ページを開始できませんでした。'
                                )

                        page_no = page_index + 1
                        progress.setLabelText(
                            f'印刷データを作成中...  {paper} {ori_text}\n'
                            f'{global_seq + 1} / {total} ページ'
                            f'  （PDF {page_no}ページ）'
                        )
                        progress.setValue(completed)
                        QApplication.processEvents()

                        # 現在ページのレンダリング結果を受け取る。
                        image = next_future.result() if next_future is not None else None
                        if image is None or image.isNull():
                            raise RuntimeError(
                                f'PDF {page_no}ページの印刷画像を作成できませんでした。'
                            )

                        resolved_color_mode = self.resolved_color_mode_for_image(image)
                        if resolved_color_mode == 'mono':
                            image = image.convertToFormat(QImage.Format_Grayscale8)

                        # 次のページをバックグラウンドで先行レンダリング。
                        next_global_seq = global_seq + 1
                        if next_global_seq < total:
                            next_page = self.preview_pages[next_global_seq]
                            next_future = self._print_executor.submit(
                                render_page_image,
                                self.pdf_path,
                                next_page,
                                render_dpi
                            )
                        else:
                            next_future = None

                        QApplication.processEvents()
                        if progress.wasCanceled():
                            cancelled = True
                            try:
                                active_printer.abort()
                            except Exception:
                                pass
                            break

                        # このジョブはbegin()前に用紙と向きを固定済み。
                        target = active_printer.pageLayout().paintRectPixels(
                            active_printer.resolution()
                        )

                        px_per_mm = active_printer.resolution() / 25.4
                        ml, mt, mr, mb = self.print_margins_mm()
                        left_px = int(round(ml * px_per_mm))
                        top_px = int(round(mt * px_per_mm))
                        right_px = int(round(mr * px_per_mm))
                        bottom_px = int(round(mb * px_per_mm))

                        hf = self.header_footer_state()
                        text_band_px = int(
                            round(self.header_footer_band_mm() * px_per_mm)
                        )
                        header_band = text_band_px if hf['header_enabled'] else 0
                        footer_band = text_band_px if hf['footer_enabled'] else 0

                        content_x = target.x() + left_px
                        content_y = target.y() + top_px + header_band
                        content_w = max(
                            1,
                            target.width() - left_px - right_px
                        )
                        content_h = max(
                            1,
                            target.height()
                            - top_px
                            - bottom_px
                            - header_band
                            - footer_band
                        )

                        # 巨大なQPixmapへ事前拡大しない。
                        # アスペクト比だけ計算し、QImageを直接プリンターへ描画する。
                        iw = max(1, image.width())
                        ih = max(1, image.height())
                        scale = min(content_w / iw, content_h / ih)
                        draw_w = max(1.0, iw * scale)
                        draw_h = max(1.0, ih * scale)
                        x = content_x + (content_w - draw_w) / 2.0
                        y = content_y + (content_h - draw_h) / 2.0

                        active_painter.save()
                        try:
                            active_painter.setRenderHint(
                                QPainter.SmoothPixmapTransform,
                                True
                            )
                            active_painter.drawImage(
                                QRectF(x, y, draw_w, draw_h),
                                image,
                                QRectF(0, 0, iw, ih)
                            )
                        finally:
                            active_painter.restore()

                        # ヘッダー / フッター
                        old_font = active_painter.font()
                        print_font = QFont(old_font)
                        print_font.setPointSize(hf['font_size'])
                        active_painter.setFont(print_font)
                        text_flags_common = Qt.AlignVCenter | Qt.TextSingleLine

                        if hf['header_enabled']:
                            header_rect = QRectF(
                                target.x() + left_px,
                                target.y() + top_px,
                                content_w,
                                max(1, header_band)
                            )
                            header_text = self.format_header_footer_text(
                                hf['header_text'],
                                page_index
                            )
                            active_painter.drawText(
                                header_rect,
                                text_flags_common
                                | self.alignment_flag(hf['header_align']),
                                header_text
                            )

                        if hf['footer_enabled']:
                            footer_rect = QRectF(
                                target.x() + left_px,
                                target.y()
                                + target.height()
                                - bottom_px
                                - footer_band,
                                content_w,
                                max(1, footer_band)
                            )
                            footer_text = self.format_header_footer_text(
                                hf['footer_text'],
                                page_index
                            )
                            active_painter.drawText(
                                footer_rect,
                                text_flags_common
                                | self.alignment_flag(hf['footer_align']),
                                footer_text
                            )

                        active_painter.setFont(old_font)

                        completed += 1
                        global_seq += 1
                        progress.setValue(completed)
                        QApplication.processEvents()

                    if cancelled:
                        break
                finally:
                    if active_painter is not None and active_painter.isActive():
                        active_painter.end()
                    active_painter = None
                    active_printer = None

            progress.close()

            if cancelled:
                QMessageBox.information(
                    self,
                    '印刷キャンセル',
                    '印刷をキャンセルしました。\n'
                    '※キャンセル時点ですでにプリンターへ送信済みのページは、'
                    '印刷される場合があります。'
                )
                return

            QMessageBox.information(
                self,
                '印刷',
                f'{completed}ページの印刷データを送信しました。\n'
                f'用紙・向きの切替単位：{len(groups)}ジョブ'
            )

        except Exception as e:
            try:
                if active_printer is not None:
                    active_printer.abort()
            except Exception:
                pass
            try:
                if active_painter is not None and active_painter.isActive():
                    active_painter.end()
            except Exception:
                pass

            progress.close()
            QMessageBox.critical(
                self,
                '印刷',
                f'印刷中にエラーが発生しました。\n{e}'
            )
            return

