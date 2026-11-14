# -*- coding: utf-8 -*-
"""
Media Configuration & Task Monitor Tab Component for QQBot Studio.
Provides media feature controls (images, video, files), limit configuration,
ffmpeg status checking, real-time media task logs, and visual asset preview.
"""

from __future__ import annotations
import asyncio
import logging
from pathlib import Path
import shutil
import threading
from typing import Optional, Dict

from PySide6.QtCore import Qt, Signal, QUrl, QTimer
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QSpinBox, QScrollArea, QPlainTextEdit,
    QGroupBox, QComboBox
)

from config import AppConfig, GeminiWebConfig, MediaConfig

logger = logging.getLogger("MediaTab")


class MediaTabWidget(QWidget):
    """媒体功能与任务选项卡组件"""
    save_requested = Signal()
    gemini_models_received = Signal(list, str)
    gemini_account_received = Signal(dict, str)
    gemini_browser_connected = Signal(str)

    def __init__(self, app_config: AppConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.app_config = app_config
        self._gemini_link = None
        self._gemini_probe_running = False
        self._gemini_auth_summary = ""
        self._gemini_login_stamp = 0
        self._gemini_link_timer = QTimer(self)
        self._gemini_link_timer.setInterval(1500)
        self._gemini_link_timer.timeout.connect(self._poll_gemini_link)
        self._setup_ui()
        self.load_media_config()
        self._resume_gemini_link()
        self.gemini_auto_browser.toggled.connect(self._toggle_gemini_auto_login)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        fields = QVBoxLayout(panel)
        fields.setSpacing(10)

        # 1. 媒体功能开关卡片
        switch_group = QGroupBox("媒体功能开关与受控通道")
        switch_layout = QVBoxLayout(switch_group)

        self.media_checks: Dict[str, QCheckBox] = {}
        for key, label in (
            ("enabled", "启用媒体功能总开关"),
            ("image_enabled", "启用图片发送与检索"),
            ("image_generation_enabled", "启用图片生成子代理"),
            ("video_enabled", "启用短视频发送与转换"),
            ("video_generation_enabled", "启用视频生成"),
            ("file_enabled", "启用文件与文档导出发送"),
            ("require_explicit_group_request", "群聊媒体请求需要明确 @ 或主人触发"),
            ("official_file_enabled", "官方通道启用通用文件（确认账号支持后开启）"),
            ("napcat_stream_enabled", "跨设备 NapCat 使用分块上传流 (Stream API)")
        ):
            box = QCheckBox(label)
            self.media_checks[key] = box
            switch_layout.addWidget(box)

        fields.addWidget(switch_group)

        # 2. 上限与并发阈值卡片
        num_group = QGroupBox("公共下载器与缓存配置")
        num_layout = QVBoxLayout(num_group)
        note = QLabel("默认由反重力自主检索、浏览并下载到任务文件夹，QQBot 直接发送成果。\n"
                      "临时任务并发运行，数量不限；交付超过 20 个文件自动打包 ZIP，单文件超过 1 GB 跳过。\n"
                      "下面的大小、时长和下载并发/总时限用于公共下载器；反重力工作时限不受这些设置限制。")
        note.setWordWrap(True)
        num_layout.addWidget(note)

        self.media_numbers: Dict[str, QSpinBox] = {}
        for key, label, lower, upper in (
            ("max_image_mb", "单张图片大小上限 (MB)", 1, 50),
            ("max_video_mb", "视频文件大小上限 (MB)", 1, 500),
            ("max_file_mb", "公共下载器文件大小上限 (MB)", 1, 1024),
            ("default_video_max_seconds", "短视频最大时长 (秒)", 1, 3600),
            ("max_concurrent_downloads", "最大并发下载数", 1, 8),
            ("cache_max_mb", "媒体缓存容量目标 (MB)", 100, 102400),
            ("task_timeout_seconds", "公共下载器总时限（秒，0 为不限制）", 0, 86400)
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            spin = QSpinBox()
            spin.setRange(lower, upper)
            row.addWidget(spin)
            self.media_numbers[key] = spin
            num_layout.addLayout(row)

        fields.addWidget(num_group)

        # 3. 路径与扩展源卡片
        path_group = QGroupBox("路径与外部服务接口设置")
        path_layout = QVBoxLayout(path_group)

        path_layout.addWidget(QLabel("网络检索方式:"))
        self.media_provider = QComboBox()
        self.media_provider.addItem("反重力自主检索、浏览与下载 → QQBot 交付（默认）", "native")
        self.media_provider.addItem("公共搜索（Bing + Wikimedia）", "auto")
        self.media_provider.addItem("Wikimedia Commons", "wikimedia")
        path_layout.addWidget(self.media_provider)

        path_layout.addWidget(QLabel("图片生成方式:"))
        self.image_generation_provider = QComboBox()
        self.image_generation_provider.addItem("反重力原生图片生成工具", "native")
        self.image_generation_provider.addItem("图片生成 API（兼容 /images/generations）", "api")
        self.image_generation_provider.addItem("Gemini 网页端（支持原图编辑 / 图生图）", "gemini_web")
        path_layout.addWidget(self.image_generation_provider)

        path_layout.addWidget(QLabel("视频生成方式:"))
        self.video_generation_provider = QComboBox()
        self.video_generation_provider.addItem("Gemini 网页端（默认，支持参考图）", "gemini_web")
        self.video_generation_provider.addItem("反重力原生视频生成工具", "native")
        path_layout.addWidget(self.video_generation_provider)

        self.media_strings: Dict[str, QLineEdit] = {}
        for key, label in (
            ("search_api_url", "公共搜索的自定义接口 URL（可选）:"),
            ("image_generation_api_url", "图片生成 API 完整地址（API 模式必填）:"),
            ("image_generation_api_key_env", "图片生成密钥的环境变量名:"),
            ("image_generation_model", "图片生成模型（API 模式必填）:"),
            ("publish_upload_url", "官方媒体 HTTPS 发布接口（可选）:"),
            ("ffmpeg_path", "ffmpeg 可执行文件路径:"),
            ("ffprobe_path", "ffprobe 可执行文件路径:"),
            ("local_library_roots", "已登记本地资料库目录（分号分隔）:")
        ):
            path_layout.addWidget(QLabel(label))
            edit = QLineEdit()
            self.media_strings[key] = edit
            path_layout.addWidget(edit)

        fields.addWidget(path_group)

        gemini_group = QGroupBox("Gemini 网页端：模型与登录态")
        gemini_layout = QVBoxLayout(gemini_group)
        note = QLabel("生图、图生图和视频生成沿用上方选择，视频生成默认为网页端；\n"
                      "可用 /生成视频 --model pro 指定本次模型，附图将作为生成参考。\n"
                      "搜索、任务、翻译和附件分析可在正文前加\n"
                      "--provider gemini_web --model pro 单次使用。附图会直接上传。\n"
                      "自动识别浏览器中的 Gemini 登录态和系统代理。Chrome / Edge 可点\n"
                      "“连接当前浏览器”，首次连接本地扩展后自动同步，无需复制 Cookie。")
        note.setWordWrap(True)
        gemini_layout.addWidget(note)
        gemini_layout.addWidget(QLabel("默认网页端模型（可输入账号支持的完整模型名称）:"))
        self.gemini_web_model = QComboBox()
        self.gemini_web_model.setEditable(True)
        self.gemini_web_model.addItems(["pro", "flash", "flash-lite", "auto"])
        gemini_layout.addWidget(self.gemini_web_model)
        self.gemini_auto_browser = QCheckBox("自动使用浏览器登录态（无需手填 Cookie）")
        gemini_layout.addWidget(self.gemini_auto_browser)
        self.gemini_browser = QComboBox()
        for label, value in (("自动（默认浏览器）", "auto"), ("Chrome", "chrome"),
                             ("Edge", "edge"), ("Brave", "brave"), ("Firefox", "firefox")):
            self.gemini_browser.addItem(label, value)
        gemini_layout.addWidget(QLabel("直接读取的浏览器:"))
        gemini_layout.addWidget(self.gemini_browser)
        self.gemini_browser_profile = QComboBox()
        self.gemini_browser_profile.setEditable(True)
        gemini_layout.addWidget(QLabel("浏览器配置（留空使用最近使用的配置；连接扩展时使用该扩展所在的配置）:"))
        gemini_layout.addWidget(self.gemini_browser_profile)
        self.gemini_browser.currentIndexChanged.connect(self._refresh_gemini_profiles)
        self.gemini_auto_proxy = QCheckBox("自动使用系统代理（代理环境变量优先）")
        gemini_layout.addWidget(self.gemini_auto_proxy)
        browser_buttons = QHBoxLayout()
        self.btn_connect_gemini = QPushButton("连接当前浏览器")
        self.btn_connect_gemini.clicked.connect(self.connect_gemini_browser)
        browser_buttons.addWidget(self.btn_connect_gemini)
        self.btn_gemini_extension = QPushButton("打开连接扩展文件夹")
        self.btn_gemini_extension.clicked.connect(self.open_gemini_extension)
        browser_buttons.addWidget(self.btn_gemini_extension)
        gemini_layout.addLayout(browser_buttons)
        gemini_layout.addWidget(QLabel("手动登录与代理设置（可选，已配置的环境变量 / Cookie 文件优先）:"))
        self.gemini_web_strings: Dict[str, QLineEdit] = {}
        for key, label in (
            ("cookie_file", "Cookie JSON 文件路径:"),
            ("secure_1psid_env", "__Secure-1PSID 的环境变量名:"),
            ("secure_1psidts_env", "__Secure-1PSIDTS 的环境变量名（可选）:"),
            ("proxy_env", "代理地址的环境变量名（留空直连）:")
        ):
            gemini_layout.addWidget(QLabel(label))
            edit = QLineEdit()
            self.gemini_web_strings[key] = edit
            gemini_layout.addWidget(edit)
        timeout_row = QHBoxLayout()
        timeout_row.addWidget(QLabel("网页端请求超时（秒）:"))
        self.gemini_web_timeout = QSpinBox()
        self.gemini_web_timeout.setRange(30, 7200)
        timeout_row.addWidget(self.gemini_web_timeout)
        gemini_layout.addLayout(timeout_row)
        self.btn_probe_gemini = QPushButton("检查登录并读取可用模型")
        self.btn_probe_gemini.clicked.connect(self.probe_gemini_models)
        gemini_layout.addWidget(self.btn_probe_gemini)
        self.gemini_web_status = QLabel("尚未检测网页端登录状态。")
        self.gemini_web_status.setWordWrap(True)
        self.gemini_web_status.setTextFormat(Qt.TextFormat.PlainText)
        gemini_layout.addWidget(self.gemini_web_status)
        self.gemini_models_received.connect(self._receive_gemini_models)
        self.gemini_account_received.connect(self._receive_gemini_account)
        self.gemini_browser_connected.connect(self._gemini_browser_ready)
        fields.addWidget(gemini_group)

        # 保存按钮
        self.btn_save_media = QPushButton("保存媒体配置")
        self.btn_save_media.setObjectName("successBtn")
        self.btn_save_media.setFixedHeight(36)
        self.btn_save_media.clicked.connect(self.save_requested.emit)
        fields.addWidget(self.btn_save_media)

        # 4. 任务日志与环境状态卡片
        status_group = QGroupBox("媒体环境与任务运行状态")
        status_layout = QVBoxLayout(status_group)

        self.media_capabilities = QLabel()
        self.media_capabilities.setWordWrap(True)
        status_layout.addWidget(self.media_capabilities)

        self.btn_refresh = QPushButton("刷新媒体环境状态")
        self.btn_refresh.clicked.connect(self.refresh_media_capabilities)
        status_layout.addWidget(self.btn_refresh)

        self.media_job_log = QPlainTextEdit()
        self.media_job_log.setReadOnly(True)
        self.media_job_log.setMaximumBlockCount(500)
        self.media_job_log.setPlaceholderText("媒体任务的搜索、下载、发送与状态日志将在此显示。")
        status_layout.addWidget(self.media_job_log)

        fields.addWidget(status_group)

        # 5. 资源预览卡片
        preview_group = QGroupBox("已发送资源即时预览")
        prev_layout = QVBoxLayout(preview_group)

        self.media_preview_info = QLabel("发送成功的图片预览、视频时长和文件大小将在此显示。")
        self.media_preview_info.setWordWrap(True)
        self.media_preview_info.setTextFormat(Qt.TextFormat.PlainText)
        prev_layout.addWidget(self.media_preview_info)

        self.media_preview = QLabel()
        self.media_preview.setMaximumHeight(220)
        self.media_preview.setAlignment(Qt.AlignCenter)
        prev_layout.addWidget(self.media_preview)

        fields.addWidget(preview_group)

        scroll.setWidget(panel)
        layout.addWidget(scroll)
        self.refresh_media_capabilities()

    def load_media_config(self):
        """将当前媒体配置加载到控件中"""
        media = getattr(self.app_config, "media", None)
        if not media:
            return
        self.media_provider.setCurrentIndex(self.media_provider.findData(media.search_provider))
        self.image_generation_provider.setCurrentIndex(self.image_generation_provider.findData(media.image_generation_provider))
        self.video_generation_provider.setCurrentIndex(self.video_generation_provider.findData(media.video_generation_provider))
        self.gemini_web_model.setCurrentText(media.gemini_web.model)
        self.gemini_web_timeout.setValue(media.gemini_web.timeout_seconds)
        self.gemini_auto_browser.setChecked(media.gemini_web.auto_browser)
        self.gemini_auto_proxy.setChecked(media.gemini_web.auto_proxy)
        self.gemini_browser.setCurrentIndex(self.gemini_browser.findData(media.gemini_web.browser))
        self._refresh_gemini_profiles()
        self.gemini_browser_profile.setCurrentText(media.gemini_web.browser_profile)
        for key, edit in self.gemini_web_strings.items():
            edit.setText(getattr(media.gemini_web, key))
        for key, box in self.media_checks.items():
            if hasattr(media, key):
                box.setChecked(getattr(media, key))
        for key, spin in self.media_numbers.items():
            if hasattr(media, key):
                spin.setValue(getattr(media, key))
        for key, edit in self.media_strings.items():
            if hasattr(media, key):
                val = getattr(media, key)
                edit.setText(";".join(val) if isinstance(val, list) else str(val))
        self.refresh_media_capabilities()

    def collect_media_config_values(self) -> dict:
        """从控件中收集媒体配置字典数据"""
        media_values = self.app_config.media.model_dump()
        media_values["search_provider"] = self.media_provider.currentData() or "native"
        media_values["image_generation_provider"] = self.image_generation_provider.currentData() or "native"
        media_values["video_generation_provider"] = self.video_generation_provider.currentData() or "gemini_web"
        media_values["gemini_web"] = self._gemini_web_values()
        for key, box in self.media_checks.items():
            media_values[key] = box.isChecked()
        for key, spin in self.media_numbers.items():
            media_values[key] = spin.value()
        for key, edit in self.media_strings.items():
            if key == "local_library_roots":
                media_values[key] = [p.strip() for p in edit.text().split(";") if p.strip()]
            else:
                media_values[key] = edit.text().strip()
        return media_values

    def _gemini_web_values(self):
        return {"model": self.gemini_web_model.currentText().strip() or "auto",
                "auto_browser": self.gemini_auto_browser.isChecked(),
                "auto_proxy": self.gemini_auto_proxy.isChecked(),
                "browser": self.gemini_browser.currentData() or "auto",
                "browser_profile": self.gemini_browser_profile.currentText().strip(),
                "timeout_seconds": self.gemini_web_timeout.value(),
                **{key: edit.text().strip() for key, edit in self.gemini_web_strings.items()}}

    def _refresh_gemini_profiles(self):
        from gemini_browser_auth import browser_profiles
        selected = self.gemini_browser_profile.currentText()
        self.gemini_browser_profile.clear()
        self.gemini_browser_profile.addItem("")
        try:
            for profile in browser_profiles(self.gemini_browser.currentData() or "auto"):
                self.gemini_browser_profile.addItem(profile["path"])
        except (OSError, ValueError, KeyError):
            pass
        self.gemini_browser_profile.setCurrentText(selected)

    def _start_gemini_link(self, config):
        from gemini_browser_link import GeminiBrowserLink
        from gemini_browser_auth import auth_directory
        if self._gemini_link and auth_directory(self._gemini_link.config) != auth_directory(config):
            self._gemini_link.close()
            self._gemini_link = None
        if not self._gemini_link:
            def connected(source):
                try:
                    self.gemini_browser_connected.emit(source)
                except RuntimeError:
                    pass
            self._gemini_link = GeminiBrowserLink(config, connected)
        url = self._gemini_link.start()
        if self._gemini_link.shared:
            self._gemini_link_timer.start()
        return url

    def _poll_gemini_link(self):
        from gemini_browser_auth import auth_directory, connected_login
        if not self._gemini_link or not self.gemini_auto_browser.isChecked():
            return
        config = GeminiWebConfig.model_validate(self._gemini_web_values())
        try:
            root = auth_directory(config)
            stamp = ((root / "browser-session.json").stat().st_mtime_ns,
                     (root / "browser-link.json").stat().st_mtime_ns)
        except OSError:
            return
        if stamp != self._gemini_login_stamp:
            self._gemini_login_stamp = stamp
            if login := connected_login(config):
                self._gemini_browser_ready(login.source)

    def _toggle_gemini_auto_login(self, enabled):
        if not enabled:
            self._gemini_link_timer.stop()
            if self._gemini_link:
                self._gemini_link.close()
                self._gemini_link = None
        else:
            self._resume_gemini_link()

    def _resume_gemini_link(self):
        from gemini_browser_auth import auth_directory
        config = GeminiWebConfig.model_validate(self._gemini_web_values())
        if config.auto_browser and (auth_directory(config) / "browser-link.json").is_file():
            try:
                self._start_gemini_link(config)
                self.gemini_web_status.setText("浏览器连接已恢复，等待登录态自动同步。")
            except (OSError, ValueError):
                self.gemini_web_status.setText("浏览器连接未恢复，可点“连接当前浏览器”重新连接。")

    def connect_gemini_browser(self):
        self.gemini_auto_browser.setChecked(True)
        self.gemini_browser.setCurrentIndex(self.gemini_browser.findData("auto"))
        self.gemini_browser_profile.setCurrentText("")
        config = GeminiWebConfig.model_validate(self._gemini_web_values())
        try:
            url = self._start_gemini_link(config)
            opened = QDesktopServices.openUrl(QUrl(url))
            self.gemini_web_status.setText("已打开浏览器连接页。请在已登录 Gemini 的配置中完成首次连接，之后自动同步。" if opened
                                          else "无法打开默认浏览器，请检查系统默认浏览器设置。")
        except (OSError, ValueError):
            self.gemini_web_status.setText("无法建立本机浏览器连接，请检查 Cookie 文件目录是否可写后重试。")

    def open_gemini_extension(self):
        from gemini_browser_link import EXTENSION_DIRECTORY
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(EXTENSION_DIRECTORY)))

    def _gemini_browser_ready(self, source):
        if not self.gemini_auto_browser.isChecked():
            return
        self.gemini_web_status.setText("浏览器已连接：" + source + "；正在读取账号模型…")
        if not self._gemini_probe_running:
            self.probe_gemini_models()

    def probe_gemini_models(self):
        if self._gemini_probe_running:
            return
        config = GeminiWebConfig.model_validate(self._gemini_web_values())
        self._gemini_probe_running = True
        self.btn_probe_gemini.setEnabled(False)
        self.gemini_web_status.setText("正在自动识别登录态、代理并读取账号模型…")

        def run():
            from gemini_web_client import inspect_account, public_error
            from media_downloader import MediaError
            try:
                account, error = asyncio.run(inspect_account(config)), ""
            except Exception as exc:
                account, error = {}, str(exc) if isinstance(exc, MediaError) else public_error(exc)
            try:
                self.gemini_account_received.emit(account, error)
            except RuntimeError:
                pass  # The window may have closed during the network request.

        threading.Thread(target=run, name="gemini-web-models", daemon=True).start()

    def _receive_gemini_account(self, account, error):
        self._gemini_auth_summary = "；".join(str(account.get(key, "")) for key in ("source", "proxy") if account.get(key))
        self._receive_gemini_models(account.get("models", []), error)

    def _receive_gemini_models(self, models, error):
        self._gemini_probe_running = False
        self.btn_probe_gemini.setEnabled(True)
        if error:
            self.gemini_web_status.setText(error)
            return
        selected = self.gemini_web_model.currentText()
        self.gemini_web_model.clear()
        names = [model["name"] for model in models if model["available"]]
        self.gemini_web_model.addItems(["auto", *names])
        self.gemini_web_model.setCurrentText(selected)
        self.gemini_web_status.setText("登录有效" + ("；" + self._gemini_auth_summary if self._gemini_auth_summary else "")
                                      + "；可用模型：" + ("、".join(names) or "账号默认模型"))

    def closeEvent(self, event):
        self._gemini_link_timer.stop()
        if self._gemini_link:
            self._gemini_link.close()
            self._gemini_link = None
        super().closeEvent(event)

    def refresh_media_capabilities(self):
        """检查 ffmpeg、缓存大小与可用资源"""
        media = getattr(self.app_config, "media", None)
        if not media:
            return
        cache = Path(media.cache_dir)
        try:
            size = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file() and not p.is_symlink())
        except OSError:
            size = 0

        tools = "、".join(
            f"{name} {'可用' if shutil.which(getattr(media, name + '_path')) else '未找到'}"
            for name in ("ffmpeg", "ffprobe")
        )
        source = (
            "反重力自主检索、浏览与下载 → QQBot 交付本地成果" if media.search_provider == "native"
            else "自定义搜索接口 + 公共后备源" if media.search_api_url
            else "Bing + Wikimedia Commons" if media.search_provider == "auto"
            else "Wikimedia Commons"
        )
        self.media_capabilities.setText(
            f"搜索来源：{source}\n图片生成：{media.image_generation_provider}"
            + (f"；网页端模型：{media.gemini_web.model}" if media.image_generation_provider == "gemini_web" else "")
            + f"\n视频生成：{media.video_generation_provider}"
            + (f"；网页端模型：{media.gemini_web.model}" if media.video_generation_provider == "gemini_web" else "")
            + f"\n环境工具：{tools}\n"
            f"缓存使用：{size / 1048576:.1f} / {media.cache_max_mb} MB；登记资料库目录: {len(media.local_library_roots)} 个。\n"
            "支持格式：PNG/JPG/GIF/WEBP 图片、H.264 MP4 视频、PDF/TXT/CSV 文档附件。"
        )

    def show_media_asset(self, payload: dict):
        """展示已发送资产的预览信息与图片缩略图"""
        kind = {"image": "图片", "video": "视频", "file": "文件"}.get(payload.get("kind"), "资源")
        state = {"accepted": "平台已受理", "link": "已返回来源链接"}.get(payload.get("state"), "")
        duration = f" · {payload.get('duration', 0):.1f} 秒" if payload.get("kind") == "video" else ""
        self.media_preview_info.setText(
            f"{payload.get('title', '')} · {kind} · "
            f"{payload.get('size', 0) / 1048576:.2f} MB{duration} · {state}"
        )
        self.media_preview.clear()
        if payload.get("kind") == "image":
            from media_downloader import contained_path, image_metadata, MediaError
            try:
                path = contained_path(
                    payload.get("path", ""),
                    [self.app_config.media.cache_dir, "data/cache/images", "data/cache/exports", *self.app_config.media.local_library_roots]
                )
                image_metadata(path)
                pixmap = QPixmap(str(path))
                self.media_preview.setPixmap(
                    pixmap.scaled(380, 220, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                )
            except (MediaError, OSError):
                pass
