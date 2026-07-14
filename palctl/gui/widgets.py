"""
Shared GUI widgets.

The important one: spin boxes / time edits that do NOT change value when you
scroll the mouse wheel over them. Qt's default is that a wheel over a spin box
adjusts it (and the default WheelFocus policy even gives it focus first), so
scrolling down a tall settings form silently rewrites every number the cursor
passes — exactly the "numbers were decreasing as I scrolled" bug.

These variants only respond to the wheel once you've clicked into them (they
have focus). Otherwise the wheel is ignored and bubbles up to the surrounding
scroll area, so the list scrolls like you'd expect.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox, QTimeEdit


class NoScrollSpinBox(QSpinBox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # StrongFocus (not WheelFocus): the wheel alone can't focus it.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()  # let the parent scroll area handle it


class NoScrollDoubleSpinBox(QDoubleSpinBox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoScrollTimeEdit(QTimeEdit):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()
