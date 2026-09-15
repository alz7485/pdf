import sys
import os
import json
import math
from dataclasses import dataclass
from collections import OrderedDict
from pathlib import Path

import fitz

from PySide6.QtCore import (
    Qt, QSize, Signal, QEvent, QTimer, QMimeData, QPointF, QRectF, QUrl
)
from PySide6.QtGui import (
    QImage, QPixmap, QTransform, QColor, QBrush, QDrag, QPainter, QPen, QFont, QPolygonF, QIcon
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QToolButton, QHBoxLayout, QVBoxLayout,
    QSplitter, QFileDialog, QMessageBox, QDialog,
    QLineEdit, QScrollArea, QAbstractItemView, QListView,
    QInputDialog, QFrame, QStackedWidget, QSizePolicy, QButtonGroup, QStyledItemDelegate, QStyle, QColorDialog
)

from pdf_core import (
    APP_DIR, PageEntry, get_temp_dir, is_temp_pdf_path, make_temp_pdf_path,
    normalize_path, open_temp_folder, parse_page_spec, render_page_image,
    safe_json_load, safe_json_save,
)
from ui_common import ANNOTATION_BUTTON_WIDTH, make_tool_button, reveal_pdf_location


def get_editor_settings_path():
    return APP_DIR / 'editor_settings.json'


def load_editor_settings():
    data = safe_json_load(get_editor_settings_path(), {})
    return data if isinstance(data, dict) else {}


def save_editor_settings(data):
    return safe_json_save(get_editor_settings_path(), data)


PDF_FONT_CHOICES = [
    ('Helvetica', 'Helv'),
    ('Helvetica Bold', 'HeBo'),
    ('Helvetica Oblique', 'HeOb'),
    ('Helvetica Bold Oblique', 'HeBI'),
    ('Times Roman', 'TiRo'),
    ('Times Bold', 'TiBo'),
    ('Times Italic', 'TiIt'),
    ('Times Bold Italic', 'TiBI'),
    ('Courier', 'Cour'),
    ('Courier Bold', 'CoBo'),
    ('Courier Oblique', 'CoOb'),
    ('Courier Bold Oblique', 'CoBI'),
]
PDF_FONT_LABEL_TO_NAME = dict(PDF_FONT_CHOICES)
PDF_FONT_NAME_TO_LABEL = {v: k for k, v in PDF_FONT_CHOICES}


def normalize_pdf_font_name(name):
    raw = str(name or '').strip()
    if raw in PDF_FONT_NAME_TO_LABEL:
        return raw
    low = raw.lower().replace('-', ' ')
    bold = 'bold' in low
    italic = any(x in low for x in ('italic', 'oblique'))
    if 'courier' in low:
        return 'CoBI' if bold and italic else 'CoBo' if bold else 'CoOb' if italic else 'Cour'
    if 'times' in low:
        return 'TiBI' if bold and italic else 'TiBo' if bold else 'TiIt' if italic else 'TiRo'
    return 'HeBI' if bold and italic else 'HeBo' if bold else 'HeOb' if italic else 'Helv'


def qfont_for_pdf_font(font_name, point_size):
    name = normalize_pdf_font_name(font_name)
    if name.startswith('Ti'):
        family = 'Times New Roman'
    elif name.startswith('Co'):
        family = 'Courier New'
    else:
        family = 'Arial'
    f = QFont(family)
    f.setPointSize(max(1, int(round(point_size or 12))))
    f.setBold(name in ('HeBo', 'HeBI', 'TiBo', 'TiBI', 'CoBo', 'CoBI'))
    f.setItalic(name in ('HeOb', 'HeBI', 'TiIt', 'TiBI', 'CoOb', 'CoBI'))
    return f


@dataclass
class AnnotationItem:
    kind: str
    x1: float
    y1: float
    x2: float
    y2: float
    text: str = ''
    angle: float = 0.0
    color: str = '#ff0000'
    fill_white: bool = False
    font_size: float = 12.0
    font_name: str = 'Helv'

    def copy(self):
        return AnnotationItem(
            self.kind,
            self.x1,
            self.y1,
            self.x2,
            self.y2,
            self.text,
            self.angle,
            self.color,
            self.fill_white,
            self.font_size,
            self.font_name,
        )


class AnnotationCanvas(QLabel):
    """単一ページ表示上の朱書きレイヤー。座標は表示ページに対する0～1正規化値。"""
    annotationChanged = Signal()
    objectCreated = Signal()
    requestText = Signal(float, float)
    requestWhiteText = Signal(float, float)
    requestPlainText = Signal(float, float)
    requestEditText = Signal(int)
    requestThumbnail = Signal()

    HANDLE_SIZE = 9.0
    HANDLE_HIT = 10.0
    ROTATE_HANDLE_DISTANCE = 28.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.annotations = []
        self.tool = 'select'
        self.current_color = '#ff0000'
        self.current_fill_white = False

        # primary selection + multi selection
        self.selected_index = -1
        self.selected_indices = set()

        self._drag_start = None
        self._drag_current = None
        self._moving = False
        self._move_origin = None
        self._move_items_origin = {}
        self._resizing = False
        self._resize_handle = None
        self._resize_item_origin = None
        self._rotating = False
        self._rotate_item_origin = None
        self._rotate_start_mouse_angle = 0.0
        self._rotate_start_item_angle = 0.0
        self.setMouseTracking(True)

    def set_drawing_style(self, color=None, fill_white=None):
        if color is not None:
            q = QColor(str(color))
            if q.isValid():
                self.current_color = q.name()
        if fill_white is not None:
            self.current_fill_white = bool(fill_white)
        self.update()

    def set_tool(self, tool):
        # 作成ツールを切り替えた時だけ明示的に選択解除する。
        # 作成直後の選択中は tool 自体は元の作成ツールのまま保持する。
        tool_changed = tool != self.tool
        self.tool = tool
        self._drag_start = None
        self._drag_current = None
        self._moving = False
        self._resizing = False
        self._resize_handle = None
        self._rotating = False
        if tool_changed:
            self.clear_selection(update=False)
        self.setCursor(Qt.ArrowCursor if tool == 'select' else Qt.CrossCursor)
        self.update()

    def set_annotations(self, annotations):
        self.annotations = annotations if annotations is not None else []
        valid = {i for i in self.selected_indices if 0 <= i < len(self.annotations)}
        self.selected_indices = valid
        if self.selected_index not in valid:
            self.selected_index = max(valid) if valid else -1
        self.update()

    def set_selected_indices(self, indices, primary=None):
        valid = {int(i) for i in indices if 0 <= int(i) < len(self.annotations)}
        self.selected_indices = valid
        if primary is not None and primary in valid:
            self.selected_index = int(primary)
        else:
            self.selected_index = max(valid) if valid else -1
        self._moving = False
        self._resizing = False
        self._rotating = False
        self.update()

    def clear_selection(self, update=True):
        self.selected_index = -1
        self.selected_indices.clear()
        self._moving = False
        self._move_items_origin = {}
        self._resizing = False
        self._resize_handle = None
        self._rotating = False
        if update:
            self.update()

    def _norm(self, pos):
        if self.width() <= 0 or self.height() <= 0:
            return 0.0, 0.0
        return (
            max(0.0, min(1.0, pos.x() / self.width())),
            max(0.0, min(1.0, pos.y() / self.height())),
        )

    def _pixel_rect(self, a):
        x1, x2 = sorted((a.x1 * self.width(), a.x2 * self.width()))
        y1, y2 = sorted((a.y1 * self.height(), a.y2 * self.height()))
        return QRectF(x1, y1, max(1.0, x2-x1), max(1.0, y2-y1))

    def _rotate_point(self, pt, center, angle_deg):
        if not angle_deg:
            return QPointF(pt)
        rad = math.radians(angle_deg)
        cs, sn = math.cos(rad), math.sin(rad)
        dx, dy = pt.x() - center.x(), pt.y() - center.y()
        return QPointF(
            center.x() + dx * cs - dy * sn,
            center.y() + dx * sn + dy * cs
        )

    def _unrotate_point(self, pt, center, angle_deg):
        return self._rotate_point(pt, center, -angle_deg)

    def _item_center_px(self, a):
        return QPointF(
            (a.x1 + a.x2) * 0.5 * self.width(),
            (a.y1 + a.y2) * 0.5 * self.height()
        )

    def _handle_points(self, a):
        center = self._item_center_px(a)
        if a.kind in ('line', 'arrow'):
            p1 = QPointF(a.x1 * self.width(), a.y1 * self.height())
            p2 = QPointF(a.x2 * self.width(), a.y2 * self.height())
            rp1 = self._rotate_point(p1, center, a.angle)
            rp2 = self._rotate_point(p2, center, a.angle)
            dx = rp2.x() - rp1.x()
            dy = rp2.y() - rp1.y()
            length = max(1.0, math.hypot(dx, dy))
            rotate = QPointF(
                center.x() - (dy / length) * self.ROTATE_HANDLE_DISTANCE,
                center.y() + (dx / length) * self.ROTATE_HANDLE_DISTANCE,
            )
            return {'p1': rp1, 'p2': rp2, 'rotate': rotate}

        r = self._pixel_rect(a)
        pts = {
            'tl': r.topLeft(), 'tr': r.topRight(),
            'bl': r.bottomLeft(), 'br': r.bottomRight(),
        }
        result = {k: self._rotate_point(v, center, a.angle) for k, v in pts.items()}
        top_center = QPointF(r.center().x(), r.top() - self.ROTATE_HANDLE_DISTANCE)
        result['rotate'] = self._rotate_point(top_center, center, a.angle)
        return result

    def _hit_handle(self, pos):
        # リサイズ・回転ハンドルは単一選択時のみ。
        if len(self.selected_indices) != 1 or self.selected_index not in self.selected_indices:
            return None
        a = self.annotations[self.selected_index]
        px, py = pos.x(), pos.y()
        for name, pt in self._handle_points(a).items():
            if abs(px - pt.x()) <= self.HANDLE_HIT and abs(py - pt.y()) <= self.HANDLE_HIT:
                return name
        return None

    def _hit_test(self, pos):
        px, py = pos.x(), pos.y()
        tol = 8.0
        for i in range(len(self.annotations)-1, -1, -1):
            a = self.annotations[i]
            center = self._item_center_px(a)
            local = self._unrotate_point(QPointF(px, py), center, a.angle)
            lpx, lpy = local.x(), local.y()
            if a.kind in ('rect', 'ellipse', 'text', 'text_white', 'text_plain'):
                r = self._pixel_rect(a).adjusted(-tol, -tol, tol, tol)
                if r.contains(QPointF(lpx, lpy)):
                    return i
            else:
                x1, y1 = a.x1*self.width(), a.y1*self.height()
                x2, y2 = a.x2*self.width(), a.y2*self.height()
                dx, dy = x2-x1, y2-y1
                denom = dx*dx + dy*dy
                if denom <= 1e-6:
                    dist = ((lpx-x1)**2 + (lpy-y1)**2) ** 0.5
                else:
                    t = max(0.0, min(1.0, ((lpx-x1)*dx + (lpy-y1)*dy)/denom))
                    qx, qy = x1+t*dx, y1+t*dy
                    dist = ((lpx-qx)**2 + (lpy-qy)**2) ** 0.5
                if dist <= tol:
                    return i
        return -1

    def _editing_selection(self):
        return bool(self.selected_indices)

    def _update_select_cursor(self, pos):
        if self._moving or self._resizing or self._rotating:
            return
        if self._editing_selection():
            handle = self._hit_handle(pos)
            if handle == 'rotate':
                self.setCursor(Qt.CrossCursor)
            elif handle in ('p1', 'p2'):
                self.setCursor(Qt.CrossCursor)
            elif handle in ('tl', 'br'):
                self.setCursor(Qt.SizeFDiagCursor)
            elif handle in ('tr', 'bl'):
                self.setCursor(Qt.SizeBDiagCursor)
            elif self._hit_test(pos) in self.selected_indices:
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.ArrowCursor if self.tool == 'select' else Qt.CrossCursor)
            return
        self.setCursor(Qt.ArrowCursor if self.tool == 'select' else Qt.CrossCursor)

    def _begin_move(self, nx, ny):
        self._moving = True
        self._move_origin = (nx, ny)
        self._move_items_origin = {
            i: (
                self.annotations[i].x1, self.annotations[i].y1,
                self.annotations[i].x2, self.annotations[i].y2
            )
            for i in self.selected_indices
            if 0 <= i < len(self.annotations)
        }

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        nx, ny = self._norm(event.position())
        ctrl = bool(event.modifiers() & Qt.ControlModifier)
        idx = self._hit_test(event.position())

        # Ctrl+クリックは現在の作成ツールに関係なく選択トグル。
        # 複数選択もここで行う。
        if ctrl:
            if idx >= 0:
                selected = set(self.selected_indices)
                if idx in selected:
                    selected.remove(idx)
                    primary = max(selected) if selected else -1
                else:
                    selected.add(idx)
                    primary = idx
                self.set_selected_indices(selected, primary)
            event.accept()
            return

        # 選択中は一時的に編集状態。
        if self._editing_selection():
            handle = self._hit_handle(event.position())
            if handle is not None and len(self.selected_indices) == 1:
                if handle == 'rotate':
                    a = self.annotations[self.selected_index]
                    center = self._item_center_px(a)
                    self._rotating = True
                    self._rotate_item_origin = a.copy()
                    self._rotate_start_item_angle = float(a.angle)
                    self._rotate_start_mouse_angle = math.degrees(
                        math.atan2(
                            event.position().y() - center.y(),
                            event.position().x() - center.x()
                        )
                    )
                else:
                    self._resizing = True
                    self._resize_handle = handle
                    self._resize_item_origin = self.annotations[self.selected_index].copy()
                event.accept()
                return

            if idx in self.selected_indices:
                self.selected_index = idx
                self._begin_move(nx, ny)
                self.update()
                event.accept()
                return

            # 選択外/空白を通常クリックしたら選択解除。
            # 作成ツール中はこのクリックでは描画を始めず、次クリックから元ツールへ戻る。
            self.clear_selection()
            if self.tool == 'select' and idx >= 0:
                self.set_selected_indices({idx}, idx)
                self._begin_move(nx, ny)
            event.accept()
            return

        # 純粋な選択モード。
        if self.tool == 'select':
            if idx >= 0:
                self.set_selected_indices({idx}, idx)
                self._begin_move(nx, ny)
            else:
                self.clear_selection()
            event.accept()
            return

        # 作成モード。テキストはクリック、図形はドラッグ。
        if self.tool == 'text':
            self.requestText.emit(nx, ny)
            event.accept()
            return
        if self.tool == 'text_white':
            self.requestWhiteText.emit(nx, ny)
            event.accept()
            return
        if self.tool == 'text_plain':
            self.requestPlainText.emit(nx, ny)
            event.accept()
            return

        self._drag_start = (nx, ny)
        self._drag_current = (nx, ny)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            idx = self._hit_test(event.position())
            if (
                0 <= idx < len(self.annotations)
                and self.annotations[idx].kind in ('text', 'text_white', 'text_plain')
            ):
                self.set_selected_indices({idx}, idx)
                self.requestEditText.emit(idx)
                event.accept()
                return
            self.requestThumbnail.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        nx, ny = self._norm(event.position())

        if self._rotating and self.selected_index >= 0:
            a = self.annotations[self.selected_index]
            center = self._item_center_px(a)
            current_mouse_angle = math.degrees(
                math.atan2(
                    event.position().y() - center.y(),
                    event.position().x() - center.x()
                )
            )
            delta = current_mouse_angle - self._rotate_start_mouse_angle
            a.angle = round((self._rotate_start_item_angle + delta) / 5.0) * 5.0
            self.update()
            event.accept()
            return

        if self._resizing and self.selected_index >= 0:
            a = self.annotations[self.selected_index]
            o = self._resize_item_origin
            h = self._resize_handle
            if a.kind in ('line', 'arrow'):
                if h == 'p1':
                    a.x1, a.y1 = nx, ny
                elif h == 'p2':
                    a.x2, a.y2 = nx, ny
            else:
                center_nx = (o.x1 + o.x2) * 0.5
                center_ny = (o.y1 + o.y2) * 0.5
                rad = math.radians(-o.angle)
                dx = nx - center_nx
                dy = ny - center_ny
                lnx = center_nx + dx * math.cos(rad) - dy * math.sin(rad)
                lny = center_ny + dx * math.sin(rad) + dy * math.cos(rad)
                left, right = sorted((o.x1, o.x2))
                top, bottom = sorted((o.y1, o.y2))
                if h == 'tl':
                    a.x1, a.y1, a.x2, a.y2 = lnx, lny, right, bottom
                elif h == 'tr':
                    a.x1, a.y1, a.x2, a.y2 = left, lny, lnx, bottom
                elif h == 'bl':
                    a.x1, a.y1, a.x2, a.y2 = lnx, top, right, lny
                elif h == 'br':
                    a.x1, a.y1, a.x2, a.y2 = left, top, lnx, lny
            self.update()
            event.accept()
            return

        if self._moving and self.selected_indices:
            ox, oy = self._move_origin
            dx, dy = nx - ox, ny - oy

            origins = list(self._move_items_origin.values())
            if origins:
                min_x = min(min(v[0], v[2]) for v in origins)
                max_x = max(max(v[0], v[2]) for v in origins)
                min_y = min(min(v[1], v[3]) for v in origins)
                max_y = max(max(v[1], v[3]) for v in origins)
                dx = max(-min_x, min(1.0 - max_x, dx))
                dy = max(-min_y, min(1.0 - max_y, dy))

            for i, (x1, y1, x2, y2) in self._move_items_origin.items():
                if 0 <= i < len(self.annotations):
                    a = self.annotations[i]
                    a.x1, a.y1, a.x2, a.y2 = x1+dx, y1+dy, x2+dx, y2+dy
            self.update()
            event.accept()
            return

        if self._drag_start is not None:
            self._drag_current = (nx, ny)
            self.update()
            event.accept()
            return

        self._update_select_cursor(event.position())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return

        if self._rotating:
            self._rotating = False
            self._rotate_item_origin = None
            self.annotationChanged.emit()
            self._update_select_cursor(event.position())
            event.accept()
            return

        if self._resizing:
            self._resizing = False
            self._resize_handle = None
            self._resize_item_origin = None
            self.annotationChanged.emit()
            self._update_select_cursor(event.position())
            event.accept()
            return

        if self._moving:
            self._moving = False
            self._move_origin = None
            self._move_items_origin = {}
            self.annotationChanged.emit()
            self._update_select_cursor(event.position())
            event.accept()
            return

        if self._drag_start is not None:
            x1, y1 = self._drag_start
            x2, y2 = self._norm(event.position())
            self._drag_start = None
            self._drag_current = None
            if abs(x2-x1) + abs(y2-y1) > 0.005:
                self.annotations.append(
                    AnnotationItem(
                        self.tool,
                        x1, y1, x2, y2,
                        color=self.current_color,
                        fill_white=(
                            self.current_fill_white
                            if self.tool in ('rect', 'ellipse')
                            else False
                        ),
                    )
                )
                idx = len(self.annotations) - 1
                # 作成直後は編集可能な選択状態。ただし tool は元の作成ツールのまま。
                self.set_selected_indices({idx}, idx)
                self.annotationChanged.emit()
                self.objectCreated.emit()
            self.update()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def delete_selected_annotation(self):
        indices = sorted(self.selected_indices, reverse=True)
        if not indices:
            return False
        for idx in indices:
            if 0 <= idx < len(self.annotations):
                del self.annotations[idx]
        self.clear_selection(update=False)
        self.annotationChanged.emit()
        self.update()
        return True

    def paintEvent(self, event):
        super().paintEvent(event)

        border_painter = QPainter(self)
        border_pen = QPen(QColor('#666666'))
        border_pen.setWidth(2)
        border_painter.setPen(border_pen)
        border_painter.setBrush(Qt.NoBrush)
        border_painter.drawRect(
            QRectF(1, 1, max(0, self.width()-2), max(0, self.height()-2))
        )
        border_painter.end()

        if not self.annotations and self._drag_start is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        default_color = QColor('#ff0000')
        pen = QPen(default_color)
        pen.setWidth(3)

        def draw_handles(a):
            handle_pen = QPen(QColor(30, 144, 255))
            handle_pen.setWidth(2)
            painter.setPen(handle_pen)
            painter.setBrush(QColor(255, 255, 255))
            half = self.HANDLE_SIZE / 2.0
            points = self._handle_points(a)
            rotate_pt = points.get('rotate')
            if rotate_pt is not None:
                painter.drawLine(self._item_center_px(a), rotate_pt)
            for name, pt in points.items():
                r = QRectF(pt.x()-half, pt.y()-half, self.HANDLE_SIZE, self.HANDLE_SIZE)
                if name == 'rotate':
                    painter.drawEllipse(r)
                else:
                    painter.drawRect(r)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(pen)

        def draw_item(a, selected=False, primary=False, multi=False):
            x1, y1 = a.x1*self.width(), a.y1*self.height()
            x2, y2 = a.x2*self.width(), a.y2*self.height()
            center = QPointF((x1+x2)/2.0, (y1+y2)/2.0)

            item_color = QColor(getattr(a, 'color', '#ff0000'))
            if not item_color.isValid():
                item_color = QColor('#ff0000')
            item_pen = QPen(item_color)
            item_pen.setWidth(3)

            painter.save()
            painter.translate(center)
            painter.rotate(a.angle)
            painter.translate(-center)
            painter.setPen(item_pen)
            painter.setBrush(Qt.NoBrush)
            if a.kind == 'rect':
                painter.setBrush(
                    QColor(255, 255, 255)
                    if getattr(a, 'fill_white', False)
                    else Qt.NoBrush
                )
                painter.drawRect(QRectF(QPointF(x1,y1), QPointF(x2,y2)).normalized())
            elif a.kind == 'ellipse':
                painter.setBrush(
                    QColor(255, 255, 255)
                    if getattr(a, 'fill_white', False)
                    else Qt.NoBrush
                )
                painter.drawEllipse(QRectF(QPointF(x1,y1), QPointF(x2,y2)).normalized())
            elif a.kind in ('line','arrow'):
                painter.drawLine(QPointF(x1,y1), QPointF(x2,y2))
                if a.kind == 'arrow':
                    # 開いた矢印。塗りつぶし三角形ではなく2本線で矢尻を描く。
                    ang = math.atan2(y2-y1, x2-x1)
                    size = 14.0
                    p1 = QPointF(x2-size*math.cos(ang-0.45), y2-size*math.sin(ang-0.45))
                    p2 = QPointF(x2-size*math.cos(ang+0.45), y2-size*math.sin(ang+0.45))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawLine(QPointF(x2, y2), p1)
                    painter.drawLine(QPointF(x2, y2), p2)
            elif a.kind in ('text', 'text_white', 'text_plain'):
                r = QRectF(QPointF(x1,y1), QPointF(x2,y2)).normalized()
                if a.kind in ('text', 'text_white'):
                    painter.setBrush(
                        QColor(255,255,255)
                        if (a.kind == 'text_white' or getattr(a, 'fill_white', False))
                        else Qt.NoBrush
                    )
                    painter.setPen(item_pen)
                    painter.drawRect(r)
                else:
                    painter.setBrush(Qt.NoBrush)
                f = qfont_for_pdf_font(
                    getattr(a, 'font_name', 'Helv'),
                    getattr(a, 'font_size', 12.0),
                )
                painter.setFont(f)
                painter.setPen(item_pen)
                painter.drawText(r.adjusted(4,2,-4,-2), Qt.AlignLeft|Qt.AlignTop|Qt.TextWordWrap, a.text)
            painter.restore()

            if selected:
                sel = QPen(QColor(30, 144, 255))
                sel.setStyle(Qt.DashLine)
                sel.setWidth(2)
                painter.setPen(sel)
                painter.setBrush(Qt.NoBrush)
                if a.kind not in ('line', 'arrow'):
                    pts = self._handle_points(a)
                    order = [pts['tl'], pts['tr'], pts['br'], pts['bl'], pts['tl']]
                    painter.drawPolyline(QPolygonF(order))
                else:
                    pts = self._handle_points(a)
                    painter.drawLine(pts['p1'], pts['p2'])

                # ハンドルは単一選択時のみ表示。複数選択は一括移動専用。
                if primary and not multi:
                    draw_handles(a)

        multi = len(self.selected_indices) > 1
        for i, a in enumerate(self.annotations):
            draw_item(
                a,
                i in self.selected_indices,
                i == self.selected_index,
                multi
            )

        if self._drag_start is not None and self._drag_current is not None:
            x1,y1 = self._drag_start
            x2,y2 = self._drag_current
            draw_item(
                AnnotationItem(
                    self.tool, x1, y1, x2, y2,
                    color=self.current_color,
                    fill_white=(
                        self.current_fill_white
                        if self.tool in ('rect', 'ellipse')
                        else False
                    ),
                ),
                False
            )

        painter.end()



class EditorPDFSourceList(QListWidget):
    """
    編集対象切替専用のPDFリスト。
    - 行そのものは編集対象の切替専用
    - 右端ハンドルはページ挿入用D&D
    - ExplorerからPDFをドロップした場合は通常PDFリストへ追加
      （現在選択は変更しない）
    """
    MIME_TYPE = 'application/x-fastpdf-source-pdf'
    pdfFilesDropped = Signal(object)
    deleteRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(False)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        # 縦スクロールバー表示時にも横スクロールバーを出さない。
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # QListWidgetのviewport自体が縦スクロールバー分だけ自動的に狭くなるため、
        # 追加の右viewport marginは入れない。ここに余白を入れると行ウィジェットが
        # 余計に狭くなり、右端のドラッグハンドルがクリップされて見えなくなる。
        self.setViewportMargins(0, 0, 0, 0)
        # 各行の横幅はviewportの実幅へ追従させる。
        # 縦スクロールバーの表示/非表示でviewport幅が変わっても、
        # 右端のドラッグハンドルが必ず表示領域内に残るようにする。
        self.verticalScrollBar().rangeChanged.connect(
            lambda _min, _max: QTimer.singleShot(0, self._sync_item_widths)
        )

    def _sync_item_widths(self):
        width = max(1, self.viewport().width() - 2)
        for row in range(self.count()):
            item = self.item(row)
            if item is not None:
                item.setSizeHint(QSize(width, 38))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_item_widths()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.deleteRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _pdf_paths_from_mime(self, mime):
        if not mime.hasUrls():
            return []
        result = []
        seen = set()
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = normalize_path(url.toLocalFile())
            if (
                path.lower().endswith('.pdf')
                and Path(path).is_file()
                and path not in seen
            ):
                seen.add(path)
                result.append(path)
        return result

    def dragEnterEvent(self, event):
        if self._pdf_paths_from_mime(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        if self._pdf_paths_from_mime(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
            return
        event.ignore()

    def dropEvent(self, event):
        paths = self._pdf_paths_from_mime(event.mimeData())
        if not paths:
            event.ignore()
            return

        # QListWidget標準drop処理を通さないので
        # currentItem / selection はdrop前のまま維持される。
        self.pdfFilesDropped.emit(paths)
        event.setDropAction(Qt.CopyAction)
        event.accept()


    def mousePressEvent(self, event):
        # 右クリックでは現在の編集対象/選択状態を変えない。
        if event.button() == Qt.RightButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        if item is None:
            event.accept()
            return
        path = item.data(Qt.UserRole)
        if path:
            reveal_pdf_location(self.window(), normalize_path(path))
        event.accept()



class PDFDragHandle(QFrame):
    """リスト選択とは完全に独立したドラッグ専用領域。"""
    def __init__(self, pdf_path, parent=None):
        super().__init__(parent)
        self.pdf_path = normalize_path(pdf_path)
        self._press_pos = None
        self.setObjectName('pdfDragHandle')
        self.setFixedWidth(30)
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip('ドラッグ専用：サムネイルへ全ページ挿入')
        self.setFocusPolicy(Qt.NoFocus)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(0)
        label = QLabel('⠿')
        label.setAlignment(Qt.AlignCenter)
        label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(label)

        self.setStyleSheet(
            'QFrame#pdfDragHandle {'
            ' border: 1px solid #777; border-radius: 3px;'
            ' background: rgba(120,120,120,35); }'
            'QFrame#pdfDragHandle:hover {'
            ' background: rgba(120,120,120,75); }'
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press_pos is None or not (event.buttons() & Qt.LeftButton):
            event.accept()
            return
        distance = (event.position().toPoint() - self._press_pos).manhattanLength()
        if distance < QApplication.startDragDistance():
            event.accept()
            return
        mime = QMimeData()
        mime.setData(EditorPDFSourceList.MIME_TYPE, self.pdf_path.encode('utf-8'))
        mime.setUrls([QUrl.fromLocalFile(self.pdf_path)])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)
        self._press_pos = None
        self.setCursor(Qt.OpenHandCursor)
        event.accept()

    def mouseReleaseEvent(self, event):
        self._press_pos = None
        self.setCursor(Qt.OpenHandCursor)
        event.accept()


    def contextMenuEvent(self, event):
        reveal_pdf_location(self.window(), self.pdf_path)
        event.accept()


class PDFNameFrame(QFrame):
    """ファイル名クリック専用領域。ここだけが編集対象切替を行う。"""
    clicked = Signal()

    def __init__(self, pdf_path, dirty=False, parent=None):
        super().__init__(parent)
        self.pdf_path = normalize_path(pdf_path)
        self.dirty = bool(dirty)
        self.setObjectName('pdfNameFrame')
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 1, 6, 1)
        layout.setSpacing(5)
        self.dirty_label = QLabel()
        self.icon_label = QLabel()
        self.name_label = QLabel(Path(self.pdf_path).name)
        self.name_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        for w in (self.dirty_label, self.icon_label, self.name_label):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.dirty_label)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.name_label, 1)
        self.refresh_style()

    def refresh_style(self):
        is_temp = is_temp_pdf_path(self.pdf_path)
        self.dirty_label.setText('✏️' if self.dirty else '')
        self.icon_label.setText('🧪' if is_temp else '📄')
        if self.dirty:
            self.name_label.setStyleSheet('color: #d95f02; font-weight: 700;')
        elif is_temp:
            self.name_label.setStyleSheet('color: #d48a00; font-weight: 600;')
        else:
            self.name_label.setStyleSheet('')
        self.setStyleSheet(
            'QFrame#pdfNameFrame { border: 1px solid transparent; border-radius: 3px; }'
            'QFrame#pdfNameFrame:hover { border: 1px solid #888; background: rgba(120,120,120,25); }'
        )
        tip = ('一時保存ファイル\nアプリの temp フォルダ内にあります。\n不要になったら 📂 temp から削除してください。' if is_temp else self.pdf_path)
        if self.dirty:
            tip = '未保存の編集があります。\n' + tip
        self.setToolTip(tip)

    def set_dirty(self, dirty):
        self.dirty = bool(dirty)
        self.refresh_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        event.accept()


    def contextMenuEvent(self, event):
        reveal_pdf_location(self.window(), self.pdf_path)
        event.accept()


class PDFSourceRowWidget(QWidget):
    """1行を [ファイル名枠] [ドラッグ枠] に完全分離する。"""
    clicked = Signal()

    def __init__(self, pdf_path, dirty=False, parent=None):
        super().__init__(parent)
        self.pdf_path = normalize_path(pdf_path)
        self.dirty = bool(dirty)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(5)
        self.name_frame = PDFNameFrame(self.pdf_path, dirty=self.dirty)
        self.handle = PDFDragHandle(self.pdf_path)
        self.name_frame.clicked.connect(self.clicked.emit)
        layout.addWidget(self.name_frame, 1)
        layout.addWidget(self.handle, 0)

    def set_dirty(self, dirty):
        self.dirty = bool(dirty)
        self.name_frame.set_dirty(self.dirty)

    def mousePressEvent(self, event):
        event.accept()

    def contextMenuEvent(self, event):
        reveal_pdf_location(self.window(), self.pdf_path)
        event.accept()



class InsertedPageDelegate(QStyledItemDelegate):
    """
    挿入ページだけページ番号を赤いバッジで強調する。
    通常ページの選択時にQt標準のフォーカス枠がページ番号周辺へ出ないよう、
    State_HasFocus を除外してから標準描画する。
    """
    def paint(self, painter, option, index):
        opt = option
        try:
            opt = type(option)(option)
        except Exception:
            pass

        try:
            opt.state &= ~QStyle.State_HasFocus
        except Exception:
            pass

        super().paint(painter, opt, index)

        entry = index.data(Qt.UserRole)
        if entry is None or not getattr(entry, 'inserted', False):
            return

        page_no = str(index.row() + 1)

        painter.save()
        font = option.font
        font.setBold(True)
        painter.setFont(font)

        fm = painter.fontMetrics()
        badge_w = max(30, fm.horizontalAdvance(page_no) + 16)
        badge_h = fm.height() + 2

        # IconModeのテキスト領域中央付近に専用バッジを重ねる。
        # 選択グレーの上でも必ず視認できるよう白背景＋赤枠＋赤文字。
        x = option.rect.center().x() - badge_w / 2
        y = option.rect.bottom() - badge_h - 8
        badge = QRectF(x, y, badge_w, badge_h)

        painter.setBrush(QColor('#ffffff'))
        pen = QPen(QColor('#d40000'))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRoundedRect(badge, 4, 4)

        painter.setPen(QColor('#d40000'))
        painter.drawText(badge, Qt.AlignCenter, page_no)
        painter.restore()


class EditorPageList(QListWidget):
    """
    編集サムネイル専用リスト。
    QListWidget の InternalMove には並び替えを任せず、
    PageEntry の順番をダイアログ側で確実に更新する。
    """
    externalPdfDropped = Signal(str, int)
    externalPdfFilesDropped = Signal(object, int)
    internalRowsDropped = Signal(object, int)

    INTERNAL_MIME_TYPE = 'application/x-fastpdf-thumbnail-rows'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.viewport().setAcceptDrops(True)
        self.setMovement(QListView.Snap)

    def _drop_row(self, pos):
        """
        IconMode の余白へドロップしても末尾扱いにせず、
        ドロップ位置に最も近いサムネイルを基準に挿入位置を決める。
        """
        if self.count() == 0:
            return 0

        item = self.itemAt(pos)
        if item is not None:
            row = self.row(item)
            rect = self.visualItemRect(item)
            if pos.x() > rect.center().x():
                row += 1
            return max(0, min(row, self.count()))

        # サムネイル間の余白でも、同じ段にある最寄りアイテムを探す。
        same_band = []
        all_items = []
        for row in range(self.count()):
            it = self.item(row)
            rect = self.visualItemRect(it)
            if not rect.isValid():
                continue

            cx = rect.center().x()
            cy = rect.center().y()
            dx = pos.x() - cx
            dy = pos.y() - cy
            dist2 = dx * dx + dy * dy
            all_items.append((dist2, row, rect))

            vertical_margin = max(12, rect.height() // 3)
            if (
                rect.top() - vertical_margin
                <= pos.y()
                <= rect.bottom() + vertical_margin
            ):
                same_band.append((abs(dx), row, rect))

        candidates = same_band if same_band else all_items
        if not candidates:
            return self.count()

        candidates.sort(key=lambda x: x[0])
        _, row, rect = candidates[0]

        # 同じ段では左右位置で「前 / 後」を決める。
        # 別段の最寄りになった場合も中心より右なら後ろ。
        if pos.x() > rect.center().x():
            row += 1

        return max(0, min(row, self.count()))

    def startDrag(self, supported_actions):
        rows = sorted({
            self.row(item)
            for item in self.selectedItems()
            if self.row(item) >= 0
        })
        if not rows:
            return

        mime = QMimeData()
        mime.setData(
            self.INTERNAL_MIME_TYPE,
            json.dumps(rows).encode('utf-8')
        )

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)

    def dragEnterEvent(self, event):
        mime = event.mimeData()

        if mime.hasFormat(self.INTERNAL_MIME_TYPE):
            event.setDropAction(Qt.MoveAction)
            event.accept()
            return

        if mime.hasFormat(EditorPDFSourceList.MIME_TYPE):
            event.setDropAction(Qt.CopyAction)
            event.accept()
            return

        if mime.hasUrls():
            pdfs = [
                u.toLocalFile()
                for u in mime.urls()
                if u.isLocalFile()
                and u.toLocalFile().lower().endswith('.pdf')
            ]
            if pdfs:
                event.setDropAction(Qt.CopyAction)
                event.accept()
                return

        event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()

        if (
            mime.hasFormat(self.INTERNAL_MIME_TYPE)
            or mime.hasFormat(EditorPDFSourceList.MIME_TYPE)
            or mime.hasUrls()
        ):
            event.accept()
            return

        event.ignore()

    def dropEvent(self, event):
        mime = event.mimeData()
        insert_at = self._drop_row(event.position().toPoint())

        if mime.hasFormat(self.INTERNAL_MIME_TYPE):
            try:
                raw = bytes(mime.data(self.INTERNAL_MIME_TYPE))
                rows = json.loads(raw.decode('utf-8'))
                rows = sorted({
                    int(r) for r in rows
                    if 0 <= int(r) < self.count()
                })
            except Exception:
                event.ignore()
                return

            if rows:
                self.internalRowsDropped.emit(rows, insert_at)
                event.setDropAction(Qt.MoveAction)
                event.accept()
                return

        if mime.hasFormat(EditorPDFSourceList.MIME_TYPE):
            try:
                raw = bytes(mime.data(EditorPDFSourceList.MIME_TYPE))
                path = normalize_path(raw.decode('utf-8'))
            except Exception:
                event.ignore()
                return

            if Path(path).is_file():
                self.externalPdfDropped.emit(path, insert_at)
                event.setDropAction(Qt.CopyAction)
                event.accept()
                return

        if mime.hasUrls():
            paths = []
            for url in mime.urls():
                if not url.isLocalFile():
                    continue
                path = normalize_path(url.toLocalFile())
                if Path(path).is_file() and path.lower().endswith('.pdf'):
                    paths.append(path)

            if paths:
                self.externalPdfFilesDropped.emit(paths, insert_at)
                event.setDropAction(Qt.CopyAction)
                event.accept()
                return

        event.ignore()



class PageManagerDialog(QDialog):
    """
    PDF編集専用ワークスペース。
    編集途中データはメインビューアへ渡さず、このUI内だけで保持する。
    """
    savedPdf = Signal(str)
    pdfListChanged = Signal(object)

    def __init__(
        self,
        pdf_path,
        favorites_manager,
        parent=None,
        start_page=0,
        pdf_paths=None
    ):
        super().__init__(parent)
        self.pdf_path = normalize_path(pdf_path)

        # 通常PDFリスト。temp ファイルはここには混ぜない。
        self.pdf_paths = [
            normalize_path(p) for p in (pdf_paths or [pdf_path])
            if p and Path(p).is_file() and not is_temp_pdf_path(p)
        ]
        if (
            self.pdf_path not in self.pdf_paths
            and not is_temp_pdf_path(self.pdf_path)
        ):
            self.pdf_paths.append(self.pdf_path)

        # 一時保存リストは temp フォルダの実ファイルから構築する。
        self.temp_paths = sorted(
            normalize_path(p)
            for p in get_temp_dir().glob('*.pdf')
            if p.is_file()
        )

        self.last_normal_pdf_path = (
            self.pdf_path
            if not is_temp_pdf_path(self.pdf_path)
            else (self.pdf_paths[0] if self.pdf_paths else None)
        )

        self.favorites = favorites_manager
        self.entries = []
        self.current_index = max(0, int(start_page))
        self.dirty = False
        self._switching_pdf = False
        self._activating_source_path = False

        # 単一ページ表示用
        self.single_zoom_percent = 100
        self.single_middle_dragging = False
        self.single_middle_last_pos = None
        # 現在の編集セッション中だけ有効な、ページ別の高精細表示DPI。
        self._single_detail_dpi = {}

        # 朱書き編集
        self.editor_settings = load_editor_settings()
        self.annotation_tool = 'select'

        saved_color_text = str(
            self.editor_settings.get('annotation_color', '#ff0000')
        ).lower()
        # 以前のFastPDF既定色は廃止し、純赤へ自動移行する。
        if saved_color_text == '#e60000':
            saved_color_text = '#ff0000'

        saved_color = QColor(saved_color_text)
        self.annotation_color = (
            saved_color.name() if saved_color.isValid() else '#ff0000'
        )
        self.annotation_fill_white = bool(
            self.editor_settings.get('annotation_fill_white', False)
        )
        try:
            self.annotation_font_size = float(
                self.editor_settings.get('annotation_font_size', 12.0)
            )
        except Exception:
            self.annotation_font_size = 12.0
        self.annotation_font_size = max(1.0, min(72.0, self.annotation_font_size))
        self.annotation_font_name = normalize_pdf_font_name(
            self.editor_settings.get('annotation_font_name', 'Helv')
        )

        self._annotation_undo = []
        self._annotation_redo = []
        self._annotation_snapshot_before_action = None

        self.thumbnail_zoom_percent = 100
        self._thumbnail_cache = OrderedDict()
        self._thumbnail_queue = []
        self._thumbnail_busy = False
        self._single_cache = OrderedDict()

        self.setWindowTitle(f'PDF編集 - {Path(self.pdf_path).name}')
        self.resize(1380, 840)
        self.setModal(True)

        self._load_original_entries()
        if self.entries:
            self.current_index = min(self.current_index, len(self.entries) - 1)

        self._build_ui()
        self._populate_pdf_source_list()
        self.rebuild_list()
        self.show_single_page()

    def _load_original_entries(self):
        favs = self.favorites.pages(self.pdf_path)
        doc = fitz.open(self.pdf_path)
        try:
            for i in range(doc.page_count):
                page = doc.load_page(i)
                self.entries.append(
                    PageEntry(
                        self.pdf_path, i, int(page.rotation), 0, i in favs
                    )
                )
        finally:
            doc.close()

    def _fill_source_list(self, widget, paths):
        widget.blockSignals(True)
        try:
            widget.clear()

            for row, path in enumerate(paths):
                path = normalize_path(path)
                item = QListWidgetItem()
                item.setData(Qt.UserRole, path)
                item.setToolTip(path)
                # 初期幅も現在のviewport実幅に合わせる。以後は
                # EditorPDFSourceList._sync_item_widths() がスクロールバー表示を含めて追従する。
                item.setSizeHint(QSize(max(1, widget.viewport().width() - 2), 38))
                widget.addItem(item)

                row_widget = PDFSourceRowWidget(
                    path,
                    dirty=(path == self.pdf_path and self.dirty)
                )
                row_widget.clicked.connect(
                    lambda p=path, w=widget: self._select_source_path(p, w)
                )
                widget.setItemWidget(item, row_widget)
        finally:
            widget.blockSignals(False)
        widget._sync_item_widths()

    def _populate_pdf_source_list(self):
        # temp フォルダ側は、削除済みファイルを消し、新規ファイルを拾う。
        disk_temp = sorted(
            normalize_path(p)
            for p in get_temp_dir().glob('*.pdf')
            if p.is_file()
        )
        self.temp_paths = disk_temp

        self._fill_source_list(self.pdf_source_list, self.pdf_paths)
        self._fill_source_list(self.temp_source_list, self.temp_paths)
        self._sync_source_selection()

    def _sync_source_selection(self):
        self._switching_pdf = True
        try:
            for widget in (self.pdf_source_list, self.temp_source_list):
                widget.blockSignals(True)
                widget.clearSelection()
                widget.setCurrentRow(-1)

                for row in range(widget.count()):
                    item = widget.item(row)
                    path = normalize_path(item.data(Qt.UserRole))
                    row_widget = widget.itemWidget(item)
                    if isinstance(row_widget, PDFSourceRowWidget):
                        row_widget.set_dirty(
                            path == self.pdf_path and self.dirty
                        )

                    if path == self.pdf_path:
                        widget.setCurrentRow(row)
                        item.setSelected(True)
                        widget.scrollToItem(item)

                widget.blockSignals(False)
        finally:
            self._switching_pdf = False

    def _select_source_path(self, path, widget):
        """
        行内ウィジェットをクリックした時の選択処理。

        currentRowChanged とここからの直接呼び出しが重なると、
        未保存確認が同じ操作で複数回出るため、
        行が変わる場合は currentRowChanged 側だけに切替処理を任せる。
        すでに同じ行がCurrentの場合だけ直接切替する。
        """
        path = normalize_path(path)

        for row in range(widget.count()):
            item = widget.item(row)
            if normalize_path(item.data(Qt.UserRole)) != path:
                continue

            if widget.currentRow() == row:
                item.setSelected(True)
                self._activate_source_path(path)
            else:
                # setCurrentRow() → currentRowChanged → _activate_source_path()
                widget.setCurrentRow(row)
                item.setSelected(True)
            return

    def _confirm_discard_current_edits_for_switch(self):
        if not self.dirty:
            return True

        result = QMessageBox.question(
            self,
            '未保存の編集',
            '現在のPDFには未保存の編集があります。\n\n'
            'この編集を破棄して別のPDFへ切り替えますか？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        return result == QMessageBox.Yes

    def add_pdfs_to_source_list(self, paths):
        """
        Explorerから通常PDFリストへ追加。
        現在の編集対象・選択状態は変えない。
        PDF本体ではなく、リスト構成だけを即時保存対象として通知する。
        """
        active_path = self.pdf_path

        added = False
        for raw in paths:
            path = normalize_path(raw)
            if (
                path
                and path.lower().endswith('.pdf')
                and Path(path).is_file()
                and not is_temp_pdf_path(path)
                and path not in self.pdf_paths
            ):
                self.pdf_paths.append(path)
                added = True

        if not added:
            return

        self._populate_pdf_source_list()

        # 新規追加PDFは選択しない。編集対象もdrop前のまま。
        self.pdf_path = active_path
        self._sync_source_selection()

        self.pdfListChanged.emit(list(self.pdf_paths))

    def delete_selected_pdf_from_source_list(self):
        """通常PDFリストの選択PDFをリストから削除し、ビューアへ即時反映する。"""
        widget = self.pdf_source_list
        item = widget.currentItem()
        if item is None:
            return

        path = normalize_path(item.data(Qt.UserRole))
        if not path or path not in self.pdf_paths:
            return

        # 現在編集中のPDFを消す場合だけ、未保存編集の破棄確認を行う。
        if path == normalize_path(self.pdf_path) and self.dirty:
            result = QMessageBox.question(
                self,
                'PDFリストから削除',
                '現在編集中のPDFには未保存の編集があります。\n\n'
                '編集内容を破棄してリストから削除しますか？',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if result != QMessageBox.Yes:
                self._sync_source_selection()
                return

        old_index = self.pdf_paths.index(path)
        self.pdf_paths = [p for p in self.pdf_paths if normalize_path(p) != path]

        # 編集中ではないPDFなら、現在の編集対象はそのまま維持する。
        if path != normalize_path(self.pdf_path):
            self._populate_pdf_source_list()
            self.pdfListChanged.emit(list(self.pdf_paths))
            return

        # 現在編集中のPDFを削除した場合は、残っている通常PDFへ切り替える。
        if self.pdf_paths:
            next_index = min(old_index, len(self.pdf_paths) - 1)
            next_path = normalize_path(self.pdf_paths[next_index])
            self.load_pdf_for_edit(next_path, start_page=0)
        elif self.temp_paths:
            # 通常リストが空なら、存在する一時PDFを編集対象として維持できる。
            next_path = normalize_path(self.temp_paths[0])
            self.load_pdf_for_edit(next_path, start_page=0)
        else:
            # 編集対象が完全になくなった場合はビューアへ戻る。
            self.pdfListChanged.emit([])
            self.close()
            return

        self._populate_pdf_source_list()
        self.pdfListChanged.emit(list(self.pdf_paths))

    def on_pdf_source_changed(self, row):
        if self._switching_pdf or row < 0:
            return

        widget = self.sender()
        if widget not in (
            self.pdf_source_list,
            self.temp_source_list
        ):
            return

        item = widget.item(row)
        if item is None:
            return

        path = normalize_path(item.data(Qt.UserRole))
        self._activate_source_path(path)

    def _activate_source_path(self, path):
        # 同一UI操作中の再入を防ぎ、確認ダイアログを1回だけにする。
        if self._activating_source_path:
            return

        self._activating_source_path = True
        try:
            path = normalize_path(path)

            if not path or path == self.pdf_path:
                self._sync_source_selection()
                return

            if not Path(path).is_file():
                if is_temp_pdf_path(path):
                    self._populate_pdf_source_list()
                    return
                QMessageBox.warning(
                    self,
                    'PDF切替',
                    f'ファイルが見つかりません。\n{path}'
                )
                self._populate_pdf_source_list()
                return

            if not self._confirm_discard_current_edits_for_switch():
                self._sync_source_selection()
                return

            self.load_pdf_for_edit(path, start_page=0)
        finally:
            self._activating_source_path = False

    def load_pdf_for_edit(self, path, start_page=0):
        path = normalize_path(path)
        if not Path(path).is_file():
            if is_temp_pdf_path(path):
                self._populate_pdf_source_list()
                return
            QMessageBox.warning(
                self, 'PDF切替', f'ファイルが見つかりません。\n{path}'
            )
            return

        self.pdf_path = path
        if not is_temp_pdf_path(path):
            self.last_normal_pdf_path = path

        self.entries = []
        self.current_index = max(0, int(start_page))
        self.dirty = False
        self._annotation_undo.clear()
        self._annotation_redo.clear()
        self._annotation_snapshot_before_action = None
        self._thumbnail_cache.clear()
        self._thumbnail_queue.clear()
        self._thumbnail_busy = False
        self._single_cache.clear()

        try:
            self._load_original_entries()
        except Exception as e:
            QMessageBox.critical(self, 'PDF切替', str(e))
            return

        if self.entries:
            self.current_index = min(
                self.current_index, len(self.entries) - 1
            )
        else:
            self.current_index = 0

        self.update_title()
        self.rebuild_list(self.current_index)
        self.show_single_page()

        self._sync_source_selection()

    def insert_pdf_pages_from_drag(self, path, insert_at):
        path = normalize_path(path)
        if not Path(path).is_file():
            return

        try:
            src = fitz.open(path)
            try:
                new_entries = [
                    PageEntry(path, idx, int(src.load_page(idx).rotation), 0, False, inserted=True)
                    for idx in range(src.page_count)
                ]
            finally:
                src.close()
        except Exception as e:
            QMessageBox.critical(
                self, 'ページ挿入', f'PDFを読み込めませんでした。\n{e}'
            )
            return

        if not new_entries:
            return

        self.sync_entries_from_items()
        insert_at = max(0, min(int(insert_at), len(self.entries)))

        for offset, entry in enumerate(new_entries):
            self.entries.insert(insert_at + offset, entry)

        self.current_index = insert_at
        self.mark_dirty()
        self.rebuild_list(self.current_index)
        self.select_inserted_thumbnail_range(
            insert_at,
            len(new_entries)
        )

    def reorder_thumbnail_rows(self, rows, insert_at):
        """
        複数選択も含め、PageEntry 自体を確実に並べ替える。
        QListWidget の内部移動結果には依存しない。
        """
        if not rows or not self.entries:
            return

        rows = sorted({
            int(r) for r in rows
            if 0 <= int(r) < len(self.entries)
        })
        if not rows:
            return

        moving = [self.entries[r] for r in rows]
        remaining = [
            entry for i, entry in enumerate(self.entries)
            if i not in set(rows)
        ]

        # 元の行を抜いた分だけ挿入位置を補正
        removed_before = sum(1 for r in rows if r < insert_at)
        corrected = insert_at - removed_before
        corrected = max(0, min(corrected, len(remaining)))

        self.entries = (
            remaining[:corrected]
            + moving
            + remaining[corrected:]
        )

        self.current_index = corrected
        self.mark_dirty()
        self.rebuild_list(corrected)

        self.list.clearSelection()
        for i in range(corrected, corrected + len(moving)):
            item = self.list.item(i)
            if item:
                item.setSelected(True)
        self.list.setCurrentRow(corrected)

    def insert_external_pdf_files(self, paths, insert_at):
        insert_at = max(0, min(int(insert_at), len(self.entries)))
        added = 0

        for path in paths:
            path = normalize_path(path)
            if not Path(path).is_file() or not path.lower().endswith('.pdf'):
                continue

            try:
                src = fitz.open(path)
                try:
                    new_entries = [
                        PageEntry(path, idx, int(src.load_page(idx).rotation), 0, False, inserted=True)
                        for idx in range(src.page_count)
                    ]
                finally:
                    src.close()
            except Exception as e:
                QMessageBox.warning(
                    self,
                    'ページ挿入',
                    f'{Path(path).name} を読み込めませんでした。\n{e}'
                )
                continue

            for offset, entry in enumerate(new_entries):
                self.entries.insert(insert_at + added + offset, entry)
            added += len(new_entries)

        if added:
            self.current_index = insert_at
            self.mark_dirty()
            self.rebuild_list(self.current_index)
            self.select_inserted_thumbnail_range(
                insert_at,
                added
            )

    def on_thumbnail_double_clicked(self, item):
        row = self.list.row(item)
        if 0 <= row < len(self.entries):
            self.current_index = row
            self.show_single_mode()

    def mark_dirty(self):
        self.dirty = True
        self.update_title()
        if hasattr(self, 'pdf_source_list'):
            self._sync_source_selection()

    def update_title(self):
        mark = ' *PDF' if self.dirty else ''
        self.setWindowTitle(
            f'PDF編集 - {Path(self.pdf_path).name}{mark}'
        )

    def _button(self, text, tip, slot, checkable=False):
        return make_tool_button(text, tip, slot, checkable)


    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ------------------------------------------------------------
        # 1行目：左=PDFリスト操作 / 中央=モード切替・ページ編集 / 右=戻る
        # ------------------------------------------------------------
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)

        # 左端：PDFリスト見出し＋保存操作。
        # body側の左ペインと同じ幅感にして、PDFリスト本体を1行分上へ詰める。
        self.pdf_header_frame = QFrame()
        self.pdf_header_frame.setObjectName('pdfHeaderGroup')
        self.pdf_header_frame.setMinimumWidth(250)
        self.pdf_header_frame.setMaximumWidth(340)
        self.pdf_header_frame.setStyleSheet(
            'QFrame#pdfHeaderGroup {'
            ' border: none;'
            ' background: transparent; }'
        )
        pdf_header_layout = QHBoxLayout(self.pdf_header_frame)
        pdf_header_layout.setContentsMargins(7, 3, 4, 3)
        pdf_header_layout.setSpacing(3)
        pdf_header_layout.addWidget(QLabel('PDFリスト'))
        pdf_header_layout.addStretch(1)
        pdf_header_layout.addWidget(
            self._button(
                '💾',
                '編集内容を元PDFへ上書き保存',
                self.save_overwrite
            )
        )
        pdf_header_layout.addWidget(
            self._button(
                '📥',
                '編集内容を別名PDFとして保存',
                self.save_as
            )
        )
        top_row.addWidget(self.pdf_header_frame, 0)

        # 右側上段。モード切替と編集操作は中央へまとめる。
        center_top = QHBoxLayout()
        center_top.setContentsMargins(0, 0, 0, 0)
        center_top.setSpacing(8)
        center_top.addStretch(1)

        # モード切替ボタン群
        self.mode_frame = QFrame()
        self.mode_frame.setObjectName('modeButtonGroup')
        self.mode_frame.setStyleSheet(
            'QFrame#modeButtonGroup {'
            ' border: 1px solid #777; border-radius: 7px;'
            ' background: rgba(80,80,80,24); }'
        )
        mode_layout = QHBoxLayout(self.mode_frame)
        mode_layout.setContentsMargins(4, 3, 4, 3)
        mode_layout.setSpacing(4)

        self.single_mode_btn = self._button(
            '📄', '1ページ表示モード', self.show_single_mode, True
        )
        self.thumb_mode_btn = self._button(
            '🗂️', 'サムネイル表示モード', self.show_thumbnail_mode, True
        )
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.single_mode_btn)
        self.mode_group.addButton(self.thumb_mode_btn)
        self.thumb_mode_btn.setChecked(True)
        mode_layout.addWidget(self.single_mode_btn)
        mode_layout.addWidget(self.thumb_mode_btn)
        center_top.addWidget(self.mode_frame)

        # ページ編集ボタン群
        self.edit_frame = QFrame()
        self.edit_frame.setObjectName('pageEditButtonGroup')
        self.edit_frame.setStyleSheet(
            'QFrame#pageEditButtonGroup {'
            ' border: none;'
            ' background: transparent; }'
        )
        edit_layout = QHBoxLayout(self.edit_frame)
        edit_layout.setContentsMargins(4, 3, 4, 3)
        edit_layout.setSpacing(4)

        self.page_action_buttons = []
        for b in (
            self._button('➕', '別PDFからページを追加', self.add_pages_from_pdf),
            self._button('🗑️', '選択ページを削除（Delete）', self.delete_selected),
            self._button('↶', '左へ90°回転', lambda: self.rotate_selected(-90)),
            self._button('↷', '右へ90°回転', lambda: self.rotate_selected(90)),
            self._button('🔄', '180°回転', lambda: self.rotate_selected(180)),
            self._button('⭐', 'お気に入り切替（Space）・変更は即時保存', self.toggle_selected_favorite),
            self._button('🆑', '現在のPDFのお気に入りをすべてクリア', self.clear_all_favorites),
            self._button('🔎', '単一ページ表示の現在ページだけを一時的に高精細で再描画', self.refresh_current_single_high_quality),
        ):
            self.page_action_buttons.append(b)
            edit_layout.addWidget(b)
        center_top.addWidget(self.edit_frame)
        center_top.addStretch(1)

        center_top_widget = QWidget()
        center_top_widget.setLayout(center_top)
        top_row.addWidget(center_top_widget, 1)

        # ビューアへ戻るは単独でUI右上端。
        self.return_viewer_btn = self._button(
            '↩️',
            '編集画面を閉じてビューアへ戻る',
            self.close
        )
        top_row.addWidget(self.return_viewer_btn, 0, Qt.AlignRight | Qt.AlignVCenter)
        root.addLayout(top_row)

        body_splitter = QSplitter(Qt.Horizontal)

        # 編集対象PDFリスト。見出しは上段へ移したので、ここはリストから開始。
        source_panel = QWidget()
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(4)

        self.pdf_source_list = EditorPDFSourceList()
        self.pdf_source_list.setMinimumWidth(210)
        self.pdf_source_list.setMaximumWidth(340)
        self.pdf_source_list.currentRowChanged.connect(
            self.on_pdf_source_changed
        )
        self.pdf_source_list.pdfFilesDropped.connect(
            self.add_pdfs_to_source_list
        )
        self.pdf_source_list.deleteRequested.connect(
            self.delete_selected_pdf_from_source_list
        )
        source_layout.addWidget(self.pdf_source_list, 3)

        temp_header = QHBoxLayout()
        temp_header.setSpacing(3)
        temp_header.addWidget(QLabel('🧪 一時保存'))
        temp_header.addStretch(1)
        temp_header.addWidget(
            self._button(
                '🧪',
                '現在の編集状態を temp フォルダへ一時保存',
                self.save_temporary
            )
        )
        temp_header.addWidget(
            self._button(
                '📂',
                '一時保存フォルダを開く',
                self.open_temp_folder_from_editor
            )
        )
        source_layout.addLayout(temp_header)

        self.temp_source_list = EditorPDFSourceList()
        self.temp_source_list.setMinimumWidth(210)
        self.temp_source_list.setMaximumWidth(340)
        self.temp_source_list.currentRowChanged.connect(
            self.on_pdf_source_changed
        )
        source_layout.addWidget(self.temp_source_list, 2)

        source_hint = QLabel(
            'ファイル名枠：編集対象切替\n'
            '右端の ⠿ 枠：選択を変えずに全ページD&D / Delete：リストから削除'
        )
        source_hint.setWordWrap(True)
        source_layout.addWidget(source_hint)

        body_splitter.addWidget(source_panel)

        self.stack = QStackedWidget()
        body_splitter.addWidget(self.stack)
        body_splitter.setStretchFactor(0, 0)
        body_splitter.setStretchFactor(1, 1)
        body_splitter.setSizes([250, 1050])

        root.addWidget(body_splitter, 1)

        # 1ページ表示
        self.single_page = QWidget()
        single_root = QVBoxLayout(self.single_page)
        single_root.setContentsMargins(0, 0, 0, 0)
        single_root.setSpacing(6)

        single_body = QHBoxLayout()
        single_body.setContentsMargins(0, 0, 0, 0)
        single_body.setSpacing(6)

        # 朱書きツールは単一ページモードだけ、ビュー左側へ縦配置。
        self.ann_panel = QFrame()
        self.ann_panel.setObjectName('annotationToolPanel')
        self.ann_panel.setStyleSheet(
            'QFrame#annotationToolPanel {'
            ' border: 1px solid #777; border-radius: 7px;'
            ' background: rgba(80,80,80,24); padding: 3px; }'
        )
        ann_layout = QVBoxLayout(self.ann_panel)
        ann_layout.setContentsMargins(4, 5, 4, 5)
        ann_layout.setSpacing(4)
        self.ann_buttons = {}
        self.ann_group = QButtonGroup(self)
        self.ann_group.setExclusive(True)
        for key, label, tip in (
            ('select', '➚', '選択・移動・リサイズ・回転ハンドル'),
            ('text', 'T□', '透明背景＋枠線ありのテキストボックス'),
            ('text_white', 'T■', '白背景＋枠線ありのテキストボックス'),
            ('text_plain', 'T', '枠なしの赤文字'),
            ('ellipse', '○', '赤丸・楕円：ドラッグで作成'),
            ('rect', '□', '赤四角：ドラッグで作成'),
            ('line', '─', '赤線：ドラッグで作成'),
            ('arrow', '→', '赤矢印：ドラッグで作成'),
        ):
            b = self._button(
                label, tip,
                lambda checked=False, k=key: self.set_annotation_tool(k),
                True
            )
            b.setFixedWidth(ANNOTATION_BUTTON_WIDTH)
            self.ann_group.addButton(b)
            self.ann_buttons[key] = b
            ann_layout.addWidget(b)

        self.ann_buttons['select'].setChecked(True)
        ann_layout.addSpacing(4)

        # 朱書き色。ボタン自体は白背景のまま、中央の色見本だけ変更する。
        self.annotation_color_btn = self._button(
            '', '朱書き色をパレットから選択',
            self.choose_annotation_color
        )
        self.annotation_color_btn.setFixedWidth(ANNOTATION_BUTTON_WIDTH)
        ann_layout.addWidget(self.annotation_color_btn)
        self._update_annotation_color_button()

        # 四角・楕円の白塗り切替。
        self.annotation_fill_btn = self._button(
            '■', '四角・楕円の内部を白塗り／白塗り解除',
            self.toggle_annotation_white_fill
        )
        self.annotation_fill_btn.setFixedWidth(ANNOTATION_BUTTON_WIDTH)
        self.annotation_fill_btn.setCheckable(True)
        self.annotation_fill_btn.setChecked(self.annotation_fill_white)
        ann_layout.addWidget(self.annotation_fill_btn)

        self.annotation_font_size_btn = self._button(
            'A↕', '選択中テキストのフォントサイズ変更。未選択時は新規テキストの既定サイズ',
            self.change_annotation_font_size
        )
        self.annotation_font_size_btn.setFixedWidth(ANNOTATION_BUTTON_WIDTH)
        ann_layout.addWidget(self.annotation_font_size_btn)

        self.annotation_font_btn = self._button(
            'Aa', '選択中テキストのフォント変更。未選択時は新規テキストの既定フォント',
            self.change_annotation_font
        )
        self.annotation_font_btn.setFixedWidth(ANNOTATION_BUTTON_WIDTH)
        ann_layout.addWidget(self.annotation_font_btn)
        self._update_annotation_font_buttons()

        ann_layout.addSpacing(4)

        ann_delete_btn = self._button(
            '❌', '選択中の朱書きを削除',
            self.delete_selected_annotation
        )
        ann_undo_btn = self._button(
            '↶U', '朱書きを元に戻す（Ctrl+Z）',
            self.undo_annotation
        )
        ann_redo_btn = self._button(
            '↷R', '朱書きをやり直す（Ctrl+Y）',
            self.redo_annotation
        )
        for b in (ann_delete_btn, ann_undo_btn, ann_redo_btn):
            b.setFixedWidth(ANNOTATION_BUTTON_WIDTH)
            ann_layout.addWidget(b)

        ann_layout.addStretch(1)
        single_body.addWidget(self.ann_panel, 0)

        self.single_scroll = QScrollArea()
        self.single_scroll.setWidgetResizable(False)
        self.single_scroll.setAlignment(Qt.AlignCenter)

        self.single_label = AnnotationCanvas()
        self.single_label.set_drawing_style(
            self.annotation_color,
            self.annotation_fill_white
        )
        self.single_label.setAlignment(Qt.AlignCenter)
        self.single_label.setStyleSheet(
            'QLabel { background: #303030; color: #eeeeee; }'
        )
        self.single_label.annotationChanged.connect(self.on_annotation_changed)
        self.single_label.requestText.connect(self.add_text_annotation)
        self.single_label.requestWhiteText.connect(self.add_white_text_annotation)
        self.single_label.requestPlainText.connect(self.add_plain_text_annotation)
        self.single_label.requestEditText.connect(self.edit_text_annotation)
        self.single_label.requestThumbnail.connect(self.show_thumbnail_mode)
        self.single_scroll.setWidget(self.single_label)
        self.single_scroll.viewport().installEventFilter(self)
        self.single_label.installEventFilter(self)
        single_body.addWidget(self.single_scroll, 1)
        single_root.addLayout(single_body, 1)

        nav = QHBoxLayout()
        nav.addStretch(1)
        nav.addWidget(self._button('⏪', '先頭ページ', self.first_page))
        nav.addWidget(self._button('◀️', '前ページ', self.previous_page))
        self.single_page_no = QLabel('0 / 0')
        self.single_page_no.setAlignment(Qt.AlignCenter)
        self.single_page_no.setMinimumWidth(120)
        nav.addWidget(self.single_page_no)
        nav.addWidget(self._button('▶️', '次ページ', self.next_page))
        nav.addWidget(self._button('⏩', '最終ページ', self.last_page))
        nav.addStretch(1)
        single_root.addLayout(nav)

        self.stack.addWidget(self.single_page)

        # サムネイル表示
        self.thumb_page = QWidget()
        thumb_root = QVBoxLayout(self.thumb_page)
        thumb_root.setContentsMargins(0, 0, 0, 0)

        self.list = EditorPageList()
        self.list.setViewMode(QListWidget.IconMode)
        icon_w, icon_h, grid_w, grid_h = self._thumbnail_dimensions()
        self.list.setIconSize(QSize(icon_w, icon_h))
        self.list.setGridSize(QSize(grid_w, grid_h))
        self.list.setSpacing(max(2, int(4 * self.thumbnail_zoom_percent / 100.0)))
        self.list.setItemDelegate(InsertedPageDelegate(self.list))
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListView.Snap)
        self.list.setFlow(QListView.LeftToRight)
        self.list.setWrapping(True)
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setStyleSheet(
            '''
            QListWidget {
                outline: 0;
            }
            QListWidget::item {
                padding: 7px;
                margin: 2px;
                border: none;
                border-radius: 8px;
            }
            QListWidget::item:selected {
                background: rgba(125, 125, 125, 115);
                border: none;
                color: palette(text);
            }
            QListWidget::item:selected:active {
                background: rgba(125, 125, 125, 135);
                border: none;
            }
            '''
        )
        self.list.installEventFilter(self)
        self.list.viewport().installEventFilter(self)

        self.list.currentRowChanged.connect(self.on_thumbnail_current_changed)
        self.list.itemDoubleClicked.connect(
            self.on_thumbnail_double_clicked
        )
        self.list.externalPdfDropped.connect(
            self.insert_pdf_pages_from_drag
        )
        self.list.externalPdfFilesDropped.connect(
            self.insert_external_pdf_files
        )
        self.list.internalRowsDropped.connect(
            self.reorder_thumbnail_rows
        )
        self.list.verticalScrollBar().valueChanged.connect(
            lambda _v: self.schedule_visible_thumbnails()
        )
        self.list.horizontalScrollBar().valueChanged.connect(
            lambda _v: self.schedule_visible_thumbnails()
        )

        thumb_root.addWidget(self.list, 1)

        hint = QLabel(
            'ページD&D：並び替え / ⠿・外部PDF D&D：ページ挿入 / Ctrl+ホイール：サムネイル拡大縮小 / ダブルクリック：1ページ表示 / Delete：削除 / Space：お気に入り'
        )
        hint.setAlignment(Qt.AlignCenter)
        thumb_root.addWidget(hint)

        self.stack.addWidget(self.thumb_page)

        # 初期表示はサムネイル。ボタン選択状態と実表示を一致させる。
        self.stack.setCurrentWidget(self.thumb_page)
        self.thumb_mode_btn.setChecked(True)
        self.single_mode_btn.setChecked(False)

    def show_single_mode(self):
        self.sync_entries_from_items()
        row = self.list.currentRow()
        if row >= 0:
            self.current_index = row
        self.single_mode_btn.setChecked(True)
        self.stack.setCurrentWidget(self.single_page)
        self.show_single_page()

    def show_thumbnail_mode(self):
        self.thumb_mode_btn.setChecked(True)
        self.stack.setCurrentWidget(self.thumb_page)
        if self.entries:
            self.current_index = min(self.current_index, len(self.entries) - 1)
            self.list.setCurrentRow(self.current_index)
        QTimer.singleShot(0, self.schedule_visible_thumbnails)

    def on_thumbnail_current_changed(self, row):
        if 0 <= row < len(self.entries):
            self.current_index = row

    def eventFilter(self, obj, event):
        # UI構築途中でも eventFilter は呼ばれる可能性があるため、
        # 各Widgetが生成済みか確認してから参照する。
        page_list = getattr(self, 'list', None)
        single_scroll = getattr(self, 'single_scroll', None)
        single_label = getattr(self, 'single_label', None)

        if (
            page_list is not None
            and obj in (page_list, page_list.viewport())
            and event.type() == QEvent.Wheel
            and event.modifiers() & Qt.ControlModifier
        ):
            delta = event.angleDelta().y()
            if delta:
                self.change_thumbnail_zoom(10 if delta > 0 else -10)
            return True

        if page_list is not None and obj is page_list and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Delete:
                self.delete_selected()
                return True
            if event.key() == Qt.Key_Space:
                self.toggle_selected_favorite()
                return True

        # 単一ページ表示はビューアと同じ操作。
        single_targets = []
        if single_scroll is not None:
            single_targets.append(single_scroll.viewport())
        if single_label is not None:
            single_targets.append(single_label)

        if obj in single_targets:
            if (
                obj is single_label
                and event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton
                and (
                    bool(getattr(single_label, 'selected_indices', set()))
                    or self.annotation_tool not in ('text', 'text_white', 'text_plain')
                )
            ):
                self._annotation_snapshot_before_action = self._annotation_state()

            if event.type() == QEvent.Wheel:
                delta = event.angleDelta().y()
                if event.modifiers() & Qt.ControlModifier:
                    self.single_zoom_percent = max(
                        20,
                        min(
                            500,
                            self.single_zoom_percent + (10 if delta > 0 else -10)
                        )
                    )
                    self.show_single_page()
                else:
                    if delta > 0:
                        self.previous_page()
                    elif delta < 0:
                        self.next_page()
                return True

            if event.type() == QEvent.MouseButtonPress:
                if event.button() == Qt.MiddleButton:
                    self.single_middle_dragging = True
                    self.single_middle_last_pos = event.globalPosition()
                    self.single_scroll.viewport().setCursor(
                        Qt.ClosedHandCursor
                    )
                    return True

            if event.type() == QEvent.MouseMove:
                if (
                    self.single_middle_dragging
                    and event.buttons() & Qt.MiddleButton
                    and self.single_middle_last_pos is not None
                ):
                    pos = event.globalPosition()
                    delta = pos - self.single_middle_last_pos
                    self.single_middle_last_pos = pos

                    hbar = self.single_scroll.horizontalScrollBar()
                    vbar = self.single_scroll.verticalScrollBar()
                    hbar.setValue(hbar.value() - int(delta.x()))
                    vbar.setValue(vbar.value() - int(delta.y()))
                    return True

            if event.type() == QEvent.MouseButtonRelease:
                if event.button() == Qt.MiddleButton:
                    self.single_middle_dragging = False
                    self.single_middle_last_pos = None
                    self.single_scroll.viewport().unsetCursor()
                    return True

        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_Z:
            self.undo_annotation()
            return
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_Y:
            self.redo_annotation()
            return
        if self.stack.currentWidget() is self.single_page:
            if event.key() == Qt.Key_Left:
                self.previous_page()
                return
            if event.key() == Qt.Key_Right:
                self.next_page()
                return
            if event.key() == Qt.Key_Delete:
                if self.single_label.delete_selected_annotation():
                    self.mark_dirty()
                else:
                    self.delete_selected()
                return
            if event.key() == Qt.Key_Space:
                self.toggle_selected_favorite()
                return
        super().keyPressEvent(event)

    def _thumbnail_key(self, entry):
        return (
            normalize_path(entry.source_path),
            int(entry.source_index),
            int(entry.extra_rotation) % 360,
        )

    def _thumbnail_dimensions(self):
        scale = self.thumbnail_zoom_percent / 100.0
        icon_w = max(81, int(round(135 * scale)))
        icon_h = max(105, int(round(175 * scale)))
        grid_w = max(icon_w + 28, int(round(175 * scale)))
        grid_h = max(icon_h + 42, int(round(225 * scale)))
        return icon_w, icon_h, grid_w, grid_h

    def change_thumbnail_zoom(self, delta):
        new_value = max(60, min(180, self.thumbnail_zoom_percent + delta))
        if new_value == self.thumbnail_zoom_percent:
            return
        self.thumbnail_zoom_percent = new_value

        icon_w, icon_h, grid_w, grid_h = self._thumbnail_dimensions()
        self.list.setIconSize(QSize(icon_w, icon_h))
        self.list.setGridSize(QSize(grid_w, grid_h))
        self.list.setSpacing(max(2, int(4 * self.thumbnail_zoom_percent / 100.0)))

        # 選択状態は維持したまま、現在倍率用サムネイルだけ再生成。
        self._thumbnail_cache.clear()
        self._thumbnail_queue.clear()
        self._thumbnail_busy = False
        placeholder = self._placeholder_thumbnail()
        for row in range(self.list.count()):
            self.list.item(row).setIcon(placeholder)
        QTimer.singleShot(0, self.schedule_visible_thumbnails)

    def _placeholder_thumbnail(self):
        icon_w, icon_h, _gw, _gh = self._thumbnail_dimensions()
        pix = QPixmap(icon_w, icon_h)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        margin = max(2, int(round(3 * self.thumbnail_zoom_percent / 100.0)))
        painter.fillRect(
            margin, margin,
            max(1, icon_w - margin * 2),
            max(1, icon_h - margin * 2),
            Qt.lightGray
        )
        pen = QPen(QColor('#8a8a8a'))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(1, 1, max(1, icon_w - 3), max(1, icon_h - 3))
        painter.end()
        return pix

    def _frame_thumbnail(self, source_pixmap):
        icon_w, icon_h, _gw, _gh = self._thumbnail_dimensions()
        canvas = QPixmap(icon_w, icon_h)
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        inner_w = max(1, icon_w - 6)
        inner_h = max(1, icon_h - 6)
        scaled = source_pixmap.scaled(
            inner_w, inner_h, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        x = (icon_w - scaled.width()) // 2
        y = (icon_h - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        pen = QPen(QColor('#8a8a8a'))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(1, 1, max(1, icon_w - 3), max(1, icon_h - 3))
        painter.end()
        return canvas

    def make_thumbnail(self, entry):
        key = self._thumbnail_key(entry)
        cached = self._thumbnail_cache.get(key)
        if cached is not None:
            self._thumbnail_cache.move_to_end(key)
            return cached
        try:
            image = render_page_image(
                entry.source_path, entry.source_index, max(40, int(55 * self.thumbnail_zoom_percent / 100.0)), entry.extra_rotation
            )
            pix = self._frame_thumbnail(QPixmap.fromImage(image))
        except Exception:
            pix = self._placeholder_thumbnail()

        self._thumbnail_cache[key] = pix
        self._thumbnail_cache.move_to_end(key)
        while len(self._thumbnail_cache) > 120:
            self._thumbnail_cache.popitem(last=False)
        return pix

    def apply_thumbnail_item_style(self, item, entry):
        """挿入ページは赤文字＋太字＋ページ番号枠で識別する。"""
        font = item.font()
        if entry is not None and getattr(entry, 'inserted', False):
            font.setBold(True)
            item.setForeground(QBrush(QColor('#d80000')))
        else:
            font.setBold(False)
            item.setForeground(QBrush())
        item.setFont(font)

    def update_item_label(self, row):
        if not (0 <= row < self.list.count()):
            return
        entry = self.list.item(row).data(Qt.UserRole)
        if entry is None:
            return
        label = f'{row + 1}'
        if entry.favorite:
            label = f'⭐ {label}'
        if entry.annotations:
            label = f'📝 {label}'
        if entry.extra_rotation % 360:
            label += f'\n↻ {entry.final_rotation}°'
        item = self.list.item(row)
        item.setText(label)
        self.apply_thumbnail_item_style(item, entry)

    def schedule_visible_thumbnails(self):
        if self.list.count() == 0:
            return
        viewport_rect = self.list.viewport().rect()
        wanted = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            if self.list.visualItemRect(item).intersects(viewport_rect):
                key = self._thumbnail_key(item.data(Qt.UserRole))
                if key not in self._thumbnail_cache:
                    wanted.append(row)

        self._thumbnail_queue = wanted
        if wanted and not self._thumbnail_busy:
            self._thumbnail_busy = True
            QTimer.singleShot(0, self._render_next_thumbnail)

    def _render_next_thumbnail(self):
        if not self._thumbnail_queue:
            self._thumbnail_busy = False
            return
        row = self._thumbnail_queue.pop(0)
        if 0 <= row < self.list.count():
            item = self.list.item(row)
            entry = item.data(Qt.UserRole)
            item.setIcon(self.make_thumbnail(entry))
        QTimer.singleShot(0, self._render_next_thumbnail)

    def rebuild_list(self, preserve_row=None):
        if preserve_row is None:
            preserve_row = self.current_index

        # 旧リスト用の遅延サムネイル処理が残ると、
        # 新規挿入後にプレースホルダのまま止まるため必ずリセット。
        self._thumbnail_queue = []
        self._thumbnail_busy = False

        self.list.blockSignals(True)
        self.list.clear()
        placeholder = self._placeholder_thumbnail()

        for i, entry in enumerate(self.entries):
            label = f'{i + 1}'
            if entry.favorite:
                label = f'⭐ {label}'
            if entry.annotations:
                label = f'📝 {label}'
            if entry.extra_rotation % 360:
                label += f'\n↻ {entry.final_rotation}°'

            key = self._thumbnail_key(entry)
            cached_icon = self._thumbnail_cache.get(key)
            icon = cached_icon if cached_icon is not None else placeholder

            item = QListWidgetItem(icon, label)
            item.setData(Qt.UserRole, entry)
            self.apply_thumbnail_item_style(item, entry)
            item.setFlags(
                item.flags()
                | Qt.ItemIsDragEnabled
                | Qt.ItemIsDropEnabled
                | Qt.ItemIsSelectable
                | Qt.ItemIsEnabled
            )
            self.list.addItem(item)

        if self.entries:
            preserve_row = max(
                0, min(int(preserve_row), len(self.entries) - 1)
            )
            self.current_index = preserve_row
            self.list.setCurrentRow(preserve_row)
        else:
            self.current_index = 0

        self.list.blockSignals(False)
        QTimer.singleShot(0, self.schedule_visible_thumbnails)

    def select_inserted_thumbnail_range(self, start, count):
        """
        挿入直後のページ群を count 件すべて選択し、
        サムネイルへ操作フォーカスを置く。
        """
        if count <= 0 or self.list.count() == 0:
            return

        start = max(0, min(int(start), self.list.count() - 1))
        count = max(0, int(count))
        end_exclusive = min(self.list.count(), start + count)

        if end_exclusive <= start:
            return

        self.current_index = start

        # 先にサムネイルモードへ。
        self.show_thumbnail_mode()

        self.list.blockSignals(True)
        try:
            self.list.clearSelection()

            # Current設定を先に行う。
            # ExtendedSelectionではsetCurrentRow()が既存選択を変更する場合が
            # あるため、複数選択を作る前に済ませる。
            self.list.setCurrentRow(start)

            # 挿入した start ～ start+count-1 を全件選択。
            for row in range(start, end_exclusive):
                item = self.list.item(row)
                if item is not None:
                    item.setSelected(True)

            first = self.list.item(start)
            if first is not None:
                self.list.scrollToItem(
                    first,
                    QAbstractItemView.PositionAtCenter
                )
        finally:
            self.list.blockSignals(False)

        self.current_index = start

        # Delete / Space / Ctrl+ホイールを挿入直後から受け付ける。
        self.list.setFocus(Qt.OtherFocusReason)


    def sync_entries_from_items(self):
        ordered = []
        for i in range(self.list.count()):
            entry = self.list.item(i).data(Qt.UserRole)
            if entry is not None:
                ordered.append(entry)

        if len(ordered) != len(self.entries):
            return False

        self.entries = ordered
        for i in range(len(self.entries)):
            self.update_item_label(i)
        return True

    def on_rows_moved(self):
        before = [id(e) for e in self.entries]
        if not self.sync_entries_from_items():
            return
        after = [id(e) for e in self.entries]
        if before != after:
            self.mark_dirty()
        self.current_index = max(0, self.list.currentRow())
        QTimer.singleShot(0, self.schedule_visible_thumbnails)

    @staticmethod
    def _editable_pdf_annotation_name(annot):
        """FastPDFが編集対象にするPDF注釈種別名を返す。"""
        try:
            name = str(annot.type[1] or '').strip().lower()
        except Exception:
            return ''
        if name in ('square', 'circle', 'line', 'freetext'):
            return name
        return ''

    @staticmethod
    def _annot_info(annot):
        try:
            info = annot.info or {}
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}

    def _pdf_annotation_to_item(self, page, annot):
        """
        対応PDF AnnotationをFastPDFのAnnotationItemへ変換する。
        座標は単一ページキャンバスと同じ0～1正規化座標。
        未対応注釈はNoneを返し、PDF上では表示だけに留める。
        """
        name = self._editable_pdf_annotation_name(annot)
        if not name:
            return None

        info = self._annot_info(annot)
        subject = str(info.get('subject') or '')
        title = str(info.get('title') or '')

        colors = {}
        try:
            colors = annot.colors or {}
            if not isinstance(colors, dict):
                colors = {}
        except Exception:
            colors = {}

        def color_to_hex(value, fallback='#ff0000'):
            try:
                if value and len(value) >= 3:
                    r = max(0, min(255, int(round(float(value[0]) * 255))))
                    g = max(0, min(255, int(round(float(value[1]) * 255))))
                    b = max(0, min(255, int(round(float(value[2]) * 255))))
                    return QColor(r, g, b).name()
            except Exception:
                pass
            return fallback

        annot_color = color_to_hex(
            colors.get('stroke') or colors.get('fill'),
            '#ff0000'
        )
        has_white_fill = bool(colors.get('fill'))

        # FastPDFの枠付きテキストは、
        # Square + FreeText の2注釈で保存している。
        # 枠側まで独立オブジェクト化すると二重になるため読み飛ばす。
        if (
            name == 'square'
            and title == 'FastPDF'
            and subject == 'FastPDF text box'
        ):
            return None

        vr = page.rect
        rm = page.rotation_matrix

        def to_norm(pt):
            try:
                rp = fitz.Point(pt) * rm
            except Exception:
                rp = fitz.Point(pt)
            if vr.width <= 0 or vr.height <= 0:
                return 0.0, 0.0
            return (
                max(0.0, min(1.0, (rp.x - vr.x0) / vr.width)),
                max(0.0, min(1.0, (rp.y - vr.y0) / vr.height)),
            )

        def rect_norm(rect):
            r = fitz.Rect(rect)
            corners = [
                fitz.Point(r.x0, r.y0),
                fitz.Point(r.x1, r.y0),
                fitz.Point(r.x1, r.y1),
                fitz.Point(r.x0, r.y1),
            ]
            pts = [to_norm(p) for p in corners]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return min(xs), min(ys), max(xs), max(ys)

        try:
            angle = float(getattr(annot, 'rotation', 0) or 0)
        except Exception:
            angle = 0.0

        if name == 'line':
            try:
                vertices = list(annot.vertices or [])
            except Exception:
                vertices = []
            if len(vertices) < 2:
                return None

            x1, y1 = to_norm(vertices[0])
            x2, y2 = to_norm(vertices[1])

            kind = 'line'
            try:
                line_ends = annot.line_ends
                if (
                    line_ends
                    and len(line_ends) >= 2
                    and int(line_ends[1]) != int(fitz.PDF_ANNOT_LE_NONE)
                ):
                    kind = 'arrow'
            except Exception:
                pass

            return AnnotationItem(kind, x1, y1, x2, y2, '', 0.0, annot_color, False)

        x1, y1, x2, y2 = rect_norm(annot.rect)

        if name == 'square':
            return AnnotationItem('rect', x1, y1, x2, y2, '', angle, annot_color, has_white_fill)

        if name == 'circle':
            return AnnotationItem('ellipse', x1, y1, x2, y2, '', angle, annot_color, has_white_fill)

        if name == 'freetext':
            text = str(info.get('content') or '')
            kind = 'text_plain'
            fill = None
            try:
                current_colors = annot.colors or {}
                fill = (
                    current_colors.get('fill')
                    if isinstance(current_colors, dict)
                    else None
                )
                # FreeText自体に背景色または境界線があれば「枠付きテキスト」。
                border = annot.border or {}
                border_width = float(border.get('width', 0) or 0) if isinstance(border, dict) else 0.0
                if fill or border_width > 0:
                    kind = 'text'
            except Exception:
                fill = None

            font_size = 12.0
            font_name = 'Helv'
            try:
                text_dict = annot.get_text('dict') or {}
                for block in text_dict.get('blocks', []):
                    for line in block.get('lines', []):
                        for span in line.get('spans', []):
                            if span.get('text'):
                                font_size = float(span.get('size') or 12.0)
                                font_name = normalize_pdf_font_name(span.get('font'))
                                raise StopIteration
            except StopIteration:
                pass
            except Exception:
                pass

            return AnnotationItem(
                kind,
                x1, y1, x2, y2,
                text,
                angle,
                annot_color,
                bool(fill) if kind == 'text' else False,
                font_size,
                font_name,
            )

        return None

    def _ensure_page_annotations_loaded(self, entry):
        """
        単一ページ編集へ入ったページだけ既存PDF注釈を遅延変換する。

        ビューワ・サムネイル・印刷ではこの処理を呼ばないため、
        大量ページPDFでも全注釈解析は行わない。
        """
        if entry.annotations_loaded:
            return

        entry.annotations_loaded = True
        converted = []

        try:
            doc = fitz.open(entry.source_path)
            try:
                page = doc.load_page(entry.source_index)
                annot = page.first_annot
                while annot is not None:
                    item = self._pdf_annotation_to_item(page, annot)
                    if item is not None:
                        converted.append(item)
                    annot = annot.next
            finally:
                doc.close()
        except Exception:
            # 読み込み失敗時もPDF背景自体は表示できるようにする。
            converted = []

        # 既にFastPDF上で未保存の注釈が存在する場合は保持する。
        # 通常は初回ロード時は空。
        if converted:
            if entry.annotations:
                entry.annotations = converted + entry.annotations
            else:
                entry.annotations = converted

    def _remove_editable_pdf_annotations(self, page):
        """
        単一ページ編集で読み込んだページを保存するとき、
        元PDFに既に存在する対応注釈を除去してから
        現在のAnnotationItem群を書き戻す。
        未対応注釈はそのまま残す。
        """
        targets = []
        try:
            annot = page.first_annot
            while annot is not None:
                if self._editable_pdf_annotation_name(annot):
                    targets.append(annot.xref)
                annot = annot.next
        except Exception:
            targets = []

        for xref in targets:
            try:
                annot = page.load_annot(xref)
                if annot is not None:
                    page.delete_annot(annot)
            except Exception:
                continue

    def show_single_page(self):
        if not self.entries:
            self.single_label.setPixmap(QPixmap())
            self.single_label.setText('ページがありません')
            self.single_page_no.setText('0 / 0')
            return

        self.current_index = max(
            0, min(self.current_index, len(self.entries) - 1)
        )
        entry = self.entries[self.current_index]

        # 既存PDF注釈をオブジェクト化するのは単一ページ編集時だけ。
        self._ensure_page_annotations_loaded(entry)
        self.single_label.set_annotations(entry.annotations)
        self.single_label.set_tool(self.annotation_tool)

        # 単一ページ編集の背景だけはPDF注釈を描画しない。
        # 注釈は上のAnnotationCanvasで編集可能オブジェクトとして描画する。
        detail_key = (normalize_path(entry.source_path), int(entry.source_index))
        render_dpi = int(self._single_detail_dpi.get(detail_key, 120))
        key = ('single-edit', render_dpi) + tuple(self._thumbnail_key(entry))

        pix = self._single_cache.get(key)
        if pix is None:
            try:
                image = render_page_image(
                    entry.source_path,
                    entry.source_index,
                    render_dpi,
                    entry.extra_rotation,
                    include_annotations=False
                )
                pix = QPixmap.fromImage(image)
            except Exception:
                pix = QPixmap()

            self._single_cache[key] = pix
            self._single_cache.move_to_end(key)
            while len(self._single_cache) > 12:
                self._single_cache.popitem(last=False)

        if pix.isNull():
            self.single_label.setPixmap(QPixmap())
            self.single_label.setText('表示できません')
        else:
            viewport = self.single_scroll.viewport().size()
            fit_w = max(300, viewport.width() - 20)
            fit_h = max(300, viewport.height() - 20)

            fitted = pix.scaled(
                fit_w,
                fit_h,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )

            factor = self.single_zoom_percent / 100.0
            target_size = QSize(
                max(1, int(fitted.width() * factor)),
                max(1, int(fitted.height() * factor))
            )
            shown = pix.scaled(
                target_size,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )

            self.single_label.setText('')
            self.single_label.setPixmap(shown)
            self.single_label.resize(shown.size())

        fav = '⭐ ' if entry.favorite else ''
        self.single_page_no.setText(
            f'{fav}{self.current_index + 1} / {len(self.entries)}'
        )

    def refresh_current_single_high_quality(self):
        """単一ページの現在ページだけを一時的に高精細表示する。

        A1/A0で固定300DPIにするとメモリ消費が大きすぎるため、
        長辺約4500pxを目安に120～300DPIの範囲で自動決定する。
        PDF本体や設定ファイルには保存しない。
        """
        if not self.entries:
            return
        self.current_index = max(0, min(self.current_index, len(self.entries) - 1))
        entry = self.entries[self.current_index]
        try:
            doc = fitz.open(entry.source_path)
            try:
                page = doc.load_page(entry.source_index)
                long_pt = max(float(page.rect.width), float(page.rect.height), 1.0)
            finally:
                doc.close()
        except Exception:
            long_pt = 842.0

        detail_dpi = int(round(4500.0 * 72.0 / long_pt))
        detail_dpi = max(120, min(300, detail_dpi))
        detail_key = (normalize_path(entry.source_path), int(entry.source_index))
        self._single_detail_dpi[detail_key] = detail_dpi

        # 同じページの旧単一ページキャッシュだけを捨て、高精細版へ差し替える。
        for cache_key in list(self._single_cache.keys()):
            if cache_key and cache_key[0] == 'single-edit':
                try:
                    if normalize_path(entry.source_path) in [str(x) for x in cache_key]:
                        self._single_cache.pop(cache_key, None)
                except Exception:
                    pass
        self.show_single_page()

    def _annotation_state(self):
        return [
            [a.copy() for a in entry.annotations]
            for entry in self.entries
        ]

    def _restore_annotation_state(self, state):
        for i, entry in enumerate(self.entries):
            entry.annotations = [a.copy() for a in (state[i] if i < len(state) else [])]
        self.single_label.clear_selection()
        self.mark_dirty()
        self.show_single_page()

    def _push_annotation_undo(self, state=None):
        self._annotation_undo.append(state if state is not None else self._annotation_state())
        if len(self._annotation_undo) > 50:
            self._annotation_undo.pop(0)
        self._annotation_redo.clear()

    def _save_annotation_style_settings(self):
        self.editor_settings['annotation_color'] = self.annotation_color
        self.editor_settings['annotation_fill_white'] = bool(
            self.annotation_fill_white
        )
        self.editor_settings['annotation_font_size'] = float(
            self.annotation_font_size
        )
        self.editor_settings['annotation_font_name'] = normalize_pdf_font_name(
            self.annotation_font_name
        )
        save_editor_settings(self.editor_settings)

    def _update_annotation_font_buttons(self):
        if hasattr(self, 'annotation_font_size_btn'):
            size = float(getattr(self, 'annotation_font_size', 12.0))
            size_text = str(int(size)) if size.is_integer() else f'{size:g}'
            self.annotation_font_size_btn.setToolTip(
                f'フォントサイズ変更（現在の既定: {size_text} pt）'
            )
        if hasattr(self, 'annotation_font_btn'):
            name = normalize_pdf_font_name(
                getattr(self, 'annotation_font_name', 'Helv')
            )
            label = PDF_FONT_NAME_TO_LABEL.get(name, 'Helvetica')
            self.annotation_font_btn.setToolTip(
                f'フォント変更（現在の既定: {label}）'
            )

    def _selected_text_annotation_indices(self):
        if not self.entries or not hasattr(self, 'single_label'):
            return []
        anns = self.entries[self.current_index].annotations
        return [
            i for i in sorted(self.single_label.selected_indices)
            if 0 <= i < len(anns) and anns[i].kind in ('text', 'text_white', 'text_plain')
        ]

    def change_annotation_font_size(self):
        selected = self._selected_text_annotation_indices()
        anns = self.entries[self.current_index].annotations if self.entries else []
        initial = self.annotation_font_size
        if selected:
            initial = float(getattr(anns[selected[0]], 'font_size', initial))
        value, ok = QInputDialog.getDouble(
            self, 'フォントサイズ', 'サイズ (pt):',
            float(initial), 1.0, 72.0, 1
        )
        if not ok:
            return
        value = max(1.0, min(72.0, float(value)))
        if selected:
            self._push_annotation_undo()
            for i in selected:
                anns[i].font_size = value
            self.mark_dirty()
            self.single_label.set_annotations(anns)
            self.single_label.update()
        else:
            self.annotation_font_size = value
            self._save_annotation_style_settings()
        self.annotation_font_size = value
        self._save_annotation_style_settings()
        self._update_annotation_font_buttons()

    def change_annotation_font(self):
        selected = self._selected_text_annotation_indices()
        anns = self.entries[self.current_index].annotations if self.entries else []
        current_name = self.annotation_font_name
        if selected:
            current_name = normalize_pdf_font_name(
                getattr(anns[selected[0]], 'font_name', current_name)
            )
        current_label = PDF_FONT_NAME_TO_LABEL.get(current_name, 'Helvetica')
        labels = [label for label, _ in PDF_FONT_CHOICES]
        try:
            current_index = labels.index(current_label)
        except ValueError:
            current_index = 0
        label, ok = QInputDialog.getItem(
            self, 'フォント', 'PDF標準フォント:', labels, current_index, False
        )
        if not ok or not label:
            return
        name = PDF_FONT_LABEL_TO_NAME.get(label, 'Helv')
        if selected:
            self._push_annotation_undo()
            for i in selected:
                anns[i].font_name = name
            self.mark_dirty()
            self.single_label.set_annotations(anns)
            self.single_label.update()
        self.annotation_font_name = name
        self._save_annotation_style_settings()
        self._update_annotation_font_buttons()

    def _update_annotation_color_button(self):
        if not hasattr(self, 'annotation_color_btn'):
            return
        q = QColor(self.annotation_color)
        if not q.isValid():
            q = QColor('#ff0000')

        pix = QPixmap(22, 22)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(q)
        p.setPen(QPen(QColor('#666666'), 1))
        p.drawRoundedRect(QRectF(2, 2, 18, 18), 3, 3)
        p.end()

        self.annotation_color_btn.setIcon(QIcon(pix))
        self.annotation_color_btn.setIconSize(QSize(22, 22))

    def choose_annotation_color(self):
        initial = QColor(self.annotation_color)

        # Qt標準カラーダイアログを使い、Basic colorsをFastPDF向けに揃える。
        # setStandardColor() はNative Dialogには反映されない場合があるため、
        # DontUseNativeDialog を指定する。
        basic_colors = [
            '#ff0000',  # red / FastPDF default
            '#00ff00',  # green
            '#0000ff',  # blue
            '#ffff00',  # yellow
            '#00ffff',  # cyan
            '#ff00ff',  # magenta
            '#000000',  # black
            '#ffffff',  # white
            '#ff8000',  # orange
            '#8000ff',  # purple
            '#804000',  # brown
            '#808080',  # gray
            '#800000',  # dark red
            '#008000',  # dark green
            '#000080',  # dark blue
        ]

        for index, value in enumerate(basic_colors):
            q = QColor(value)
            if q.isValid():
                QColorDialog.setStandardColor(index, q)

        dialog = QColorDialog(initial, self)
        dialog.setWindowTitle('朱書き色')
        dialog.setOption(
            QColorDialog.DontUseNativeDialog,
            True
        )

        if dialog.exec() != QDialog.Accepted:
            return

        chosen = dialog.selectedColor()
        if not chosen.isValid():
            return

        new_color = chosen.name()
        selected = (
            set(self.single_label.selected_indices)
            if hasattr(self, 'single_label')
            else set()
        )

        if selected and self.entries:
            anns = self.entries[self.current_index].annotations
            targets = [
                i for i in selected
                if 0 <= i < len(anns)
            ]
            if targets:
                self._push_annotation_undo()
                for i in targets:
                    anns[i].color = new_color
                self.mark_dirty()
                self.single_label.update()

        self.annotation_color = new_color
        self._update_annotation_color_button()
        self._save_annotation_style_settings()

        if hasattr(self, 'single_label'):
            self.single_label.set_drawing_style(
                color=self.annotation_color
            )

    def toggle_annotation_white_fill(self):
        selected = (
            set(self.single_label.selected_indices)
            if hasattr(self, 'single_label')
            else set()
        )

        applicable = []
        if selected and self.entries:
            anns = self.entries[self.current_index].annotations
            applicable = [
                i for i in selected
                if 0 <= i < len(anns)
                and anns[i].kind in ('rect', 'ellipse')
            ]

        if applicable:
            anns = self.entries[self.current_index].annotations

            # すべて白塗りなら一括解除。
            # 1つでも未塗りがあれば、選択図形をすべて白塗りに揃える。
            new_state = not all(
                bool(getattr(anns[i], 'fill_white', False))
                for i in applicable
            )

            self._push_annotation_undo()
            for i in applicable:
                anns[i].fill_white = new_state

            self.annotation_fill_white = new_state
            self.annotation_fill_btn.setChecked(new_state)
            self._save_annotation_style_settings()
            self.mark_dirty()
            self.single_label.set_drawing_style(
                fill_white=new_state
            )
            self.single_label.update()
            return

        # 選択図形が無いときは「次に作る四角・楕円」の既定値を切替。
        self.annotation_fill_white = not self.annotation_fill_white
        self.annotation_fill_btn.setChecked(self.annotation_fill_white)
        self._save_annotation_style_settings()
        if hasattr(self, 'single_label'):
            self.single_label.set_drawing_style(
                fill_white=self.annotation_fill_white
            )

    def set_annotation_tool(self, tool):
        self.annotation_tool = tool
        if hasattr(self, 'single_label'):
            self.single_label.set_tool(tool)
        if self.stack.currentWidget() is not self.single_page:
            self.show_single_mode()

    def on_annotation_changed(self):
        # Canvas変更後に呼ばれるため、直前状態を簡易復元用に構成する。
        # 作成・移動は現在状態から対象操作を1段戻したスナップショットを取れないため、
        # mouse press 時に保持した状態があれば優先する。
        before = self._annotation_snapshot_before_action
        if before is not None:
            self._push_annotation_undo(before)
            self._annotation_snapshot_before_action = None
        else:
            # 削除などDialog側操作では事前にpush済み。
            pass
        self.mark_dirty()

    def add_text_annotation(self, nx, ny):
        if not self.entries:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, 'テキスト注記', '文字列:'
        )
        if not ok or not text.strip():
            return
        self._push_annotation_undo()
        w, h = 0.28, 0.10
        x2 = min(1.0, nx + w)
        y2 = min(1.0, ny + h)
        self.entries[self.current_index].annotations.append(
            AnnotationItem(
                'text', nx, ny, x2, y2, text.strip(),
                color=self.annotation_color,
                fill_white=False,
                font_size=self.annotation_font_size,
                font_name=self.annotation_font_name,
            )
        )
        self.single_label.set_annotations(self.entries[self.current_index].annotations)
        new_idx = len(self.entries[self.current_index].annotations) - 1
        self.single_label.set_selected_indices({new_idx}, new_idx)
        self.mark_dirty()
        self.single_label.update()

    def add_white_text_annotation(self, nx, ny):
        if not self.entries:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, '文字注記', '文字列:'
        )
        if not ok or not text.strip():
            return
        self._push_annotation_undo()
        w, h = 0.28, 0.10
        x2 = min(1.0, nx + w)
        y2 = min(1.0, ny + h)
        self.entries[self.current_index].annotations.append(
            AnnotationItem(
                'text_white', nx, ny, x2, y2, text.strip(),
                color=self.annotation_color,
                fill_white=True,
                font_size=self.annotation_font_size,
                font_name=self.annotation_font_name,
            )
        )
        self.single_label.set_annotations(self.entries[self.current_index].annotations)
        new_idx = len(self.entries[self.current_index].annotations) - 1
        self.single_label.set_selected_indices({new_idx}, new_idx)
        self.mark_dirty()
        self.single_label.update()

    def add_plain_text_annotation(self, nx, ny):
        if not self.entries:
            return
        value, ok = QInputDialog.getMultiLineText(
            self, '文字注記', '文字列:'
        )
        if not ok or not value.strip():
            return
        self._push_annotation_undo()
        w, h = 0.28, 0.08
        x2 = min(1.0, nx + w)
        y2 = min(1.0, ny + h)
        self.entries[self.current_index].annotations.append(
            AnnotationItem(
                'text_plain', nx, ny, x2, y2, value.strip(),
                color=self.annotation_color,
                fill_white=False,
                font_size=self.annotation_font_size,
                font_name=self.annotation_font_name,
            )
        )
        self.single_label.set_annotations(
            self.entries[self.current_index].annotations
        )
        new_idx = len(self.entries[self.current_index].annotations) - 1
        self.single_label.set_selected_indices({new_idx}, new_idx)
        self.mark_dirty()
        self.single_label.update()

    def rotate_selected_annotation(self, degrees):
        if not self.entries or self.stack.currentWidget() is not self.single_page:
            return
        idx = self.single_label.selected_index
        anns = self.entries[self.current_index].annotations
        if not (0 <= idx < len(anns)):
            return
        self._push_annotation_undo()
        anns[idx].angle = (anns[idx].angle + degrees) % 360
        self.mark_dirty()
        self.single_label.update()

    def edit_text_annotation(self, index):
        if not self.entries:
            return

        anns = self.entries[self.current_index].annotations
        if not (0 <= index < len(anns)):
            return
        if anns[index].kind not in ('text', 'text_white', 'text_plain'):
            return

        text, ok = QInputDialog.getMultiLineText(
            self,
            'テキスト注記を編集',
            '文字列:',
            anns[index].text
        )
        if not ok:
            return

        self._push_annotation_undo()

        if not text.strip():
            del anns[index]
            self.single_label.clear_selection()
        else:
            anns[index].text = text.strip()

        self.mark_dirty()
        self.single_label.set_annotations(anns)
        self.single_label.update()

    def delete_selected_annotation(self):
        if not self.entries or self.stack.currentWidget() is not self.single_page:
            return
        if self.single_label.selected_indices:
            self._push_annotation_undo()
            self.single_label.delete_selected_annotation()
            self.mark_dirty()

    def undo_annotation(self):
        if not self._annotation_undo:
            return
        current = self._annotation_state()
        state = self._annotation_undo.pop()
        self._annotation_redo.append(current)
        self._restore_annotation_state(state)

    def redo_annotation(self):
        if not self._annotation_redo:
            return
        current = self._annotation_state()
        state = self._annotation_redo.pop()
        self._annotation_undo.append(current)
        self._restore_annotation_state(state)

    def first_page(self):
        if self.entries:
            self.current_index = 0
            self.list.setCurrentRow(0)
            self.show_single_page()

    def previous_page(self):
        if self.entries and self.current_index > 0:
            self.current_index -= 1
            self.list.setCurrentRow(self.current_index)
            self.show_single_page()

    def next_page(self):
        if self.entries and self.current_index < len(self.entries) - 1:
            self.current_index += 1
            self.list.setCurrentRow(self.current_index)
            self.show_single_page()

    def last_page(self):
        if self.entries:
            self.current_index = len(self.entries) - 1
            self.list.setCurrentRow(self.current_index)
            self.show_single_page()

    def selected_rows(self):
        if self.stack.currentWidget() is self.single_page:
            return [self.current_index] if self.entries else []
        return sorted(
            self.list.row(item) for item in self.list.selectedItems()
        )

    def delete_selected(self):
        self.sync_entries_from_items()
        rows = self.selected_rows()
        if not rows:
            return

        count = len(rows)
        if count == 1:
            message = f'{rows[0] + 1}ページ目を削除しますか？'
        else:
            message = f'選択した{count}ページを削除しますか？'

        result = QMessageBox.question(
            self,
            'ページ削除',
            message + '\n\nこの操作はPDF保存前でも編集状態に反映されます。',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if result != QMessageBox.Yes:
            return

        first = rows[0]
        for row in reversed(rows):
            if 0 <= row < len(self.entries):
                del self.entries[row]

        self.current_index = min(first, max(0, len(self.entries) - 1))
        self.mark_dirty()
        self.rebuild_list(self.current_index)
        self.show_single_page()

    def rotate_selected(self, degrees):
        self.sync_entries_from_items()
        rows = self.selected_rows()
        if not rows:
            return

        for row in rows:
            if not (0 <= row < len(self.entries)):
                continue
            entry = self.entries[row]
            old_key = self._thumbnail_key(entry)
            entry.extra_rotation = (entry.extra_rotation + degrees) % 360
            self._thumbnail_cache.pop(old_key, None)
            self._single_cache.pop(old_key, None)
            self._thumbnail_cache.pop(self._thumbnail_key(entry), None)
            self._single_cache.pop(self._thumbnail_key(entry), None)

        self.mark_dirty()

        if self.stack.currentWidget() is self.thumb_page:
            placeholder = self._placeholder_thumbnail()
            for row in rows:
                if 0 <= row < self.list.count():
                    self.list.item(row).setIcon(placeholder)
                    self.update_item_label(row)
            QTimer.singleShot(0, self.schedule_visible_thumbnails)
        else:
            self.show_single_page()

    def toggle_selected_favorite(self):
        self.sync_entries_from_items()
        rows = self.selected_rows()
        if not rows:
            return

        new_state = not all(
            self.entries[row].favorite
            for row in rows
            if 0 <= row < len(self.entries)
        )

        for row in rows:
            if 0 <= row < len(self.entries):
                self.entries[row].favorite = new_state
                self.update_item_label(row)

        # お気に入りはPDF編集とは別管理。変更した時点で即時保存する。
        pages = {
            i for i, entry in enumerate(self.entries)
            if entry.favorite
        }
        self.favorites.set_pages(self.pdf_path, pages)
        if not self.favorites.save():
            QMessageBox.warning(
                self,
                'お気に入り',
                'favorites.json を保存できませんでした。'
            )

        self.show_single_page()

    def clear_all_favorites(self):
        if not self.entries:
            return

        if not any(entry.favorite for entry in self.entries):
            return

        for row, entry in enumerate(self.entries):
            entry.favorite = False
            if row < self.list.count():
                self.update_item_label(row)

        self.favorites.set_pages(self.pdf_path, set())
        if not self.favorites.save():
            QMessageBox.warning(
                self,
                'お気に入り',
                'favorites.json を保存できませんでした。'
            )

        self.show_single_page()

    def add_pages_from_pdf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'PDF', '', 'PDF (*.pdf)'
        )
        if not path:
            return

        path = normalize_path(path)
        try:
            doc = fitz.open(path)
            page_count = doc.page_count
            doc.close()
        except Exception as e:
            QMessageBox.critical(self, 'PDF', str(e))
            return

        spec, ok = QInputDialog.getText(
            self,
            'ページ追加',
            f'追加ページ 例: 1-5,8\n全ページなら空欄（1～{page_count}）'
        )
        if not ok:
            return

        try:
            pages = (
                list(range(page_count))
                if not spec.strip()
                else parse_page_spec(spec, page_count)
            )
        except ValueError as e:
            QMessageBox.warning(self, 'ページ指定', str(e))
            return

        src = fitz.open(path)
        try:
            new_entries = [
                PageEntry(path, idx, int(src.load_page(idx).rotation), 0, False, inserted=True)
                for idx in pages
            ]
        finally:
            src.close()

        self.sync_entries_from_items()
        rows = self.selected_rows()
        insert_at = rows[-1] + 1 if rows else len(self.entries)

        for offset, entry in enumerate(new_entries):
            self.entries.insert(insert_at + offset, entry)

        self.current_index = min(
            insert_at, max(0, len(self.entries) - 1)
        )
        self.mark_dirty()
        self.rebuild_list(self.current_index)
        self.select_inserted_thumbnail_range(
            insert_at,
            len(new_entries)
        )

    def _apply_annotations_to_page(self, page, annotations):
        """FastPDFの朱書きをPDF Annotationとして追加する。"""
        if not annotations:
            return
        white = (1.0, 1.0, 1.0)
        vr = page.rect

        def pdf_color(value):
            q = QColor(str(value))
            if not q.isValid():
                q = QColor('#ff0000')
            return (q.redF(), q.greenF(), q.blueF())
        drm = page.derotation_matrix

        def point(nx, ny):
            return fitz.Point(
                vr.x0 + nx * vr.width,
                vr.y0 + ny * vr.height
            ) * drm

        def rotated_norm_points(a):
            cx = (a.x1 + a.x2) * 0.5
            cy = (a.y1 + a.y2) * 0.5
            rad = math.radians(a.angle)
            cs, sn = math.cos(rad), math.sin(rad)
            def rot(x, y):
                dx, dy = x-cx, y-cy
                return cx + dx*cs - dy*sn, cy + dx*sn + dy*cs
            return rot(a.x1,a.y1), rot(a.x2,a.y2)

        for a in annotations:
            try:
                stroke_color = pdf_color(
                    getattr(a, 'color', '#ff0000')
                )
                (rx1, ry1), (rx2, ry2) = rotated_norm_points(a)
                p1 = point(rx1, ry1)
                p2 = point(rx2, ry2)
                annot = None

                if a.kind == 'line':
                    annot = page.add_line_annot(p1, p2)
                    annot.set_colors(stroke=stroke_color)
                    annot.set_border(width=2.0)

                elif a.kind == 'arrow':
                    annot = page.add_line_annot(p1, p2)
                    annot.set_colors(stroke=stroke_color)
                    annot.set_border(width=2.0)
                    annot.set_line_ends(
                        fitz.PDF_ANNOT_LE_NONE,
                        fitz.PDF_ANNOT_LE_OPEN_ARROW
                    )

                elif a.kind in ('rect', 'ellipse', 'text', 'text_white', 'text_plain'):
                    q1 = point(min(a.x1, a.x2), min(a.y1, a.y2))
                    q2 = point(max(a.x1, a.x2), max(a.y1, a.y2))
                    r = fitz.Rect(q1, q2).normalize()
                    rot = int(round(a.angle)) % 360

                    if a.kind == 'rect':
                        annot = page.add_rect_annot(r)
                        annot.set_colors(
                            stroke=stroke_color,
                            fill=white if getattr(a, 'fill_white', False) else None
                        )
                        annot.set_border(width=2.0)
                        if rot:
                            annot.set_rotation(rot)

                    elif a.kind == 'ellipse':
                        annot = page.add_circle_annot(r)
                        annot.set_colors(
                            stroke=stroke_color,
                            fill=white if getattr(a, 'fill_white', False) else None
                        )
                        annot.set_border(width=2.0)
                        if rot:
                            annot.set_rotation(rot)

                    elif a.kind in ('text', 'text_white'):
                        # T□ = 透明背景＋枠線、T■ = 白背景＋枠線。
                        # どちらもFreeText注釈1個として保存する。
                        use_white_fill = (
                            a.kind == 'text_white'
                            or bool(getattr(a, 'fill_white', False))
                        )
                        annot = page.add_freetext_annot(
                            r, a.text,
                            fontsize=max(1.0, float(getattr(a, 'font_size', 12.0))),
                            fontname=normalize_pdf_font_name(getattr(a, 'font_name', 'Helv')),
                            text_color=stroke_color,
                            fill_color=white if use_white_fill else None,
                            border_width=1.2,
                            align=fitz.TEXT_ALIGN_LEFT,
                            rotate=rot,
                        )

                    else:
                        annot = page.add_freetext_annot(
                            r, a.text,
                            fontsize=max(1.0, float(getattr(a, 'font_size', 12.0))),
                            fontname=normalize_pdf_font_name(getattr(a, 'font_name', 'Helv')),
                            text_color=stroke_color,
                            fill_color=None,
                            border_width=0,
                            align=fitz.TEXT_ALIGN_LEFT,
                            rotate=rot,
                        )

                if annot is not None:
                    annot.set_info(
                        title='FastPDF',
                        subject='FastPDF redline'
                    )
                    annot.update()
            except Exception:
                continue

    def _write_entries(self, output_path):
        if not self.entries:
            QMessageBox.warning(self, '保存', '保存するページがありません。')
            return False

        out = fitz.open()
        docs = {}
        try:
            for entry in self.entries:
                src = docs.get(entry.source_path)
                if src is None:
                    src = fitz.open(entry.source_path)
                    docs[entry.source_path] = src

                out.insert_pdf(
                    src,
                    from_page=entry.source_index,
                    to_page=entry.source_index
                )
                out_page = out.load_page(out.page_count - 1)
                out_page.set_rotation(entry.final_rotation)

                if entry.annotations_loaded:
                    # 単一ページ編集でオブジェクト化したページだけ置換。
                    # 一度も単一ページ編集していないページは、
                    # insert_pdfがコピーした元注釈をそのまま保持する。
                    self._remove_editable_pdf_annotations(out_page)
                    self._apply_annotations_to_page(
                        out_page,
                        entry.annotations
                    )

            out.save(output_path, garbage=4, deflate=True)

        except Exception as e:
            QMessageBox.critical(
                self, 'PDF保存', f'PDFを保存できませんでした.\n{e}'
            )
            return False

        finally:
            try:
                out.close()
            except Exception:
                pass
            for doc in docs.values():
                try:
                    doc.close()
                except Exception:
                    pass

        return True

    def save_overwrite(self):
        self.sync_entries_from_items()
        if not self.dirty:
            QMessageBox.information(
                self, '上書き保存', '未保存の編集内容はありません。'
            )
            return

        if QMessageBox.question(
            self,
            '上書き保存',
            f'編集内容で元PDFを上書きしますか？\n\n{self.pdf_path}',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        ) != QMessageBox.Yes:
            return

        tmp_path = str(
            Path(self.pdf_path).with_name(
                Path(self.pdf_path).stem + '.__fastpdf_editor_save__.pdf'
            )
        )

        if not self._write_entries(tmp_path):
            return

        try:
            os.replace(tmp_path, self.pdf_path)
        except Exception as e:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
            QMessageBox.critical(
                self,
                '上書き保存',
                f'元PDFを置き換えられませんでした.\n{e}'
            )
            return

        # 保存結果を編集UI自身で読み直し、以後の編集基準にする。
        # 現在のページ順に合わせたお気に入り情報も保存してから再読込する。
        current_favorites = {
            i for i, e in enumerate(self.entries) if e.favorite
        }
        self.favorites.set_pages(self.pdf_path, current_favorites)
        self.favorites.save()

        self.entries = []
        self._annotation_undo.clear()
        self._annotation_redo.clear()
        self._thumbnail_cache.clear()
        self._single_cache.clear()
        self._load_original_entries()

        if self.entries:
            self.current_index = min(
                self.current_index, len(self.entries) - 1
            )
        else:
            self.current_index = 0

        self.dirty = False
        self.update_title()
        self._sync_source_selection()
        self.rebuild_list(self.current_index)
        self.show_single_page()
        self.savedPdf.emit(self.pdf_path)

    def save_as(self):
        self.sync_entries_from_items()
        if not self.entries:
            QMessageBox.warning(self, '保存', '保存するページがありません。')
            return

        default_name = Path(self.pdf_path).with_name(
            Path(self.pdf_path).stem + '_edited.pdf'
        )
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            '名前を付けて保存',
            str(default_name),
            'PDF (*.pdf)'
        )
        if not out_path:
            return

        if not out_path.lower().endswith('.pdf'):
            out_path += '.pdf'
        out_path = normalize_path(out_path)

        if out_path == self.pdf_path:
            QMessageBox.warning(
                self,
                '名前を付けて保存',
                '元PDFと同じファイルです。💾上書き保存を使用してください。'
            )
            return

        if not self._write_entries(out_path):
            return

        # 「名前を付けて保存」は、編集リスト上の元PDFを新しいPDFへ置換する。
        # 元ファイル自体は削除しないが、ビューアへ戻った時に2件並ばないようにする。
        old_path = normalize_path(self.pdf_path)
        current_index = self.current_index
        current_favorites = {
            i for i, e in enumerate(self.entries) if e.favorite
        }

        if old_path in self.pdf_paths:
            replaced = []
            for p in self.pdf_paths:
                candidate = out_path if normalize_path(p) == old_path else normalize_path(p)
                if candidate not in replaced:
                    replaced.append(candidate)
            self.pdf_paths = replaced
        elif not is_temp_pdf_path(out_path):
            if out_path not in self.pdf_paths:
                self.pdf_paths.append(out_path)

        self.favorites.set_pages(out_path, current_favorites)
        self.favorites.save()

        self.pdf_path = out_path
        if not is_temp_pdf_path(out_path):
            self.last_normal_pdf_path = out_path

        self.entries = []
        self._annotation_undo.clear()
        self._annotation_redo.clear()
        self._thumbnail_cache.clear()
        self._single_cache.clear()
        self._single_detail_dpi.clear()
        self._load_original_entries()
        self.current_index = min(current_index, max(0, len(self.entries) - 1))
        self.dirty = False

        self._populate_pdf_source_list()
        self.update_title()
        self.rebuild_list(self.current_index)
        self.show_single_page()

        self.pdfListChanged.emit(list(self.pdf_paths))
        self.savedPdf.emit(out_path)
        QMessageBox.information(
            self, '保存', f'保存しました。\n{out_path}'
        )

    def open_temp_folder_from_editor(self):
        try:
            open_temp_folder()
        except Exception as e:
            QMessageBox.warning(
                self,
                'tempフォルダ',
                f'tempフォルダを開けませんでした。\n{e}'
            )

    def save_temporary(self):
        self.sync_entries_from_items()
        if not self.entries:
            QMessageBox.warning(
                self,
                '一時保存',
                '保存するページがありません。'
            )
            return

        current_favorites = {
            i for i, entry in enumerate(self.entries)
            if entry.favorite
        }

        # 一時保存ファイルを編集中なら、同じファイルへ上書きする。
        if is_temp_pdf_path(self.pdf_path):
            temp_path = normalize_path(self.pdf_path)
            target = Path(temp_path)
            work_path = normalize_path(
                target.with_name(
                    target.stem + '.__fastpdf_temp_save__.pdf'
                )
            )

            if not self._write_entries(work_path):
                return

            try:
                os.replace(work_path, temp_path)
            except Exception as e:
                try:
                    Path(work_path).unlink(missing_ok=True)
                except Exception:
                    pass
                QMessageBox.critical(
                    self,
                    '一時保存',
                    f'一時PDFを上書きできませんでした。\n{e}'
                )
                return

            self.favorites.set_pages(temp_path, current_favorites)
            self.favorites.save()

            self.entries = []
            self._annotation_undo.clear()
            self._annotation_redo.clear()
            self._thumbnail_cache.clear()
            self._thumbnail_queue.clear()
            self._thumbnail_busy = False
            self._single_cache.clear()

            self._load_original_entries()
            self.current_index = min(
                self.current_index,
                max(0, len(self.entries) - 1)
            )
            self.dirty = False

            self._populate_pdf_source_list()
            self.update_title()
            self.rebuild_list(self.current_index)
            self.show_single_page()

            QMessageBox.information(
                self,
                '一時保存',
                '現在の一時PDFへ上書き保存しました。\n\n'
                f'{temp_path}'
            )
            return

        # 通常PDFを編集中なら、新しい一時PDFを作る。
        source_for_name = (
            self.last_normal_pdf_path
            or self.pdf_path
        )
        temp_path = normalize_path(
            make_temp_pdf_path(source_for_name)
        )

        if not self._write_entries(temp_path):
            return

        if temp_path not in self.temp_paths:
            self.temp_paths.append(temp_path)

        self.favorites.set_pages(temp_path, current_favorites)
        self.favorites.save()

        self.pdf_path = temp_path
        self.entries = []
        self._annotation_undo.clear()
        self._annotation_redo.clear()
        self._thumbnail_cache.clear()
        self._thumbnail_queue.clear()
        self._thumbnail_busy = False
        self._single_cache.clear()

        self._load_original_entries()
        self.current_index = min(
            self.current_index,
            max(0, len(self.entries) - 1)
        )
        self.dirty = False

        self._populate_pdf_source_list()
        self.update_title()
        self.rebuild_list(self.current_index)
        self.show_single_page()

        QMessageBox.information(
            self,
            '一時保存',
            '一時PDFを作成しました。\n'
            '元PDFはそのまま残り、一時PDFは下段リストに追加されます。\n\n'
            f'{temp_path}'
        )


    def viewer_return_path(self):
        if (
            self.last_normal_pdf_path
            and Path(self.last_normal_pdf_path).is_file()
        ):
            return normalize_path(self.last_normal_pdf_path)
        if self.pdf_paths:
            return normalize_path(self.pdf_paths[0])
        return None

    def closeEvent(self, event):
        if not self.dirty:
            event.accept()
            return

        box = QMessageBox(self)
        box.setWindowTitle('編集を終了')
        box.setText('未保存のPDF編集があります。')
        box.setInformativeText('保存せずにビューアへ戻りますか？')

        discard_btn = box.addButton(
            '保存せず戻る', QMessageBox.DestructiveRole
        )
        box.addButton(
            '編集を続ける', QMessageBox.RejectRole
        )
        box.exec()

        if box.clickedButton() is discard_btn:
            event.accept()
        else:
            event.ignore()

