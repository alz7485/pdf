from PySide6.QtWidgets import QToolButton, QMessageBox

from pdf_core import reveal_file_in_explorer

BUTTON_WIDTH = 38
BUTTON_HEIGHT = 34
ANNOTATION_BUTTON_WIDTH = 46
BUTTON_BORDER_COLOR = '#888'
BUTTON_RADIUS = 6


def button_stylesheet(widget_name='QToolButton', font_size=None):
    font_line = f'font-size: {int(font_size)}px;' if font_size else ''
    return f'''\n        {widget_name} {{\n            background: #ffffff;\n            color: #202020;\n            border: 1px solid {BUTTON_BORDER_COLOR};\n            border-radius: {BUTTON_RADIUS}px;\n            padding: 2px;\n            {font_line}\n        }}\n        {widget_name}:hover {{ background: #f3f3f3; }}\n        {widget_name}:pressed {{ background: #e5e5e5; }}\n        {widget_name}:checked {{\n            background: #dcecff;\n            color: #202020;\n            border: 1px solid #5b8fd9;\n        }}\n    '''


TOOL_BUTTON_STYLE = button_stylesheet('QToolButton', 17)
PUSH_BUTTON_STYLE = button_stylesheet('QPushButton')

APP_STYLESHEET = '''\nQPushButton, QToolButton { background: #ffffff; color: #202020; }\nQPushButton:hover, QToolButton:hover { background: #f3f3f3; }\nQPushButton:pressed, QToolButton:pressed { background: #e5e5e5; }\nQPushButton:checked, QToolButton:checked { background: #dcecff; color: #202020; }\n'''


def style_tool_button(button, width=BUTTON_WIDTH, height=BUTTON_HEIGHT):
    button.setFixedSize(width, height)
    button.setStyleSheet(TOOL_BUTTON_STYLE)
    return button


def style_push_button(button, width=None, height=BUTTON_HEIGHT):
    if width is None:
        button.setFixedHeight(height)
    else:
        button.setFixedSize(width, height)
    button.setStyleSheet(PUSH_BUTTON_STYLE)
    return button


def make_tool_button(text, tooltip, slot=None, checkable=False,
                     width=BUTTON_WIDTH, height=BUTTON_HEIGHT):
    button = QToolButton()
    button.setText(text)
    button.setToolTip(tooltip)
    button.setCheckable(checkable)
    style_tool_button(button, width, height)
    if slot is not None:
        button.clicked.connect(slot)
    return button


def framed_label_style(font_size=13, font_weight=600):
    return (
        'QLabel {'
        ' background: #ffffff; color: #202020;'
        f' border: 1px solid {BUTTON_BORDER_COLOR};'
        f' border-radius: {BUTTON_RADIUS}px;'
        ' padding: 3px 6px;'
        f' font-size: {font_size}px; font-weight: {font_weight};'
        '}'
    )



def reveal_pdf_location(parent, path):
    """PDFの格納場所をExplorerで表示し、欠損時だけ案内する。"""
    result = reveal_file_in_explorer(path)
    if result == 'selected':
        return True
    if result == 'file_missing':
        QMessageBox.warning(
            parent,
            'ファイルが見つかりません',
            'ファイルが見つかりません。\n'
            '削除または移動された可能性があります。\n\n'
            f'{path}'
        )
        return False
    if result == 'folder_missing':
        QMessageBox.warning(
            parent,
            '格納フォルダが見つかりません',
            '格納フォルダが見つかりません。\n'
            'フォルダが削除または移動された可能性があります。\n\n'
            f'{path}'
        )
        return False
    QMessageBox.warning(
        parent,
        'Explorer',
        'Explorerで場所を開けませんでした。\n\n'
        f'{path}'
    )
    return False
