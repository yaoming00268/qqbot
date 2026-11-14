# 主人身份与专属称谓管理模块 (Master Management Subsystem)

## 模块定位
负责机器人对主人（开发者 / 指定用户）的身份识别、专属称谓定制、群聊隔离配置以及配置持久化。使管理员能够在 GUI 监控侧边栏直接通过右键菜单将群成员设置为主人、自定义称谓（如“哥哥”、“主人”、“司令”等），并在所有交互轮次中永久生效与随时删除。

---

## 核心功能清单
1. **身份识别与多级判定**：
   - 支持**单群专属主人**（通过群专属 `master_id` 或名片/昵称 `master_nickname` 识别，各群互不干扰，群聊隔离）。
   - 支持**全局主人**（`bot.master_id` 支持多个 QQ 号逗号分隔）。
2. **专属称谓定制与提醒**：
   - 主人可配置专属称呼（例如“哥哥”、“主人”、“亲爱的”、“提督”等）。
   - 自动在提示词（Prompt）中注入称谓规范，指导大模型以主人专属亲近度回复，避免生硬或乱叫。
3. **侧边栏一键交互与永久保存**：
   - 监控侧边栏右键群友名字即可唤起上下文菜单，直接设置/修改称谓，并持久化回写至 `config.yaml`。
   - 支持直接从右键菜单移除/撤回主人特权。
4. **100% 必定回复特权**：
   - 主人在群内发言即使不 @ 机器人，也会触发 100% 优先参聊，展现对主人的绝对顺从与专属重视感。

---

## 文件结构
```
master/
├── __init__.py       # 导出 MasterManager, MasterRecord, default_master_manager
├── manager.py        # 主人增删改查、称谓解析与 config.yaml 持久化核心逻辑
└── README.md         # 模块设计规范、API 参考与测试维护指引
```

---

## 关键类与 API 说明

### 1. `MasterRecord`
存储查询到的主人信息实体：
```python
@dataclass
class MasterRecord:
    user_id: str           # QQ号或 OpenID
    nickname: str          # 昵称或群名片
    title: str = "主人"    # 专属称谓（如：哥哥、主人）
    group_id: str = ""     # 所属群号（为空代表全局）
    is_global: bool = False

    def display_tag(self) -> str:
        # 生成标签，如 "[主人: 哥哥]" 或 "[主人]"
```

### 2. `MasterManager`
主管理器接口：
```python
from master import default_master_manager

# 1. 检查指定群的某用户是否为主人
is_m = default_master_manager.is_master(group_id="1129846525", user_id="123456", nickname="雪村奈々子")

# 2. 获取该群对主人的称谓
title = default_master_manager.get_master_title(group_id="1129846525")  # 输出: "哥哥"

# 3. 设置主人并永久保存至 config.yaml
default_master_manager.set_master(
    user_id="123456",
    nickname="雪村奈々子",
    title="哥哥",
    group_id="1129846525",
    is_global=False,
    persist=True
)

# 4. 移除主人身份并持久化保存
default_master_manager.remove_master(
    user_id="123456",
    group_id="1129846525",
    persist=True
)
```

---

## 配置映射 (`config.yaml`)
持久化后在 `config.yaml` 中呈现形式如下：
```yaml
bot:
  master_id: "10001"              # 全局主人 ID
  master_title: "主人"             # 全局称谓
  group_settings:
    "1129846525":
      group_id: "1129846525"
      group_name: "某二次元交流群"
      master_id: "123456"          # 该群专属主人 QQ
      master_nickname: "雪村奈々子" # 该群主人的群名片
      master_title: "哥哥"          # 该群专属称呼
      persona: "天然呆猫娘妹妹"     # 该群专属人设
```

---

## 单元测试与验证建议
运行针对主人判定的测试套件：
```bash
pytest test_group_memory_and_isolation.py -k "test_group_custom_config_and_isolation"
```
新增测试需覆盖：
- 仅 ID 匹配
- 仅昵称匹配
- 称谓读取回退（群称谓 -> 全局称谓 -> 默认“主人”）
- 永久写入与重新加载持久化一致性
