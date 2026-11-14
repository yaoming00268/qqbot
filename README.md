# QQBot

QQ 官方机器人 / OneBot / 独立角色运行时，提供 pywebview + WebView2 桌面控制台、CLI、MCP、本地情感分析、媒体处理和隔离的后台任务。

这是纯源码仓库。包含 Python、网页 GUI、浏览器扩展、构建脚本、测试、依赖清单和空白配置模板；账号配置、人设、聊天与记忆、Cookie、数据库、模型、数据集、运行日志、截图、EXE 及第三方程序不在仓库内。首次运行需要自行配置依赖和账号。

## 运行

Windows 推荐 Python 3.11/3.12、Edge WebView2 Runtime；根据功能安装 Antigravity、NapCat、ffmpeg 等外部依赖。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.yaml.example config.yaml
# 编辑本地 config.yaml，填写所需账号与运行模式。
$env:QQBOT_USE_SOURCE = '1'
.\run_gui.bat
```

`config.yaml`、`personas.yaml`、`.env` 和 `data/` 均仅保存在本地。源码入口也可使用 `python gui.py`；CLI 使用 `python -m console_tools --help`，独立角色使用 `python -m agent_core --help`。未经配置不要连接真实账号。

## 独立情感工作台

训练和推理项目单独维护在 [emotion-studio](https://github.com/yaoming00268/emotion-studio)。QQBot 的旧版 LM Studio / 轻量分类器接口代码保留在本仓库。

为沿用默认的“打开 BERT 工作台”按钮，可把独立仓库克隆到本目录下的 `emotion_studio/`（此目录已忽略）：

```powershell
git clone https://github.com/yaoming00268/emotion-studio.git emotion_studio
powershell -ExecutionPolicy Bypass -File emotion_studio/setup.ps1
```

工作台加载模型后，在 QQBot 配置中选择 `emotion.provider: bert`，服务描述路径为 `emotion_studio/runtime/inference.json`；也可填写独立部署工作台的绝对路径。两个项目通过本机鉴权 HTTP 接口连接，机器人不加载 CUDA 训练依赖。

## 文档与验证

- [控制台与接口](console_tools/README.md)、[接口参考](console_tools/API.md)
- [Studio 桌面界面](ui/README.md)
- [运行包构建](build_support/README.md)
- [维护脚本](maintenance/README.md)

```powershell
python -m pytest -q
```

QQBot 测试由 `conftest.py` 转入临时源码副本并限制真实网络、账号和数据访问。历史资料库完整性、历史构建物等验收需要额外的本地文件；纯源码仓库不携带这些资料。
