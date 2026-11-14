"""Native Qt plots; no plotting runtime or GUI-thread training work is required."""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


class MetricChart(QWidget):
    def __init__(self, title, train_key, validation_key, parent=None):
        super().__init__(parent)
        self.title, self.train_key, self.validation_key = title, train_key, validation_key
        self.history = []
        self.setMinimumSize(250, 210)
        self.setToolTip("蓝线为训练集，绿线为独立验证集。横轴为轮次；无验证集时只显示训练曲线。")

    def set_history(self, history):
        self.history = list(history)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#111827"))
        painter.setPen(QColor("#334155"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(14, 25, self.title)
        for x, color, label in ((14, "#60a5fa", "训练"), (85, "#34d399", "验证")):
            painter.setPen(QPen(QColor(color), 2))
            painter.drawLine(x, 43, x + 17, 43)
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(x + 23, 47, label)
        plot = QRectF(49, 65, self.width() - 65, self.height() - 99)
        if not self.history:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(plot, Qt.AlignCenter, "训练后显示实际指标")
            return
        accuracy = "accuracy" in self.train_key
        values = [row[key] for row in self.history for key in (self.train_key, self.validation_key) if row.get(key) is not None]
        maximum = 1.0 if accuracy else max(max(values, default=0.) * 1.12, 0.01)
        epochs = max(self.history[-1]["epoch"], 2)
        for tick in range(5):
            value = maximum * tick / 4
            y = plot.bottom() - plot.height() * tick / 4
            painter.setPen(QColor("#253247"))
            painter.drawLine(plot.left(), y, plot.right(), y)
            painter.setPen(QColor("#94a3b8"))
            label = f"{value:.0%}" if accuracy else f"{value:.2f}"
            painter.drawText(QRectF(0, y - 8, 43, 17), Qt.AlignRight | Qt.AlignVCenter, label)
        for key, color in ((self.train_key, "#60a5fa"), (self.validation_key, "#34d399")):
            path, started = QPainterPath(), False
            points = []
            for row in self.history:
                if row.get(key) is None:
                    continue
                x = plot.left() + (row["epoch"] - 1) / (epochs - 1) * plot.width()
                y = plot.bottom() - row[key] / maximum * plot.height()
                (path.lineTo if started else path.moveTo)(x, y)
                started = True
                points.append((x, y))
            painter.setPen(QPen(QColor(color), 2))
            painter.drawPath(path)
            if points:
                painter.setBrush(QColor(color))
                painter.drawEllipse(points[-1][0] - 2.5, points[-1][1] - 2.5, 5, 5)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(plot.left(), plot.bottom() + 7, 30, 20), Qt.AlignLeft, "1")
        painter.drawText(QRectF(plot.right() - 35, plot.bottom() + 7, 35, 20), Qt.AlignRight, str(self.history[-1]["epoch"]))
        painter.drawText(QRectF(plot.left(), plot.bottom() + 7, plot.width(), 20), Qt.AlignCenter, "轮次")


class ConfusionChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.labels, self.matrix, self.source = [], [], ""
        self.setMinimumHeight(270)
        self.setToolTip("纵轴为人工标签，横轴为模型预测。对角线表示预测正确的句数。")

    def set_matrix(self, labels, matrix, source):
        self.labels, self.matrix, self.source = list(labels), matrix, source
        self.setMinimumHeight(max(270, len(labels) * 26 + 90))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#111827"))
        painter.setPen(QColor("#334155"))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        painter.setPen(QColor("#e2e8f0"))
        painter.drawText(14, 25, "混淆矩阵" + (f" · {self.source}" if self.source else ""))
        if not self.matrix:
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(self.rect(), Qt.AlignCenter, "训练后显示人工标签与模型预测的对照")
            return
        count = len(self.labels)
        size = min((self.width() - 125) / count, (self.height() - 88) / count)
        left, top = 95, 46
        maximum = max(1, max(max(row) for row in self.matrix))
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        for row, label in enumerate(self.labels):
            painter.setPen(QColor("#cbd5e1"))
            shortened = painter.fontMetrics().elidedText(label, Qt.ElideRight, 80)
            painter.drawText(QRectF(5, top + row * size, 80, size), Qt.AlignRight | Qt.AlignVCenter, shortened)
            painter.save()
            painter.translate(left + (row + .5) * size, top + count * size + 7)
            painter.rotate(30)
            painter.drawText(0, 10, painter.fontMetrics().elidedText(label, Qt.ElideRight, 55))
            painter.restore()
            for col in range(count):
                value = self.matrix[row][col]
                color = QColor("#2563eb") if row == col else QColor("#b45309")
                color.setAlphaF(.12 + .88 * value / maximum)
                painter.fillRect(QRectF(left + col * size, top + row * size, size - 2, size - 2), color)
                painter.setPen(QColor("#f1f5f9"))
                painter.drawText(QRectF(left + col * size, top + row * size, size - 2, size - 2), Qt.AlignCenter, str(value))
