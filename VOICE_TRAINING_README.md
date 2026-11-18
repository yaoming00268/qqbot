# 角色声音训练

控制台的“声音训练”提供独立于情感分类器的 TTS 训练流程。语音工作室切换角色立即保存；“应用语音设置到 Bot”应用其他语音设置。页面会区分已保存、等待核心确认与 Bot 已应用。正在合成时换声音，旧音频在发送前取消；聊天会话与记忆继续保留。

## 训练路线

默认采用 GPT-SoVITS 官方 v2ProPlus 的 GPT 和 SoVITS 双阶段微调。它支持日语、少量语料、现有角色推理接口，并适配本机 8GB CUDA 显存。v4 LoRA 为可选路线，不能混用不同版本的训练权重。没有脱离数据量、语言与硬件的统一“最优模型”；这里优先选择可实际训练、速度较快并能接入当前 Bot 的路线。

训练器复用本项目固定的 GPT-SoVITS-Inference 源码。为适配 Windows 内存，训练副本将 DataLoader 改为零子进程，并提前创建实际使用的检查点目录；Windows 单 GPU 不使用分布式进程组，GPT 采用 Lightning 的单卡策略和显式单卡采样器参数。保留原优化器与训练循环，每次保存训练器与 DataModule 的源码、适配副本、补丁说明和 SHA-256。训练资产来自官方 `lj1995/GPT-SoVITS`，下载固定解析到的提交并核验 LFS SHA-256。

- [官方项目与训练说明](https://github.com/RVC-Boss/GPT-SoVITS)
- [官方预训练资产](https://huggingface.co/lj1995/GPT-SoVITS)
- [Qwen3-TTS 官方微调方案，作为选型对照](https://github.com/QwenLM/Qwen3-TTS/tree/main/finetuning)

## 使用流程

1. 首次点击“安装训练环境”和“下载官方预训练资产”。环境位于 `tools/voice-engine/.train-venv`，只读复用已有推理环境和 CUDA Torch，不修改情感训练环境。
2. 导入 CSV/JSONL/.list；或选择本地音频执行切分、日语转写和标签建议。默认 Faster Whisper small，精度优先时可填写 `Systran/faster-whisper-large-v3` 或本机模型目录。
3. 在样本表中逐条试听并核对台词。情绪、说话方式和强度通过下拉框选择，分别有 43、16、4 个中文选项与说明；已有自定义情绪继续保留。确认后勾选“已听辨”并保存。自动转写与基于台词的情绪建议均保留未复核标记；标签建议置信度为 0.35，不代表声学情绪识别的置信度。导入已有人工标注默认视为已复核，也可用 `reviewed=false` 明确标记。
4. 开始训练。默认 batch=2、SoVITS 8 轮、GPT 10 轮。训练不向验证集泄漏同一音源的切片或相同台词；验证音频不参与训练。小语料和未复核语料生成的模型标注为实验模型。
5. “验证试听”使用留出文本实际合成 WAV，并检查转码流程。该检查不等于客观音色相似度、发音准确率或情绪质量评测，仍应听辨。
6. “注册角色”写入新的 catalog ID，拒绝覆盖已有角色。随后“应用到 Bot”。主代理继续通过原有 `reply --voice --emotion` 工作。

GPT-SoVITS 的这条路线不直接将 emotion 当成额外的监督输入。它从真实角色录音学习音色和发音，情绪标签用于分组统计、审阅和挑选该情绪的参考切片。没有样本的情绪不会被编造成可用情绪；台词里不添加伪造的训练控制词。

## 将训练切片用作参考

在“声音训练 → 样本复核”选择目标角色，保存标注后点击对应行的“用作参考”。默认选择该数据集已发布的角色。绑定使用当前保存的完整台词、情绪、发声方式和强度；修改标注不会悄悄替换已发布参考，需要再次点击绑定。已有同情绪参考会要求明确替换。

参考会独立保存到 `data/voice/references/training`，保留音源 SHA-256、数据集与切片 ID、实际时长及复核状态。1–3 秒的完整切片只在副本末尾补静音至 3.1 秒以满足引擎要求，不添加或裁掉语音；超过 10 秒需重新切分并核对文本。原始训练音频保持不变。语音工作室的参考编辑弹窗也提供同一套中文下拉。

CLI：`voice.bat train use-reference --dataset 数据集ID --sample 切片ID --character 角色ID`；替换已有同情绪绑定时加 `--replace-reference`。

## 小样本模型的提前结束保护

实验角色默认按标点和最长 32 字假名短语合成，避免把长句尾部交给同一次早停预测；成熟角色保持原有切句。可在角色目录中通过 `synthesis.max_chunk_chars`（20–100）调整。合成缓存版本已更新，旧音频不会继续命中。

实验角色先接收完整短语，再进行保守的时长检查。明显过短的片段不会播放或提交，使用另一随机种子与串行推理重试一次；再次异常则明确失败，不编码成可交付的 SILK。网络仍采用流式接收，控制台按通过检查的短语连续播放。该检查不是逐字 ASR 校验，不能保证小样本模型的全部发音与情绪准确；更好的参考台词和更多干净语料仍会影响效果。

## 标注格式

CSV 列：`audio,text,speaker,language,emotion,delivery,intensity,reviewed,source_group`；旧文件缺少 `delivery/intensity` 时分别默认为 `normal/medium`。

JSONL 每行一个对象，例如：

```json
{"audio":"clips/001.wav","text":"こんにちは。","speaker":"chieri","language":"ja","emotion":"neutral","reviewed":true,"source_group":"recording-01"}
```

`.list` 为 `音频路径|角色ID|ja|台词`，可附加第五列情绪。没有情绪时记为 neutral。相对音频路径以标注文件所在目录为基准。每个数据集只包含一个角色。导入会重采样为 32kHz 单声道 PCM，检查 1–30 秒时长、静音、削波、重复和文件哈希；原件保留。

`source_group` 应标识切分前的原始录音。同一录音切出来的多个文件应填写相同值；自动切分会自动记录。这样留出集才不会与训练集混用同一段录音。

## 雪村千绘莉样本

提供从《あまいろショコラータ》1/2/3 官方角色页面取得普通试听片段的入口，保留原文件、URL、哈希、切分时间线和转写来源。官网当前共提供 9 个普通样本；数量较少，应作为实验训练素材。来源记录保留原权利说明；数据集和训练权重不会随干净运行包分发。

## CLI

```text
voice.bat train setup
voice.bat train assets
voice.bat train fetch-chieri
voice.bat train prepare --audio D:\voices\recording.wav --speaker chieri
voice.bat train import --path D:\voices\labels.jsonl
voice.bat train status
voice.bat train run --dataset DATASET_ID
voice.bat train run --dataset DATASET_ID --allow-unreviewed
voice.bat train run --dataset DATASET_ID --resume RUN_ID --allow-unreviewed
voice.bat train validate --run RUN_ID
voice.bat train publish --run RUN_ID --character NEW_CHARACTER_ID
voice.bat train log --run RUN_ID
```

取消会结束本任务拥有的 worker 及其训练子进程，保留检查点；没有完成的训练不会注册到角色目录。继续训练使用原始数据快照和参数，数据修改后应新建训练。训练与唱歌共用 GPU 互斥，并在持有原生推理锁后暂时停止本项目拥有的 TTS 进程，结束后恢复。Bot 本身的聊天和记忆进程保持运行。

数据位于 `data/voice/training`。每次训练保存 `manifest.json`、`dataset_snapshot.json`、`train.list`、`s1.yaml`、`s2.json`、`upstream-provenance.json`、日志、完整恢复检查点、GPT/SoVITS 推理权重和验证产物。
