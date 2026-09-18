from PySide6.QtWidgets import QPushButton, QHBoxLayout, QLabel
from PySide6.QtCore import Qt


def make_shortcut_button(label: str, hint: str) -> QPushButton:
    """A QPushButton showing `label` plus a light-grey shortcut-key hint
    (e.g. "New Shot" + a dim "N") alongside it. A plain QPushButton can't
    render part of its text in a different color, and the shortcut
    letter doesn't always appear in the label itself (e.g. "Q" for "Edit
    Start/End") so Qt's native mnemonic-underline can't be used either -
    this instead lays two QLabels (main text, dim hint) inside the
    button via its own layout, a standard Qt technique for custom button
    content. update_hint() lets the hint be changed later (e.g. after
    the user customizes it in Settings) without rebuilding the button.
    """
    btn = QPushButton()
    layout = QHBoxLayout(btn)
    layout.setContentsMargins(8, 2, 8, 2)
    layout.setSpacing(4)
    layout.addStretch()

    main_label = QLabel(label)
    main_label.setAttribute(Qt.WA_TransparentForMouseEvents)
    layout.addWidget(main_label)

    hint_label = QLabel(f"({hint})" if hint else "")
    hint_label.setStyleSheet("color: #999999;")
    hint_label.setAttribute(Qt.WA_TransparentForMouseEvents)
    layout.addWidget(hint_label)

    layout.addStretch()

    btn._shortcut_hint_label = hint_label

    def update_hint(new_hint: str):
        hint_label.setText(f"({new_hint})" if new_hint else "")

    btn.update_shortcut_hint = update_hint
    return btn
