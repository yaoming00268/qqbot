"""Game control through background windows or an explicitly configured VM."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from qemu_vm import QemuVM
from vm_protocol import file_lock
from runtime_paths import application_home


@contextmanager
def game_input_lock(timeout_ms=1000):
    # Retained for callers; file locks also work across MCP/CLI processes.
    path = application_home() / "data/sandbox/vm/input.lock"
    with file_lock(path, timeout_ms / 1000):
        yield


class GameWindow:
    from browser_control import SOFTWARE_KEYS
    KEYS = tuple(QemuVM.KEYS) + tuple(SOFTWARE_KEYS)

    def __init__(self, config=None, directory=None, *, mode="background"):
        root = Path(directory or application_home() / "data/sandbox")
        if mode == "background":
            from background_window import BackgroundWindow
            self.backend = BackgroundWindow(root / "windows")
        elif mode == "qemu":
            self.backend = QemuVM(root / "vm", config)
        else:
            raise ValueError("不支持的游戏控制模式。")

    def status(self):
        return self.backend.status()

    @staticmethod
    def validate_action(action, *, key="", x=None, y=None, button="left", text="", delta=0, url="", element_id="", software=False):
        if action == "key":
            if not isinstance(key, str) or key not in (GameWindow.KEYS if software else QemuVM.KEYS):
                raise ValueError("不支持的游戏按键。")
        elif action == "click" or action == "scroll" and software:
            if (type(x) not in (int, float) or type(y) not in (int, float)
                    or not 0 <= x < 1 or not 0 <= y < 1 or button not in ("left", "right", "middle")):
                raise ValueError("点击需为 0 到 1 之间的截图坐标，鼠标键为 left/right/middle。")
            if action == "scroll" and (type(delta) not in (int, float) or not 0 < abs(delta) <= 3000):
                raise ValueError("滚动 delta 为非零像素数，绝对值最多 3000；正数向下。")
        elif action in ("invoke", "fill", "select") and software:
            if not isinstance(element_id, str) or not element_id or len(element_id) > 100:
                raise ValueError("语义动作需要最新 inspect 返回的 element_id。")
            if not isinstance(text, str) or len(text) > 4000:
                raise ValueError("控件文本最多 4000 字。")
        elif action == "type" and software:
            if not isinstance(text, str) or not text or len(text) > 4000 or any(ord(char) < 32 and char not in "\n\t\r" for char in text):
                raise ValueError("输入文本应为 1–4000 字，不能包含控制字符。")
        elif action == "navigate" and software:
            from browser_control import checked_url
            checked_url(url)
        else:
            raise ValueError("游戏支持 key/click；软件另支持 type/scroll/navigate。")

    def launch(self, target):
        return self.backend.launch(target)

    def observe(self, target, path):
        return self.backend.observe(target, path)

    def inspect(self, target, observation):
        if target.get("kind") != "software" or not hasattr(self.backend, "inspect"):
            from computer_control import ControlError
            raise ControlError("SEMANTIC_UNAVAILABLE", "当前目标没有语义控件接口，请使用截图观察。")
        return self.backend.inspect(target, observation)

    def act(self, target, observation, action, *, key="", x=None, y=None, button="left", guard=None, **extra):
        self.validate_action(action, key=key, x=x, y=y, button=button, software=target.get("kind") == "software", **extra)
        return self.backend.act(target, observation, action, key=key, x=x, y=y, button=button, guard=guard, **extra)
