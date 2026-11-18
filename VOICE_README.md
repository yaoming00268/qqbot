# 语音与 DeepSeek Harness

## 自然语速与拖长音

主代理会获得可用标记并按语境决定是否添加，平时使用正常语速，不需要新增开关或逐次设置。`spoken_text`（或直接朗读的 `text`）支持：

| 标记 | 效果 |
| --- | --- |
| `[fast]我们一起出发吧。[/fast]` | 当前语速的 1.2 倍 |
| `[slow]听我慢慢说。[/slow]` | 当前语速的 0.8 倍 |
| `[stretch]呀[/stretch]` | 用当前语速的 0.65 倍拉长短词或语气音，不重复字音 |

闭合后恢复当前语速，最终速度限制在 0.5–2 倍。三个标记不能互相嵌套，可与情绪切换及 mixed 语言标签组合，标记自身不朗读。拖长音通过模型的速度控制延长该范围，不保证精确的单个元音时长；实际自然度仍取决于声音模型与参考切片。建议少量标注完整短句，拖长音仅标短词，避免逐字套标签。

普通台词会合并句内视觉换行、合并同状态下过短的应答，重分配长度上限产生的短尾段，减少碎片合成。语速切换不额外添加停顿，防截断检查按片段实际语速计算；旧音频缓存已隔离。QQ 和独立直播语音均支持这些标记。工作室可选中文字后点“拖长音 / 快读 / 慢读”试听，连接信息与推理参数收在高级设置。

语音输出走正常消息队列：主代理选择语音和情绪 → 日语假名化/混语分段 → 分词切句 → VoiceStudio 流式合成与降噪 → 24kHz 单声道 PCM → 合并 → 腾讯 SILK → QQ 语音回执。OneBot 发送 `record`，官方通道上传 `file_type=3`，不会把 WAV 当普通附件发出。官方通道需要配置已有的 HTTPS 文件发布服务；OneBot 本机直接读文件，远程 NapCat 使用已校验的流式上传。

## VoiceStudio、句内多情绪与流式试听

接入 [debpalash/VoiceStudio](https://github.com/debpalash/VoiceStudio)，固定 revision `4da51ddb4841e04e1e9878f9934fde46c21389f9`。本机采用轻量服务，实际导入上游 GPT-SoVITS 适配器、SentenceChunker 和 expression capabilities，扩展原生 api_v2 流式推理、角色情绪参考与降噪；复用当前模型和独立环境。没有安装完整 VoiceStudio 桌面程序或切换默认角色。许可及源码说明在 `third_party/VoiceStudio-NOTICE.md`。

```powershell
.\voice.bat studio-setup
.\voice.bat check
.\voice.bat preview --text 'こんにちは。[emotion:sad]少し寂しいけど[emotion:firm]大丈夫。' --emotion neutral --stream --no-cache
```

示例需先在控制台选择有 `sad`、`firm` 真实切片的 ATRI；当前泡普卡只有 `neutral`，不能直接使用其他标签。`[emotion:标签]` 从所在位置起持续生效，`[emotion:]` 恢复 `--emotion` 默认值。标记不会朗读，可放在一句话任意词语之间；切换片段不额外插入句间静音，合并时采用短交叉淡化。所有标签会在队列提交前检查，任一未知标签会整条拒绝。语言范围与情绪可组合，例如 mixed 模式的 `[ja]一緒なら[emotion:sad]寂しくても大丈夫。[/ja][en]We can do it.[/en]`。

本机 endpoint 为 `http://127.0.0.1:9881`，背后仍使用 `http://127.0.0.1:9880` 的 GPT-SoVITS。控制台 WebAudio 随 WebSocket PCM 分片播放，默认 80ms 一块；停止或离开页面会停止本机播放，停止任务也会取消合成。缓存命中时直接提供完整播放器。关闭“流式试听”会等待完整播放器；VoiceStudio 协议内部仍使用 WebSocket。GPT-SoVITS 的首包取决于当前模型生成片段的耗时，小网络帧不会使它变成逐 token 生成。QQ 的 record 需要完整 SILK，不能将尚未完成的 PCM 直接作为一条 QQ 实时语音发送。

引擎复用已核实推理进程中的权重选择；台词、情绪、设置、模型指纹、参考内容相同且 WAV/SILK 哈希正确时复用完整音频。参考降噪单独按内容缓存，原文件保留。流式 WAV 和 WebSocket 都校验音频格式、大小、时长和完成状态；断流、半个采样、缺失 done 帧都会失败。仅在本机门面连接不可用且尚未提交推理时退回原生接口，鉴权或内容错误、生成途中失败不自动重发。消息积压和用户停止仍沿用原队列取消流程。

降噪用本机 FFmpeg `afftdn`，默认 6dB，可分别关闭语音降噪、参考降噪或调节强度。默认采样步数从 32 降为 16；可在工作室调高，速度和细节由模型与设备决定。调试可使用 `--no-cache`、`--no-denoise`、`--no-stream`。安装器只新增轻量依赖，完整角色及基础模型仍由原安装器提供。

连接已有完整 VoiceStudio 服务时，将 `voice.voicestudio.local_bridge` 设为 `false`，`voice.endpoint` 填其 HTTP 根地址，并选择 `voice.voicestudio.engine`。在角色或每个情绪对象填写 `voicestudio_profile_id`；可在情绪对象的 `voicestudio` 中填写 `instruct`、`emo_text`、`emo_alpha`、`emo_vector`，由所选引擎支持的控制方式决定实际效果。远程 HTTP 探测与 WebSocket 使用同一个 `api_key_env` Bearer 凭据。该模式无需本机 GPT 权重或参考路径，不会退回本机的另一种声音。完整服务的其他引擎需使用者自行安装；本次实测覆盖本机 GPT-SoVITS 集成。

## 当前工作区

`tools/voice-engine/.venv` 是独立推理环境。Bot 不加载 torch，也不修改情感工作台环境；本机推理环境通过 `.pth` 只读复用其已有 torch。泡普卡日语 v4 模型和默认参考 WAV 在 `data/voice/models/popukar`，ATRI 样板在 `data/voice/models/atri`，角色、情绪与参考台词在 `data/voice/catalog.json`。默认语言为日语，默认输出仍由主代理按语境选择。启动器切换新运行包后，需要退出旧核心并重开，才能使用新增代码。

GUI“语音工作室”可调语言、角色、速度和接口，编辑情绪切片与读音词典，查看分词并本机试听。试听不会发送 QQ 消息，关闭 QQ 语音开关时也可试听。切换语言后保存设置，下一轮代理获得新的语言能力。日语的 `spoken_text` 应由代理写成日语，前端负责转为假名；它不承担中文翻译。数字、拉丁字母也会转换读音，生僻字不能完整分词时明确报错。专名及多音字请填词典或直接写假名。

“角色声音”下拉框列出目录中的角色，切换后同步可用情绪并清除旧试听。泡普卡 ID 为 `popukar`，使用 `tools/voice-engine/popukar-infer.yaml`；原包只提供 `neutral` 一条默认切片，不伪造其他情绪。参考台词按 PRTS 原文填写为「ポプカル、どうすればいいのか知ってるよ。」，原 ZIP 文件名的转写有误，不能直接用作提示文本。模型发布者为红血球AE3803与白菜工厂1145号员工，来源为 https://www.ai-hobbyist.com/thread-964-1-1.html，仅限非商业二次创作且禁止二次分发。

源码环境运行 `python -X utf8 maintenance/prepare_popukar_voice.py` 可重建该角色目录与推理配置；安装器校验模型 ZIP 以及 v4 底模、声码器的固定 SHA-256，保留来源清单。安装不切换 Bot 当前角色。在语音工作室选择“泡普卡（日语）”、日语、`neutral`，保存即可使用现有 CLI `reply --voice --emotion neutral` 工作流。

主代理查询 `voice` 获取当前语言和真实可用情绪，再自行决定是否使用语音：

```powershell
.\cli.bat voice --context-file <当前上下文文件>
.\cli.bat reply --context-file <当前上下文文件> --text "一起读书吧。" --voice --emotion neutral --spoken-text "いっしょにほんをよみましょう。" --finish
```

MCP 等价调用：`qqbot_reply(context_file, text, voice=true, emotion="neutral", spoken_text="いっしょにほんをよみましょう。")`。语音只接受一条正文，长文本在内部切段后合成一条语音；文本是日志/记忆里的含义，`spoken_text` 是真实朗读台词。未指定情绪、未知标签、未启用语音会在提交前拒绝。`voice=false` 沿用文字回复。

混语模式支持 `[ja]一緒に。[/ja][zh]一起。[/zh][en]Together.[/en]`。没有显式标记时按句推断：有假名判日语、纯汉字判中文、其余判英语。纯汉字日语必须用 `[ja]` 标记。多语种是否自然取决于角色模型；ATRI 是日语角色样板。

每次合成在 `data/cache/voice/<任务>/` 保存原始分段、WAV、SILK、分词和来源 manifest。消息积压/重置会取消尚未提交的语音；提交后没有回执记为 `unknown`，不自动重发。语音同样受官方五次回复额度限制，每条语音占一次。

## 安装、诊断与搬迁

源码：`python -X utf8 voice_cli.py <命令>`。便携包：`voice.bat <命令>`，或 `QQBot.exe --home <数据目录> --module voice <命令>`。这组本地命令从不发送 QQ 消息：

```powershell
.\voice.bat status
.\voice.bat prepare --text "一緒に本を読む。" --language ja
.\voice.bat engine start
.\voice.bat check
.\voice.bat preview --text "こんにちは。" --emotion neutral
.\voice.bat engine stop
```

便携包包含假名词典、SILK 编码器和 DSH 官方独立运行时；GPU 环境与模型不塞进主程序。新机器安装需要 Git、Python 3.10–3.12、ffmpeg；CUDA 模式需支持所选 PyTorch 构建的 NVIDIA 驱动。运行：

```powershell
.\voice.bat setup --python C:\Python312\python.exe --device cuda
.\voice.bat studio-setup
# 无 NVIDIA GPU 可选 --device cpu（推理较慢）
# 可用 --torch-site-packages D:\ExistingEnv\Lib\site-packages 只读复用已安装的 torch
```

安装器固定推理引擎 commit `e030403bcafbd26bb3eb8ac376226e7ccaaec8c0`、ATRI 和基础编码器 revision，校验 Hugging Face LFS SHA-256，保留来源清单；独立安装 torch/torchaudio 2.8.0，Windows 前端自动为 jieba_fast 加纯 Python jieba 回退。日志输出到终端，最终 JSON 给出路径。`--skip-install` 校验现有环境并重建模型目录和推理配置，适合搬迁数据目录后使用。安装不改 Bot 总配置和现有其他角色。

准备完成后，在语音工作室选择 `voicestudio`、`http://127.0.0.1:9881`、角色 `atri`，开启语音与自动启动，保存。直接使用原接口时可选 `gpt_sovits_v2` 和端口 9880。引擎启动/停止核对并管理本项目的 VoiceStudio 与原生推理进程树。

也可使用 X-T-E-R/GPT-SoVITS-Inference 的 `/character_list` 和 `/tts` 角色服务：provider 选 `gpt_sovits_inference`，检查接口后同步角色与情绪。该模式的权重和切片由服务器管理；不存在的情绪不会静默回退到默认情绪。api_v2 的文件路径必须对推理服务器可读，远程服务器应在目录中填写服务端路径。

ATRI 来源：[2DIPW/ATRI_GPT-SoVITS](https://huggingface.co/2DIPW/ATRI_GPT-SoVITS)，作者 bilibili@二维环状无限深势阱 (2DIPW)，CC-BY-NC-SA-4.0，另请保留并遵守模型 README 的用途说明。仅作本地非商业评估。neutral/sad/firm 是按参考台词手动绑定，需试听校准，不是自动情绪识别的结论。

## DeepSeek Harness

使用官方 `deepseek-harness-sdk==0.2.2rc2` 与同版 `deepseek-harness-runtime-bin`，不调用旧的全局 dsh，不需要另装 Node。默认后端保留 Antigravity。需切换时，在 GUI 模型设置选 `deepseek_harness`，填 provider/model，先用“检查 DeepSeek Harness”验证官方运行时握手。检查不发送模型请求；成功不表示模型 API 凭据已经验证。

```yaml
model_backend:
  backend: deepseek_harness
deepseek_harness:
  home: data/dsh
  workspace: .
  provider: deepseek-official
  model: deepseek-v4-flash
  api_key_env: DEEPSEEK_API_KEY
```

启动 Bot 的进程环境中设置 `DEEPSEEK_API_KEY`；密钥不写入 Bot 配置或预设。可填写 `base_url` 使用兼容的 DeepSeek Messages 服务。默认 `data/dsh` 是独立 Harness Home，若已有官方登录配置，可显式选用对应 Home。

主代理只公开 QQBot 的 CLI、快捷回复、受控记忆读取和帮助；后台工作分支使用官方文件/终端/网页工具和 QQBot MCP。持续子代理仍由 QQBot 信箱管理，避免两套队列竞争。身份预设作为 system persona，恢复同一个原生会话 ID 和 JSONL 历史，不逐轮当新角色发送。AG 与 DSH 的会话映射分别保存；切换模型保留当前 DSH 历史，切换后端不自动移植另一后端的模型历史。

真实模型鉴权和 QQ 发送应由使用者在自己的配置下验证。本轮验收范围见 `maintenance_reports/voice-dsh-20261114/REPORT.md`。
# 训练专属角色声音

“声音训练”支持标注数据导入、自动切分与转写、复核、GPT/SoVITS 微调、验证和注册。详见 [VOICE_TRAINING_README.md](VOICE_TRAINING_README.md)。切换角色现在立即应用，并在语音工作室显示 Bot 的应用回执。

