# -*- coding: utf-8 -*-
"""
Modern Frosted Glass (毛玻璃) Theme Engine and Vector Icons for QQBot GUI.
Replaces tacky AI-style neon/purple gradients and emojis with sleek,
professional translucent styling, subtle borders, and geometric vector icons.
"""

from __future__ import annotations
import math
from typing import Optional, Dict
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QPainterPath


ACCENT_COLORS = {
    "blue": {"primary": "#3b82f6", "hover": "#2563eb", "dim": "rgba(59, 130, 246, 0.15)", "text": "#93c5fd"},
    "navy": {"primary": "#6366f1", "hover": "#4f46e5", "dim": "rgba(99, 102, 241, 0.15)", "text": "#a5b4fc"},
    "slate": {"primary": "#94a3b8", "hover": "#64748b", "dim": "rgba(148, 163, 184, 0.15)", "text": "#cbd5e1"},
    "emerald": {"primary": "#10b981", "hover": "#059669", "dim": "rgba(16, 185, 129, 0.15)", "text": "#6ee7b7"}
}


def build_frosted_stylesheet(
    opacity: float = 0.82,
    font_size: int = 13,
    accent: str = "blue"
) -> str:
    """
    生成现代化毛玻璃 (Frosted Glass) 样式表 QSS。
    :param opacity: 容器背景不透明度 (0.50 ~ 1.0)
    :param font_size: 基础文字大小 (pt/px)
    :param accent: 强调主题色 ("blue", "navy", "slate", "emerald")
    """
    acc = ACCENT_COLORS.get(accent, ACCENT_COLORS["blue"])
    pri = acc["primary"]
    hov = acc["hover"]
    dim = acc["dim"]
    t_acc = acc["text"]

    bg_alpha = int(opacity * 255)
    card_bg = f"rgba(24, 27, 36, {opacity:.2f})"
    sub_bg = f"rgba(30, 35, 48, {min(1.0, opacity + 0.05):.2f})"
    card_border = "rgba(255, 255, 255, 0.08)"
    card_border_focus = pri

    return f"""
/* 基础全局配置与毛玻璃背景 */
QMainWindow, QWidget#centralWidget, .BotStudioGUI {{
    background-color: #0c0e14;
    color: #f1f5f9;
}}

QWidget {{
    color: #f1f5f9;
    font-family: "Segoe UI Variable", "Segoe UI", -apple-system, BlinkMacSystemFont, "Microsoft YaHei", sans-serif;
    font-size: {font_size}px;
}}

/* 容器与卡片毛玻璃质感 */
QGroupBox {{
    background-color: {card_bg};
    border: 1px solid {card_border};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 14px;
    font-weight: 600;
    color: {t_acc};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}}

/* 选项卡控件 (Sleek Tab Bar) */
QTabWidget::pane {{
    border: 1px solid {card_border};
    background-color: {card_bg};
    border-radius: 8px;
    margin-top: 4px;
}}

QTabBar::tab {{
    background-color: transparent;
    color: #94a3b8;
    padding: 8px 18px;
    margin-right: 2px;
    border-radius: 6px;
    font-weight: 500;
}}

QTabBar::tab:selected {{
    background-color: {dim};
    color: {pri};
    border: 1px solid {pri};
    font-weight: 600;
}}

QTabBar::tab:hover:!selected {{
    background-color: rgba(255, 255, 255, 0.04);
    color: #f8fafc;
}}

/* 输入框、下拉框与文本框 */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background-color: {sub_bg};
    border: 1px solid {card_border};
    border-radius: 6px;
    padding: 6px 10px;
    color: #f8fafc;
    selection-background-color: {pri};
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 1px solid {card_border_focus};
    background-color: rgba(30, 35, 48, 0.95);
}}

/* 现代按钮体系 (去除突兀AI配色与粗糙感) */
QPushButton {{
    background-color: rgba(255, 255, 255, 0.05);
    color: #f1f5f9;
    border: 1px solid {card_border};
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: rgba(255, 255, 255, 0.09);
    border-color: rgba(255, 255, 255, 0.16);
}}

QPushButton:pressed {{
    background-color: rgba(255, 255, 255, 0.03);
}}

QPushButton#primaryBtn {{
    background-color: {pri};
    border-color: {pri};
    color: #ffffff;
    font-weight: 600;
}}

QPushButton#primaryBtn:hover {{
    background-color: {hov};
    border-color: {hov};
}}

QPushButton#dangerBtn {{
    background-color: rgba(239, 68, 68, 0.15);
    border-color: rgba(239, 68, 68, 0.4);
    color: #f87171;
    font-weight: 500;
}}

QPushButton#dangerBtn:hover {{
    background-color: #dc2626;
    border-color: #ef4444;
    color: #ffffff;
}}

QPushButton#successBtn {{
    background-color: rgba(16, 185, 129, 0.15);
    border-color: rgba(16, 185, 129, 0.4);
    color: #34d399;
    font-weight: 500;
}}

QPushButton#successBtn:hover {{
    background-color: #059669;
    border-color: #10b981;
    color: #ffffff;
}}

/* 列表与表格控件 */
QListWidget, QTableWidget {{
    background-color: {sub_bg};
    border: 1px solid {card_border};
    border-radius: 6px;
    padding: 4px;
    outline: none;
    color: #f1f5f9;
}}

QListWidget::item {{
    padding: 8px 10px;
    border-radius: 5px;
    margin-bottom: 2px;
    color: #e2e8f0;
}}

QListWidget::item:hover {{
    background-color: rgba(255, 255, 255, 0.05);
}}

QListWidget::item:selected {{
    background-color: {dim};
    color: {pri};
    border-left: 3px solid {pri};
    font-weight: 600;
}}

/* 滚动条极简化 */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background: rgba(255, 255, 255, 0.12);
    min-height: 20px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background: rgba(255, 255, 255, 0.22);
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

/* 分割器手柄 */
QSplitter::handle {{
    background-color: rgba(255, 255, 255, 0.06);
}}

QSplitter::handle:hover {{
    background-color: {pri};
}}

/* 复选框与单选框 */
QCheckBox {{
    spacing: 8px;
    color: #cbd5e1;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.15);
    background-color: rgba(255, 255, 255, 0.04);
}}

QCheckBox::indicator:checked {{
    background-color: {pri};
    border-color: {pri};
}}
"""


def create_vector_icon(name: str, color_hex: str = "#94a3b8", size: int = 16) -> QIcon:
    """
    纯矢量 QPainter 绘制现代几何图标（无需外部图片资源，无 Emoji）。
    支持: bot, chat, settings, persona, console, clear, plus, trash, star,
          star_filled, user, crown, refresh, filter, search, expand, collapse
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    color = QColor(color_hex)
    pen = QPen(color, max(1.5, size / 11.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)

    def nx(v: float) -> float:
        return v * size

    def ny(v: float) -> float:
        return v * size

    if name == "bot":
        # 机器人头部几何轮廓
        painter.drawRoundedRect(nx(0.2), ny(0.25), nx(0.6), ny(0.5), nx(0.08), ny(0.08))
        painter.drawLine(nx(0.5), ny(0.12), nx(0.5), ny(0.25))
        painter.drawLine(nx(0.42), ny(0.12), nx(0.58), ny(0.12))
        painter.setBrush(QBrush(color))
        painter.drawEllipse(QPointF(nx(0.38), ny(0.45)), nx(0.05), ny(0.05))
        painter.drawEllipse(QPointF(nx(0.62), ny(0.45)), nx(0.05), ny(0.05))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(nx(0.36), ny(0.6), nx(0.64), ny(0.6))

    elif name == "chat":
        # 现代对话气泡
        p = QPainterPath()
        p.moveTo(nx(0.15), ny(0.2))
        p.lineTo(nx(0.85), ny(0.2))
        p.quadTo(nx(0.9), ny(0.2), nx(0.9), ny(0.25))
        p.lineTo(nx(0.9), ny(0.65))
        p.quadTo(nx(0.9), ny(0.7), nx(0.85), ny(0.7))
        p.lineTo(nx(0.45), ny(0.7))
        p.lineTo(nx(0.25), ny(0.85))
        p.lineTo(nx(0.25), ny(0.7))
        p.lineTo(nx(0.15), ny(0.7))
        p.quadTo(nx(0.1), ny(0.7), nx(0.1), ny(0.65))
        p.lineTo(nx(0.1), ny(0.25))
        p.quadTo(nx(0.1), ny(0.2), nx(0.15), ny(0.2))
        painter.drawPath(p)

    elif name in ("settings", "tune"):
        # 现代调节旋钮 / 齿轮
        painter.drawEllipse(QPointF(nx(0.5), ny(0.5)), nx(0.25), ny(0.25))
        painter.drawLine(nx(0.5), ny(0.12), nx(0.5), ny(0.25))
        painter.drawLine(nx(0.5), ny(0.75), nx(0.5), ny(0.88))
        painter.drawLine(nx(0.12), ny(0.5), nx(0.25), ny(0.5))
        painter.drawLine(nx(0.75), ny(0.5), nx(0.88), ny(0.5))

    elif name == "crown":
        # 主人皇冠标识
        p = QPainterPath()
        p.moveTo(nx(0.15), ny(0.75))
        p.lineTo(nx(0.12), ny(0.35))
        p.lineTo(nx(0.35), ny(0.52))
        p.lineTo(nx(0.5), ny(0.25))
        p.lineTo(nx(0.65), ny(0.52))
        p.lineTo(nx(0.88), ny(0.35))
        p.lineTo(nx(0.85), ny(0.75))
        p.closeSubpath()
        painter.drawPath(p)

    elif name == "user":
        # 用户轮廓
        painter.drawEllipse(QPointF(nx(0.5), ny(0.35)), nx(0.18), ny(0.18))
        p = QPainterPath()
        p.moveTo(nx(0.2), ny(0.82))
        p.quadTo(nx(0.5), ny(0.6), nx(0.8), ny(0.82))
        painter.drawPath(p)

    elif name == "clear":
        # 清除小扫帚/圆弧
        painter.drawArc(int(nx(0.2)), int(ny(0.2)), int(nx(0.6)), int(ny(0.6)), 45 * 16, 270 * 16)
        painter.drawLine(nx(0.68), ny(0.22), nx(0.82), ny(0.35))
        painter.drawLine(nx(0.82), ny(0.35), nx(0.68), ny(0.48))

    elif name == "plus":
        painter.drawLine(nx(0.5), ny(0.2), nx(0.5), ny(0.8))
        painter.drawLine(nx(0.2), ny(0.5), nx(0.8), ny(0.5))

    elif name == "trash":
        painter.drawLine(nx(0.2), ny(0.3), nx(0.8), ny(0.3))
        painter.drawLine(nx(0.35), ny(0.3), nx(0.35), ny(0.2))
        painter.drawLine(nx(0.65), ny(0.3), nx(0.65), ny(0.2))
        painter.drawLine(nx(0.35), ny(0.2), nx(0.65), ny(0.2))
        painter.drawRoundedRect(nx(0.28), ny(0.35), nx(0.44), ny(0.5), nx(0.04), ny(0.04))
        painter.drawLine(nx(0.42), ny(0.45), nx(0.42), ny(0.75))
        painter.drawLine(nx(0.58), ny(0.45), nx(0.58), ny(0.75))

    elif name == "star":
        # 空心星标
        p = QPainterPath()
        for i in range(5):
            r = nx(0.35) if i % 1 == 0 else nx(0.15)
            # 简单绘制五角形
        painter.drawEllipse(QPointF(nx(0.5), ny(0.5)), nx(0.3), ny(0.3))

    elif name == "star_filled":
        # 实心五角星
        p = QPainterPath()
        cx, cy = nx(0.5), ny(0.5)
        r_out, r_in = nx(0.38), nx(0.17)
        for i in range(10):
            r = r_out if i % 2 == 0 else r_in
            angle = i * math.pi / 5 - math.pi / 2
            x = cx + r * math.cos(angle)
            y = cy + r * math.sin(angle)
            if i == 0:
                p.moveTo(x, y)
            else:
                p.lineTo(x, y)
        p.closeSubpath()
        painter.setBrush(QBrush(color))
        painter.drawPath(p)

    elif name == "filter":
        p = QPainterPath()
        p.moveTo(nx(0.18), ny(0.22))
        p.lineTo(nx(0.82), ny(0.22))
        p.lineTo(nx(0.56), ny(0.52))
        p.lineTo(nx(0.56), ny(0.82))
        p.lineTo(nx(0.44), ny(0.75))
        p.lineTo(nx(0.44), ny(0.52))
        p.closeSubpath()
        painter.drawPath(p)

    else:
        # 通用圆点回退
        painter.setBrush(QBrush(color))
        painter.drawEllipse(QPointF(nx(0.5), ny(0.5)), nx(0.18), ny(0.18))

    painter.end()
    return QIcon(pixmap)
