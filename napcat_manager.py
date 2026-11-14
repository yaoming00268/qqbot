# -*- coding: utf-8 -*-
"""
NapCatQQ (OneBot v11) Downloader, Configurator and Process Manager for Windows.
Enables automated setup, multi-account configuration, and launching for local QQNT accounts.
"""

from __future__ import annotations
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("NapCatManager")
logger.setLevel(logging.INFO)

# NapCat 常用下载源 (支持 GitHub 官方、ghproxy 代理以及国内镜像)
# 对应最新 NapCat 发行版 Windows 独立自带 Node 运行时免配置版 (NapCat.Shell.Windows.Node.zip)
NAPCAT_DOWNLOAD_MIRRORS = [
    "https://mirror.ghproxy.com/https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.Windows.Node.zip",
    "https://ghproxy.net/https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.Windows.Node.zip",
    "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.Windows.Node.zip",
    "https://mirror.ghproxy.com/https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.Windows.OneKey.zip"
]

DEFAULT_NAPCAT_DIR = "napcat"


class NapCatManager:
    """
    NapCatQQ (OneBot v11) 自动化管理器:
    负责 NapCat 的检测、下载解压、双账号 OneBot 配置写入、启动及 WebUI 打开。
    """

    def __init__(self, base_dir: Optional[str] = DEFAULT_NAPCAT_DIR):
        self.base_dir = os.path.abspath(base_dir or DEFAULT_NAPCAT_DIR)

    def is_installed(self, custom_path: Optional[str] = None) -> bool:
        """检查 NapCat 是否已安装"""
        target = os.path.abspath(custom_path) if custom_path else self.base_dir
        if os.path.isfile(target):
            return True
        if not os.path.isdir(target):
            return False

        # 检查常见可执行文件或启动脚本
        indicators = [
            "napcat.win32.x64.exe",
            "napcat.exe",
            "napcat.bat",
            "launcher.bat",
            "napcat.cmd",
            "napcat.ps1",
            "napcat.mjs"
        ]
        for ind in indicators:
            if os.path.exists(os.path.join(target, ind)):
                return True
        return False

    def get_executable_path(self, custom_path: Optional[str] = None) -> Optional[str]:
        """获取 NapCat 可执行文件或启动脚本的完整路径"""
        target = os.path.abspath(custom_path) if custom_path else self.base_dir
        if os.path.isfile(target):
            return target
        if not os.path.isdir(target):
            return None

        candidates = [
            "napcat.win32.x64.exe",
            "napcat.exe",
            "napcat.bat",
            "launcher.bat",
            "napcat.cmd",
            "napcat.ps1"
        ]
        for c in candidates:
            p = os.path.join(target, c)
            if os.path.isfile(p):
                return p
        return None

    def download_and_extract(
        self,
        target_dir: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Tuple[bool, str]:
        """
        从镜像源自动下载 NapCat Windows 发行包并解压至目标目录。
        """
        out_dir = os.path.abspath(target_dir or self.base_dir)
        os.makedirs(out_dir, exist_ok=True)
        zip_path = os.path.join(out_dir, "NapCat.win32.x64.zip")

        success = False
        last_error = ""

        for mirror_url in NAPCAT_DOWNLOAD_MIRRORS:
            try:
                msg = f"正在尝试从 {mirror_url.split('/')[2]} 下载 NapCatQQ..."
                logger.info(msg)
                if progress_callback:
                    progress_callback(0, 100, msg)

                req = urllib.request.Request(
                    mirror_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QQBot/1.0"}
                )

                with urllib.request.urlopen(req, timeout=30) as resp:
                    total_bytes = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    chunk_size = 1024 * 64

                    with open(zip_path, "wb") as f_out:
                        while True:
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break
                            f_out.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and total_bytes > 0:
                                percent = int((downloaded / total_bytes) * 100)
                                progress_callback(
                                    downloaded,
                                    total_bytes,
                                    f"下载中: {downloaded // 1024} KB / {total_bytes // 1024} KB ({percent}%)"
                                )

                success = True
                break
            except Exception as e:
                last_error = str(e)
                logger.warning(f"从 {mirror_url} 下载失败: {e}，尝试下一个备用源...")

        if not success or not os.path.exists(zip_path):
            return False, f"下载 NapCatQQ 失败: {last_error}。您可以手动从 https://github.com/NapNeko/NapCatQQ/releases 下载解压至 {out_dir}"

        # 解压
        try:
            if progress_callback:
                progress_callback(95, 100, "正在解压缩 NapCatQQ 安装包...")
            logger.info(f"正在解压缩 {zip_path} 到 {out_dir} ...")
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(out_dir)

            try:
                os.remove(zip_path)
            except Exception:
                pass

            if progress_callback:
                progress_callback(100, 100, "NapCatQQ 下载并解压完成！")
            return True, f"NapCatQQ 已成功下载并安装至: {out_dir}"
        except Exception as e:
            return False, f"解压 NapCat 压缩包异常: {e}"

    def setup_dual_accounts(
        self,
        accounts: Any,
        target_dir: Optional[str] = None
    ) -> Tuple[bool, str]:
        """为多账号自动配置 NapCat，兼容 GUI 并返回 (ok, msg)。"""
        acc_dict = {}
        if isinstance(accounts, dict):
            for k, v in accounts.items():
                if hasattr(v, "model_dump"):
                    acc_dict[k] = v.model_dump()
                elif isinstance(v, dict):
                    acc_dict[k] = v
                else:
                    acc_dict[k] = dict(v)
        else:
            return False, "账号配置格式错误。"
        try:
            ok = self.setup_multi_account_configs(acc_dict, target_dir=target_dir)
            if ok:
                return True, "NapCat 账号配置已成功生成！"
            return False, "NapCat 配置文件写入失败。"
        except Exception as e:
            return False, f"配置 NapCat 异常: {e}"

    def setup_multi_account_configs(
        self,
        accounts: Dict[str, dict],
        target_dir: Optional[str] = None
    ) -> bool:
        """
        为用户的两个 QQ 小号自动生成 OneBot v11 配置文件。
        严格遵从 NapCat 最新运行时契约（network.websocketServers 结构及 token 鉴权），
        支持多目录检测（napcat/config、napcat/napcat/config 及 NAPCAT_WORKDIR），并采用原子写入。
        """
        config_dirs = []
        if target_dir:
            target_abs = os.path.abspath(target_dir)
            config_dirs.append(os.path.join(target_abs, "config"))
            sub_napcat = os.path.join(target_abs, "napcat")
            if os.path.isdir(sub_napcat):
                config_dirs.append(os.path.join(sub_napcat, "config"))
        else:
            workdir = os.environ.get("NAPCAT_WORKDIR")
            if workdir:
                config_dirs.append(os.path.join(os.path.abspath(workdir), "config"))
            base = os.path.abspath(self.base_dir)
            config_dirs.append(os.path.join(base, "config"))
            # 随附运行时在未设 NAPCAT_WORKDIR 时默认从模块同级 config 加载
            sub_napcat = os.path.join(base, "napcat")
            if os.path.isdir(sub_napcat):
                config_dirs.append(os.path.join(sub_napcat, "config"))

        # 确保目录去重且已创建
        unique_dirs = []
        for d in config_dirs:
            norm = os.path.normpath(d)
            if norm not in unique_dirs:
                unique_dirs.append(norm)
                os.makedirs(norm, exist_ok=True)

        try:
            for acc_key, acc_data in accounts.items():
                if hasattr(acc_data, "model_dump"):
                    acc_data = acc_data.model_dump()
                qq_id = str(acc_data.get("account_id", "") if isinstance(acc_data, dict) else getattr(acc_data, "account_id", "")).strip()
                ws_url = acc_data.get("ws_url", "ws://127.0.0.1:3001") if isinstance(acc_data, dict) else getattr(acc_data, "ws_url", "ws://127.0.0.1:3001")
                token = acc_data.get("access_token", "") if isinstance(acc_data, dict) else getattr(acc_data, "access_token", "")

                port = 3001
                if ":" in ws_url:
                    try:
                        port = int(ws_url.split(":")[-1].split("/")[0])
                    except ValueError:
                        port = 3001

                filenames = []
                if qq_id:
                    filenames.append(f"onebot11_{qq_id}.json")
                filenames.append(f"onebot11_{acc_key}.json")
                if acc_key == "account_1":
                    filenames.append("onebot11.json")

                for cfg_dir in unique_dirs:
                    for fn in filenames:
                        target_file = os.path.join(cfg_dir, fn)
                        existing = {}
                        if os.path.isfile(target_file):
                            try:
                                with open(target_file, "r", encoding="utf-8") as rf:
                                    loaded = json.load(rf)
                                    if isinstance(loaded, dict):
                                        existing = loaded
                            except Exception:
                                existing = {}

                        network = existing.get("network", {})
                        if not isinstance(network, dict):
                            network = {}

                        ws_servers = network.get("websocketServers", [])
                        server_entry = {
                            "name": f"ws-{qq_id or acc_key}",
                            "enable": True,
                            "host": "127.0.0.1",
                            "port": port,
                            "messagePostFormat": "array",
                            "reportSelfMessage": False,
                            "token": token,
                            "enableForcePushEvent": True,
                            "debug": False,
                            "heartInterval": 30000
                        }
                        new_ws_servers = [s for s in ws_servers if isinstance(s, dict) and s.get("port") != port]
                        new_ws_servers.append(server_entry)

                        ob11_config = {
                            **existing,
                            "network": {
                                "httpServers": network.get("httpServers", []),
                                "httpSseServers": network.get("httpSseServers", []),
                                "httpClients": network.get("httpClients", []),
                                "websocketServers": new_ws_servers,
                                "websocketClients": network.get("websocketClients", []),
                                "plugins": network.get("plugins", [])
                            },
                            "musicSignUrl": existing.get("musicSignUrl", ""),
                            "enableLocalFile2Url": existing.get("enableLocalFile2Url", False),
                            "parseMultMsg": existing.get("parseMultMsg", False),
                            "imageDownloadProxy": existing.get("imageDownloadProxy", ""),
                            "timeout": existing.get("timeout", {
                                "baseTimeout": 10000,
                                "uploadSpeedKBps": 256,
                                "downloadSpeedKBps": 256,
                                "maxTimeout": 1800000
                            }),
                            # 兼容旧版顶层 http / ws 字段
                            "http": existing.get("http", {
                                "enable": False,
                                "host": "",
                                "port": port + 1000,
                                "secret": token,
                                "enableCors": True
                            }),
                            "ws": {
                                "enable": True,
                                "host": "",
                                "port": port,
                                "secret": token
                            },
                            "reverseWs": existing.get("reverseWs", {
                                "enable": False,
                                "urls": []
                            }),
                            "GroupLocalTime": existing.get("GroupLocalTime", {
                                "Record": False,
                                "RecordList": []
                            }),
                            "debug": existing.get("debug", False),
                            "heartInterval": existing.get("heartInterval", 30000),
                            "messagePostFormat": existing.get("messagePostFormat", "string"),
                            "enableQsign": existing.get("enableQsign", False),
                            "reportSelfMessage": existing.get("reportSelfMessage", False)
                        }

                        # 原子写入防半写
                        tmp_target = f"{target_file}.tmp.{uuid.uuid4().hex[:8]}"
                        with open(tmp_target, "w", encoding="utf-8") as f:
                            json.dump(ob11_config, f, indent=2, ensure_ascii=False)
                        os.replace(tmp_target, target_file)
                        logger.info(f"已生成 OneBot v11 配置文件: {target_file} (WS 端口: {port})")

            # 生成 napcat.json 基础全局配置
            napcat_base = {
                "fileLog": True,
                "consoleLog": True,
                "fileLogLevel": "debug",
                "consoleLogLevel": "info"
            }
            for cfg_dir in unique_dirs:
                napcat_json = os.path.join(cfg_dir, "napcat.json")
                tmp_napcat = f"{napcat_json}.tmp.{uuid.uuid4().hex[:8]}"
                with open(tmp_napcat, "w", encoding="utf-8") as f:
                    json.dump(napcat_base, f, indent=2, ensure_ascii=False)
                os.replace(tmp_napcat, napcat_json)

            return True
        except Exception as e:
            logger.error(f"生成 NapCat 账号配置失败: {e}", exc_info=True)
            return False

    def launch_napcat(
        self,
        account_qq: str = "",
        custom_path: Optional[str] = None
    ) -> Tuple[bool, str]:
        """启动 NapCat 进程进行登录"""
        exe = self.get_executable_path(custom_path)
        if not exe:
            return False, f"未在 {custom_path or self.base_dir} 找到 NapCat 可执行文件，请先下载安装！"

        work_dir = os.path.dirname(exe)
        try:
            args = [exe]
            if account_qq:
                args.extend(["-q", str(account_qq)])

            if sys.platform == "win32":
                # Windows 下通过独立新控制台窗口拉起，避免阻塞主进程
                subprocess.Popen(
                    args,
                    cwd=work_dir,
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                )
            else:
                subprocess.Popen(args, cwd=work_dir)

            return True, f"NapCatQQ 启动成功！已在独立窗口拉起: {exe}"
        except Exception as e:
            logger.error(f"启动 NapCatQQ 异常: {e}", exc_info=True)
            return False, f"启动 NapCat 失败: {e}"

    @staticmethod
    def is_port_open(host: str = "127.0.0.1", port: int = 3001, timeout: float = 1.0) -> bool:
        """检查指定主机和端口是否处于监听可用状态"""
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except (OSError, socket.error):
            return False

    def ensure_running(
        self,
        account_qq: str = "",
        host: str = "127.0.0.1",
        port: int = 3001,
        max_wait: float = 15.0
    ) -> Tuple[bool, str]:
        """
        保证 NapCat 正在运行：
        若指定端口未开启，则自动调用 launch_napcat 启动进程，并等待直到端口可用。
        """
        if self.is_port_open(host, port):
            return True, f"NapCat 端口 {port} 处于监听状态，服务正常。"

        if not self.is_installed():
            return False, f"未检测到本地 NapCat 安装 (目录 {self.base_dir} 不存在)。"

        logger.info(f"🚀 [NapCatManager] 未检测到 NapCat (端口 {port} 未就绪)，尝试自动拉起...")
        ok, msg = self.launch_napcat(account_qq=account_qq)
        if not ok:
            return False, msg

        # 等待端口就绪
        start_t = time.time()
        while time.time() - start_t < max_wait:
            if self.is_port_open(host, port):
                logger.info(f"✅ [NapCatManager] NapCat 端口 {port} 已成功就绪！")
                return True, f"NapCat 核心启动就绪 (端口 {port})"
            time.sleep(0.8)

        return False, f"NapCat 已拉起，但在 {max_wait} 秒内端口 {port} 仍未处于监听状态，请检查 NapCat 窗口提示。"

    @staticmethod
    def open_webui(port: int = 6099) -> Tuple[bool, str]:
        """在系统默认浏览器中打开 NapCat WebUI 扫码登录后台"""
        url = f"http://127.0.0.1:{port}/webui/"
        try:
            webbrowser.open(url)
            return True, f"已在浏览器中打开 WebUI: {url}"
        except Exception as e:
            logger.error(f"打开 WebUI 失败: {e}")
            return False, f"打开 WebUI 失败: {e}"

