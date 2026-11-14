"""Headless Windows VM lifecycle and game windows, controlled over QMP only."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import secrets
import socket
import subprocess
import time
import uuid
import zipfile

from PIL import Image

from vm_protocol import GuestClient, QMP, VMError, file_lock
from runtime_paths import application_home, resource_path

PROJECT = application_home()


def project_path(value):
    path = Path(value)
    return (path if path.is_absolute() else PROJECT / path).resolve()


def save_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class QemuVM:
    KEYS = {"enter": "ret", "space": "spc", "up": "up", "down": "down", "left": "left", "right": "right",
            "escape": "esc", "s": "s", "l": "l", "tab": "tab", "backspace": "backspace", "f5": "f5"}

    def __init__(self, directory, config=None):
        from config import VirtualMachineConfig
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = VirtualMachineConfig.model_validate(config or {})
        self.binary = project_path(self.config.qemu_path)
        self.disk = project_path(self.config.disk_path)
        self.lock_path = self.root / "control.lock"

    def credentials(self):
        path = self.root / "guest.json"
        if not path.is_file():
            save_json(path, {"vm_id": "qqbot-vm-" + uuid.uuid4().hex, "token": secrets.token_urlsafe(48), "port": 47653})
        return json.loads(path.read_text(encoding="utf-8"))

    def runtime(self):
        path = self.root / "process.json"
        if not path.is_file():
            raise VMError("VM_NOT_RUNNING", "虚拟机未运行。")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["disk"] != str(self.disk):
            raise VMError("VM_CONFIG_CHANGED", "磁盘配置已改变，请先停止原虚拟机。")
        return data

    def monitor(self):
        data = self.runtime()
        return QMP(data["qmp_port"], data["vm_id"])

    def guest(self):
        data, credentials = self.runtime(), self.credentials()
        return GuestClient(data["guest_port"], credentials["token"], credentials["vm_id"])

    def status(self):
        value = {"mode": "qemu", "os_isolated": True, "host_input": False,
                 "qemu_installed": self.binary.is_file(), "disk_exists": self.disk.is_file(),
                 "bootstrap_iso": str(self.root / "bootstrap.iso"), "disk_path": str(self.disk),
                 "state": "setup_required", "running": False, "guest_ready": False,
                 "accelerator": self.config.accelerator}
        try:
            monitor = self.monitor()
            state = monitor.execute("query-status")
            value.update(running=True, vm_id=monitor.name, state=state.get("status", "running"))
            try:
                self.guest().call("health")
                value.update(guest_ready=True, state="ready")
            except VMError as error:
                value.update(code=error.code, detail=str(error), state="guest_setup_required")
        except (OSError, ValueError) as error:
            value["code"] = getattr(error, "code", "VM_NOT_RUNNING")
        return value

    def prepare(self):
        with file_lock(self.lock_path):
            if not self.binary.is_file():
                raise VMError("VM_RUNTIME_MISSING", "尚未安装 QEMU，请运行 vm_setup.py。")
            if not self.disk.is_file():
                self.disk.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run([str(self.binary.with_name("qemu-img.exe")), "create", "-f", "qcow2", str(self.disk), "64G"],
                               check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            iso = self.root / "bootstrap.iso"
            if not iso.is_file():
                try:
                    import pycdlib
                except ImportError:
                    raise VMError("VM_DEPENDENCY_MISSING", "缺少 pycdlib，请运行 pip install pycdlib 或 pip install -r requirements.txt。") from None
                embedded = PROJECT / "tools/downloads/python-3.12.10-embed-amd64.zip"
                if not embedded.is_file():
                    raise VMError("VM_RUNTIME_MISSING", "缺少来宾 Python 运行时，请先运行 vm_setup.py。")
                credentials = self.credentials()
                image = pycdlib.PyCdlib()
                image.new(interchange_level=3, joliet=3, vol_ident="QQBOTINIT")
                entries = [(resource_path("vm_guest/agent.py"), "/AGENT.PY;1", "/agent.py"),
                           (resource_path("vm_guest/Setup.ps1"), "/SETUP.PS1;1", "/Setup.ps1"),
                           (embedded, "/PYTHON.ZIP;1", "/python.zip")]
                for source, iso_name, joliet in entries:
                    image.add_file(str(source), iso_path=iso_name, joliet_path=joliet)
                content = json.dumps(credentials).encode()
                buffer = io.BytesIO(content)
                image.add_fp(buffer, len(content), iso_path="/GUEST.JSN;1", joliet_path="/guest.json")
                image.write(str(iso))
                image.close()
            return {**self.status(), "prepared": True,
                    "next_step": "配置 Windows 安装 ISO，用 vm_control.py start 启动；系统内运行 QQBOTINIT 光盘 Setup.ps1。"}

    def start(self):
        with file_lock(self.lock_path):
            if not self.binary.is_file() or not self.disk.is_file() or not (self.root / "bootstrap.iso").is_file():
                raise VMError("VM_NOT_CONFIGURED", "虚拟机尚未准备；先执行 vm_control.py prepare。")
            current = self.status()
            if current["running"]:
                return current
            credentials = self.credentials()
            qmp_port, guest_port = free_port(), free_port()
            arguments = [str(self.binary), "-name", credentials["vm_id"], "-machine", "pc", "-accel", self.config.accelerator,
                "-m", str(self.config.memory_mb), "-smp", str(self.config.cpus), "-display", "none", "-vga", "std",
                "-usb", "-device", "usb-tablet", "-monitor", "none", "-serial", "none", "-parallel", "none",
                "-qmp", f"tcp:127.0.0.1:{qmp_port},server=on,wait=off",
                "-drive", f"file={self.disk},format=qcow2,if=ide,index=0",
                "-drive", f"file={self.root / 'bootstrap.iso'},media=cdrom,readonly=on,if=ide,index=3",
                "-netdev", f"user,id=guestnet,restrict=on,hostfwd=tcp:127.0.0.1:{guest_port}-:47653",
                "-device", "e1000,netdev=guestnet", "-boot", "menu=on,order=dc"]
            if self.config.install_iso:
                source = project_path(self.config.install_iso)
                if not source.is_file() or source.suffix.lower() != ".iso":
                    raise VMError("VM_ISO_MISSING", "Windows 安装 ISO 不存在。")
                arguments.extend(["-drive", f"file={source},media=cdrom,readonly=on,if=ide,index=2"])
            # No SDL/GTK viewer, clipboard, host input hooks or shared host dirs.
            with (self.root / "qemu.log").open("ab") as log:
                process = subprocess.Popen(arguments, cwd=self.binary.parent, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
            data = {"pid": process.pid, "vm_id": credentials["vm_id"], "qmp_port": qmp_port,
                    "guest_port": guest_port, "disk": str(self.disk), "started": time.time()}
            save_json(self.root / "process.json", data)
            for _ in range(40):
                if process.poll() is not None:
                    raise VMError("VM_START_FAILED", "QEMU 启动失败，详见虚拟机目录内 qemu.log。")
                try:
                    self.monitor().execute("query-status")
                    return self.status()
                except OSError:
                    time.sleep(.1)
            raise VMError("VM_START_PENDING", "虚拟机尚未返回状态；稍后查询，不重复启动。")

    def stop(self):
        with file_lock(self.lock_path):
            return self.monitor().execute("system_powerdown")

    def framebuffer(self, destination):
        destination = Path(destination).resolve()
        raw = destination.with_suffix(".ppm")
        self.monitor().execute("screendump", {"filename": str(raw)})
        try:
            with Image.open(raw) as picture:
                image = picture.convert("RGB")
            image.save(destination, "PNG")
            return image
        finally:
            raw.unlink(missing_ok=True)

    def package(self, target):
        """Transfer a copy, never mount the owner's game directory writable."""
        identity = target["id"]
        if self.guest().call("installed", {"game_id": identity}).get("installed"):
            return
        executable = Path(target["executable"]).resolve(strict=True)
        source = executable.parent
        package = self.root / (identity + ".zip")
        try:
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                info = json.dumps({"game_id": identity, "executable": executable.name})
                archive.writestr("qqbot-package.json", info)
                for file in source.rglob("*"):
                    if file.is_symlink() or not file.resolve().is_relative_to(source):
                        raise VMError("GAME_EXTERNAL_LINK", "游戏目录包含外部链接，请提供独立完整游戏目录。")
                    if file.is_file():
                        if file.name == "qqbot-package.json":
                            raise VMError("GAME_RESERVED_FILE", "游戏包含接口保留名称 qqbot-package.json。")
                        archive.write(file, file.relative_to(source).as_posix())
            self.guest().call("stage", file=package)
        finally:
            package.unlink(missing_ok=True)

    def launch(self, target):
        self.start()
        with file_lock(self.lock_path):
            self.guest().call("health")
            self.package(target)
            return self.guest().call("launch", {"game_id": target["id"]})

    def observe(self, target, path):
        with file_lock(self.lock_path):
            data = self.guest().call("target", {"game_id": target["id"], "focus": True})
            frame = self.framebuffer(path)
            bounds = self.checked_bounds(data["rect"], frame.size)
            image = frame.crop(bounds)
            image.save(path, "PNG")
            after = self.guest().call("target", {"game_id": target["id"]})
            if any(after.get(key) != data.get(key) for key in ("hwnd", "pid", "rect")) or not after.get("foreground"):
                raise VMError("GUEST_WINDOW_CHANGED", "来宾窗口变化，请重新观察。")
            return {key: data[key] for key in ("hwnd", "pid", "title", "rect")} | {
                "size": list(image.size), "frame_size": list(frame.size), "vm_id": self.credentials()["vm_id"],
                "mode": "qemu", "os_isolated": True}

    @staticmethod
    def checked_bounds(rect, size):
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4 or any(type(v) is not int for v in rect)
                or not 0 <= rect[0] < rect[2] <= size[0] or not 0 <= rect[1] < rect[3] <= size[1]):
            raise VMError("GUEST_WINDOW_OUTSIDE_SCREEN", "来宾游戏窗口超出画面，请在来宾中恢复窗口后重新观察。")
        return tuple(rect)

    def act(self, target, observation, action, *, key="", x=None, y=None, button="left", guard=None):
        with file_lock(self.lock_path):
            monitor = self.monitor()
            if observation.get("vm_id") != monitor.name:
                raise VMError("VM_IDENTITY_CHANGED", "截图来自其他虚拟机，未投递输入。")
            current = self.guest().call("target", {"game_id": target["id"], "focus": True})
            if any(current.get(field) != observation.get(field) for field in ("hwnd", "pid", "rect")) or not current.get("foreground"):
                raise VMError("GUEST_WINDOW_CHANGED", "来宾游戏窗口已变化，先重新观察。")
            if guard:
                guard()
            if action == "key":
                monitor.execute("send-key", {"keys": [{"type": "qcode", "data": self.KEYS[key]}], "hold-time": 60})
            else:
                width, height = observation["frame_size"]
                rect = self.checked_bounds(observation["rect"], (width, height))
                px, py = rect[0] + int(x * observation["size"][0]), rect[1] + int(y * observation["size"][1])
                self.guest().call("validate_click", {"game_id": target["id"], "x": px, "y": py, "hwnd": current["hwnd"]})
                if guard:
                    guard()
                events = [{"type": "abs", "data": {"axis": axis, "value": round(pixel * 32767 / max(1, extent - 1))}}
                          for axis, pixel, extent in (("x", px, width), ("y", py, height))]
                monitor.execute("input-send-event", {"events": events})
                monitor.execute("input-send-event", {"events": [{"type": "btn", "data": {"button": button, "down": True}}]})
                # Always release inside the guest even if cancellation arrives.
                try:
                    time.sleep(.06)
                finally:
                    monitor.execute("input-send-event", {"events": [{"type": "btn", "data": {"button": button, "down": False}}]})
            return {"posted": True, "verified": False, "mode": "qemu", "host_input": False,
                    "hint": "输入仅送到虚拟机；用新截图确认结果。"}
