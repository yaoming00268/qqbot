"""Regenerate the fixed public API reference from its runtime contracts."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from console_tools.contracts import COMMANDS, GROUPS, FIELDS, STATES


def render():
    target = ROOT / "console_tools/API.md"
    preamble = target.read_text(encoding="utf-8").split("## 基础命令", 1)[0]
    lines = [preamble.rstrip(), "", "## 基础命令", ""]

    def commands(prefix, definitions):
        for name, spec in definitions.items():
            params = ", ".join(field + ("*" if field in spec["required"] else "") for field in spec["params"])
            lines.append(f'- `{prefix}{name}({params})`：{spec["description"]}')
            if spec["returns"]:
                lines.append("  返回：" + spec["returns"])

    commands("", COMMANDS)
    for group, definitions in GROUPS.items():
        lines.extend(["", "## " + group, ""])
        commands(group + " ", definitions)
    lines.extend(["", "## 参数类型", ""])
    for name, value in FIELDS.items():
        constraints = {key: item for key, item in value.items() if key not in ("type", "description")}
        extra = " " + json.dumps(constraints, ensure_ascii=False, separators=(",", ":")) if constraints else ""
        lines.append(f'- `{name}` {value["type"]}{extra}：{value["description"]}')
    lines.extend(["", "## 状态", ""])
    lines.extend(f"- `{key}`：{value}" for key, value in STATES.items())
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    render()
