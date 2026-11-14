"""Sentence annotation, LM Studio models and supervised local training in Studio."""
from __future__ import annotations

import json
from pathlib import Path
import threading

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from config import EmotionConfig
from emotion_models import LocalEmotionClassifier, analyze_sentences, atomic_json, list_lms_models, split_sentences
from emotion_training import TrainingCancelled, read_dataset, train_classifier, write_dataset
from ui.components.emotion_charts import ConfusionChart, MetricChart


class EmotionWorker(QThread):
    progress = Signal(dict)
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation, self.stop = operation, threading.Event()

    def check(self):
        if self.stop.is_set():
            raise TrainingCancelled()

    def run(self):
        try:
            result = self.operation(self.progress.emit, self.check, self.stop)
            if not (isinstance(result, dict) and "history" in result):
                self.check()
            self.result.emit(result)
        except TrainingCancelled:
            self.result.emit({"cancelled": True})
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class EmotionTabWidget(QWidget):
    save_requested = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config, self._worker = config, None
        self._close_pending = False
        self.history, self.training_result = [], {}
        self._labels = list(config.emotion.labels)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        header = QHBoxLayout()
        header.addWidget(QLabel("/emotion 分析方式"))
        self.provider = QComboBox()
        for label, value in (("主会话分析", "main"), ("BERT 独立分析服务", "bert"), ("LM Studio 语言模型", "lmstudio"), ("旧版本地分类器", "local_classifier")):
            self.provider.addItem(label, value)
        header.addWidget(self.provider, 1)
        self.save_button = QPushButton("保存配置")
        self.save_button.clicked.connect(self.save_requested.emit)
        header.addWidget(self.save_button)
        self.stop_button = QPushButton("停止当前任务")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.cancel_work)
        header.addWidget(self.stop_button)
        layout.addLayout(header)
        self.pages = QTabWidget()
        layout.addWidget(self.pages, 1)
        self._build_models()
        self._build_annotation()
        self._build_training()
        self.status = QLabel("选择本地模型进行分析，或先逐句标注再开始训练。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.load_config(config)

    def _build_models(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.model_settings = QGroupBox("本地模型")
        form = QFormLayout(self.model_settings)
        row = QHBoxLayout()
        self.bert_service_file = QLineEdit()
        row.addWidget(self.bert_service_file, 1)
        launch = QPushButton("打开 BERT 工作台")
        launch.clicked.connect(self.launch_bert_studio)
        row.addWidget(launch)
        form.addRow("BERT 服务描述文件", row)
        row = QHBoxLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText("127.0.0.1:1234")
        row.addWidget(self.host, 1)
        self.refresh_button = QPushButton("读取 LM Studio 模型")
        self.refresh_button.clicked.connect(self.refresh_models)
        row.addWidget(self.refresh_button)
        form.addRow("SDK 地址", row)
        self.llm_model, self.embedding_model = QComboBox(), QComboBox()
        self.llm_model.setEditable(True)
        self.embedding_model.setEditable(True)
        self.llm_model.setMinimumContentsLength(15)
        self.embedding_model.setMinimumContentsLength(15)
        self.llm_model.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.embedding_model.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        form.addRow("语言模型（用于分析）", self.llm_model)
        form.addRow("嵌入模型（用于训练特征）", self.embedding_model)
        row = QHBoxLayout()
        self.classifier_path = QLineEdit()
        row.addWidget(self.classifier_path, 1)
        browse = QPushButton("选择…")
        browse.clicked.connect(self.browse_classifier)
        row.addWidget(browse)
        inspect = QPushButton("读取分类器")
        inspect.clicked.connect(self.inspect_classifier)
        row.addWidget(inspect)
        form.addRow("本地分类器 / 训练输出", row)
        self.model_info = QLabel("LM Studio 读取已下载的语言/嵌入模型；本地分类器读取训练导出的 .npz 文件。")
        self.model_info.setWordWrap(True)
        self.model_info.setTextFormat(Qt.PlainText)
        form.addRow(self.model_info)
        layout.addWidget(self.model_settings)
        self.analysis_input = QPlainTextEdit()
        self.analysis_input.setPlaceholderText("输入待分析的句子；支持多句，结果逐句显示。")
        self.analysis_input.setMaximumHeight(100)
        layout.addWidget(self.analysis_input)
        self.analysis_button = QPushButton("分析输入文本")
        self.analysis_button.setObjectName("primaryBtn")
        self.analysis_button.clicked.connect(self.start_analysis)
        layout.addWidget(self.analysis_button)
        self.predictions = QTableWidget(0, 4)
        self.predictions.setHorizontalHeaderLabels(["句子", "标签", "概率", "依据 / 概率分布"])
        self.predictions.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.predictions.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.predictions.setColumnWidth(1, 75)
        self.predictions.setColumnWidth(2, 65)
        self.predictions.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.predictions, 1)
        hint = QLabel("语言模型返回固定标签和依据；概率仅在训练分类器可计算时显示。保存配置并重启机器人后，/emotion 使用所选分析方式。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.pages.addTab(page, "模型与分析")

    def _build_annotation(self):
        self.annotation_page = QWidget()
        layout = QVBoxLayout(self.annotation_page)
        row = QHBoxLayout()
        row.addWidget(QLabel("固定情感标签"))
        self.label_editor = QLineEdit()
        self.label_editor.setPlaceholderText("中性, 喜悦, 悲伤, 愤怒, 恐惧, 惊讶, 厌恶")
        row.addWidget(self.label_editor, 1)
        apply = QPushButton("应用标签集")
        apply.clicked.connect(self.apply_labels)
        row.addWidget(apply)
        layout.addLayout(row)
        hint = QLabel("每行一句话、一个人工标签。新增文本默认“未标注”，全部完成后才可训练。相同句子的冲突标签会阻止训练。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.sample_input = QPlainTextEdit()
        self.sample_input.setPlaceholderText("粘贴文本后按句拆分，再为每句话选择固定标签。")
        self.sample_input.setMaximumHeight(80)
        layout.addWidget(self.sample_input)
        row = QHBoxLayout()
        self.sample_label = QComboBox()
        row.addWidget(self.sample_label)
        add = QPushButton("拆句并添加")
        add.clicked.connect(self.add_samples)
        row.addWidget(add)
        delete = QPushButton("删除选中行")
        delete.clicked.connect(self.remove_samples)
        row.addWidget(delete)
        row.addStretch()
        self.sample_counts = QLabel("0 句")
        self.sample_counts.setWordWrap(True)
        row.addWidget(self.sample_counts, 1)
        layout.addLayout(row)
        self.samples = QTableWidget(0, 2)
        self.samples.setHorizontalHeaderLabels(["句子（双击可修改）", "人工情感标签"])
        self.samples.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.samples.setColumnWidth(1, 135)
        self.samples.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.samples.itemChanged.connect(self.update_counts)
        layout.addWidget(self.samples, 1)
        row = QHBoxLayout()
        self.dataset_path = QLineEdit()
        row.addWidget(self.dataset_path, 1)
        load = QPushButton("导入 / 追加…")
        load.clicked.connect(self.import_samples)
        row.addWidget(load)
        save = QPushButton("保存标注…")
        save.clicked.connect(self.export_samples)
        row.addWidget(save)
        layout.addLayout(row)
        layout.addWidget(QLabel("文件格式：CSV / JSONL / JSON 的 text、label 两列；TXT 按句拆分后人工标注。"))
        self.pages.addTab(self.annotation_page, "旧版逐句标注")

    def _build_training(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        self.training_settings = QGroupBox("训练参数")
        grid = QGridLayout(self.training_settings)
        self.feature_kind = QComboBox()
        self.feature_kind.addItem("字符特征 + 分类器（CPU）", "char_hash")
        self.feature_kind.addItem("LM Studio 嵌入 + 分类器", "lmstudio_embedding")
        self.feature_dim, self.epochs, self.batch_size, self.seed = QSpinBox(), QSpinBox(), QSpinBox(), QSpinBox()
        self.feature_dim.setRange(64, 65536)
        self.epochs.setRange(1, 10000)
        self.batch_size.setRange(1, 1024)
        self.seed.setRange(0, 2147483647)
        self.learning_rate, self.validation_fraction, self.l2 = QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()
        self.learning_rate.setDecimals(5)
        self.learning_rate.setRange(0.00001, 10)
        self.learning_rate.setSingleStep(.05)
        self.validation_fraction.setRange(0, .5)
        self.validation_fraction.setSingleStep(.05)
        self.l2.setDecimals(6)
        self.l2.setRange(0, 1)
        self.l2.setSingleStep(.0001)
        fields = [("训练特征", self.feature_kind), ("字符特征维度", self.feature_dim),
                  ("训练轮次", self.epochs), ("批大小", self.batch_size),
                  ("学习率", self.learning_rate), ("验证比例", self.validation_fraction),
                  ("L2 正则", self.l2), ("随机种子", self.seed)]
        for index, (label, widget) in enumerate(fields):
            row, col = divmod(index, 2)
            grid.addWidget(QLabel(label), row, col * 2)
            grid.addWidget(widget, row, col * 2 + 1)
        self.warm_start = QCheckBox("读取当前分类器权重并继续训练（需相同标签、特征及嵌入模型）")
        grid.addWidget(self.warm_start, 4, 0, 1, 3)
        restore = QPushButton("载入模型训练设置")
        restore.clicked.connect(self.restore_training_settings)
        grid.addWidget(restore, 4, 3)
        self.checkpoint_info = QLabel()
        self.checkpoint_info.setWordWrap(True)
        self.checkpoint_info.setTextFormat(Qt.PlainText)
        self.classifier_path.textChanged.connect(lambda value: self.checkpoint_info.setText("输出分类器：" + value))
        grid.addWidget(self.checkpoint_info, 5, 0, 1, 4)
        self.feature_kind.currentIndexChanged.connect(lambda: self.feature_dim.setEnabled(self.feature_kind.currentData() == "char_hash"))
        layout.addWidget(self.training_settings)
        hint = QLabel("本页为旧版轻量分类器。BERT / CUDA 训练请通过“模型与分析”中的“打开 BERT 工作台”使用独立 pywebview 项目。"
                      "LM Studio SDK 提供推理与嵌入，不提供大模型权重训练。这里训练的是情感分类器；嵌入模型保持冻结。"
                      "验证集独立留出；每种已标注情感至少需要 2 条不重复样本，验证比例为 0 时只显示训练指标。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.train_button = QPushButton("开始训练")
        self.train_button.setObjectName("primaryBtn")
        self.train_button.clicked.connect(self.start_training)
        row.addWidget(self.train_button)
        self.use_model_button = QPushButton("用于本地分析")
        self.use_model_button.clicked.connect(self.use_trained_model)
        row.addWidget(self.use_model_button)
        self.export_metrics_button = QPushButton("导出指标 JSON…")
        self.export_metrics_button.clicked.connect(self.export_metrics)
        row.addWidget(self.export_metrics_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        row.addWidget(self.progress, 1)
        layout.addLayout(row)
        self.metrics_summary = QLabel("等待训练。损失为交叉熵；准确率和宏 F1 由模型预测与人工标签对比计算。")
        self.metrics_summary.setWordWrap(True)
        layout.addWidget(self.metrics_summary)
        charts = QHBoxLayout()
        self.loss_chart = MetricChart("交叉熵损失", "train_loss", "val_loss")
        self.accuracy_chart = MetricChart("准确率", "train_accuracy", "val_accuracy")
        charts.addWidget(self.loss_chart)
        charts.addWidget(self.accuracy_chart)
        layout.addLayout(charts)
        self.confusion_chart = ConfusionChart()
        layout.addWidget(self.confusion_chart)
        self.label_metrics = QTableWidget(0, 5)
        self.label_metrics.setHorizontalHeaderLabels(["标签", "精确率", "召回率", "F1", "样本数"])
        self.label_metrics.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.label_metrics.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.label_metrics.setMinimumHeight(170)
        layout.addWidget(self.label_metrics)
        scroll.setWidget(page)
        self.pages.addTab(scroll, "旧版训练与图表")

    def collect(self):
        values = self.config.emotion.model_dump()
        values.update(provider=self.provider.currentData(), lms_host=self.host.text().strip(),
                      bert_service_file=self.bert_service_file.text().strip(),
                      llm_model=self.llm_model.currentText().strip(), embedding_model=self.embedding_model.currentText().strip(),
                      classifier_path=self.classifier_path.text().strip(), dataset_path=self.dataset_path.text().strip(),
                      labels=self._editor_labels(), training_features=self.feature_kind.currentData())
        for key in ("feature_dim", "epochs", "batch_size", "learning_rate", "validation_fraction", "l2", "seed"):
            values[key] = getattr(self, key).value()
        return EmotionConfig.model_validate(values)

    def load_config(self, config):
        self.config = config
        value = config.emotion
        self.provider.setCurrentIndex(self.provider.findData(value.provider))
        self.host.setText(value.lms_host)
        self.bert_service_file.setText(value.bert_service_file)
        self.llm_model.setEditText(value.llm_model)
        self.embedding_model.setEditText(value.embedding_model)
        self.classifier_path.setText(value.classifier_path)
        self.dataset_path.setText(value.dataset_path)
        self._labels = list(value.labels)
        self.label_editor.setText(", ".join(value.labels))
        self._fill_label_combo(self.sample_label)
        self.feature_kind.setCurrentIndex(self.feature_kind.findData(value.training_features))
        self.feature_dim.setEnabled(value.training_features == "char_hash")
        for key in ("feature_dim", "epochs", "batch_size", "learning_rate", "validation_fraction", "l2", "seed"):
            getattr(self, key).setValue(getattr(value, key))
        if not self.samples.rowCount() and Path(value.dataset_path).is_file():
            try:
                self.append_samples(read_dataset(value.dataset_path, self._labels))
            except Exception as error:
                self.status.setText("标注文件未载入：" + str(error))

    def _editor_labels(self):
        return [label.strip() for label in self.label_editor.text().replace("，", ",").split(",")]

    def _fill_label_combo(self, combo, selected=""):
        combo.clear()
        combo.addItem("未标注", "")
        for label in self._labels:
            combo.addItem(label, label)
        combo.setCurrentIndex(max(0, combo.findData(selected)))

    def apply_labels(self):
        try:
            value = self.collect()
            used = {row["label"] for row in self.sample_rows() if row["label"]}
            if used - set(value.labels):
                raise ValueError("现有样本仍使用被移除的标签，请先修改或删除对应样本。")
            self._labels = list(value.labels)
            self._fill_label_combo(self.sample_label, self.sample_label.currentData())
            for index in range(self.samples.rowCount()):
                combo = self.samples.cellWidget(index, 1)
                self._fill_label_combo(combo, combo.currentData())
            self.update_counts()
            self.status.setText("固定标签集已应用。")
            return True
        except Exception as error:
            self.status.setText(str(error))
            return False

    def append_samples(self, rows):
        for row in rows:
            index = self.samples.rowCount()
            self.samples.insertRow(index)
            self.samples.setItem(index, 0, QTableWidgetItem(row["text"]))
            combo = QComboBox()
            self._fill_label_combo(combo, row.get("label", ""))
            combo.currentIndexChanged.connect(self.update_counts)
            self.samples.setCellWidget(index, 1, combo)
        self.update_counts()

    def sample_rows(self):
        return [{"text": self.samples.item(index, 0).text() if self.samples.item(index, 0) else "",
                 "label": self.samples.cellWidget(index, 1).currentData() if self.samples.cellWidget(index, 1) else ""}
                for index in range(self.samples.rowCount())]

    def add_samples(self):
        rows = [{"text": text, "label": self.sample_label.currentData()} for text in split_sentences(self.sample_input.toPlainText())]
        self.append_samples(rows)
        self.sample_input.clear()

    def remove_samples(self):
        for index in sorted({item.row() for item in self.samples.selectedIndexes()}, reverse=True):
            self.samples.removeRow(index)
        self.update_counts()

    def update_counts(self, *_):
        from collections import Counter
        counts = Counter(row["label"] or "未标注" for row in self.sample_rows())
        self.sample_counts.setText(f"{self.samples.rowCount()} 句 · " + " / ".join(f"{label} {count}" for label, count in counts.items()))

    def import_samples(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入逐句标注", self.dataset_path.text(), "文本数据 (*.csv *.jsonl *.json *.txt)")
        if not path:
            return
        try:
            if not self.apply_labels():
                return
            rows = read_dataset(path, self._labels)
            self.append_samples(rows)
            self.status.setText(f"已追加 {len(rows)} 句，请完成未标注行。")
        except Exception as error:
            self.status.setText("导入失败：" + str(error))

    def export_samples(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存逐句标注", self.dataset_path.text(), "JSONL (*.jsonl);;CSV (*.csv);;JSON (*.json)")
        if not path:
            return
        try:
            write_dataset(path, self.sample_rows())
            self.dataset_path.setText(path)
            self.status.setText("标注数据已保存：" + path)
        except Exception as error:
            self.status.setText("保存失败：" + str(error))

    def browse_classifier(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择已训练分类器", self.classifier_path.text(), "情感分类器 (*.npz)")
        if path:
            self.classifier_path.setText(path)
            self.inspect_classifier()

    def launch_bert_studio(self):
        import subprocess
        from runtime_paths import application_home
        root = application_home() / "emotion_studio"
        python = root / ".venv/Scripts/python.exe"
        try:
            if not python.is_file():
                raise ValueError("请先运行 emotion_studio/setup.ps1 安装独立工作台的 CUDA 环境。")
            subprocess.Popen([str(python), "-m", "emotionlab", "gui"], cwd=root,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.status.setText("已打开独立 BERT 工作台。训练、标注和分析在工作台中进行；加载模型后选择 BERT 独立分析服务。")
        except Exception as error:
            self.status.setText(str(error))

    def refresh_models(self):
        try:
            config = self.collect()
            self._start_job(lambda notify, check, stop: list_lms_models(config.lms_host), self._models_ready,
                            "正在读取 LM Studio 已下载的模型…")
        except Exception as error:
            self.status.setText(str(error))

    def _models_ready(self, rows):
        if isinstance(rows, dict) and rows.get("cancelled"):
            return
        for combo, kind in ((self.llm_model, "llm"), (self.embedding_model, "embedding")):
            selected = combo.currentText()
            combo.clear()
            combo.addItems([row["key"] for row in rows if row["type"] == kind])
            if selected:
                combo.setEditText(selected)
        self.model_info.setText(f"已读取 {sum(row['type'] == 'llm' for row in rows)} 个语言模型、"
                                f"{sum(row['type'] == 'embedding' for row in rows)} 个嵌入模型。按需加载，不下载新模型。")
        self.model_info.setToolTip("\n".join(f"{row['type']} · {row['key']}\n{row['path']}" for row in rows))
        self.status.setText("模型列表已更新。" if rows else "LM Studio 中没有已下载模型，请先在 LM Studio 导入或下载模型。")

    def inspect_classifier(self):
        try:
            config = self.collect()
            self._start_job(lambda notify, check, stop: LocalEmotionClassifier.load(config.classifier_path, host=config.lms_host),
                            self._classifier_ready, "正在读取分类器及已保存的训练指标…")
        except Exception as error:
            self.status.setText(str(error))

    def restore_training_settings(self):
        try:
            path, host = self.classifier_path.text().strip(), self.host.text().strip()
            self._start_job(lambda notify, check, stop: LocalEmotionClassifier.load(path, host=host),
                            self._restore_settings_ready, "正在读取模型标签与训练设置…")
        except Exception as error:
            self.status.setText(str(error))

    def _restore_settings_ready(self, model):
        if isinstance(model, dict):
            return
        used = {row["label"] for row in self.sample_rows() if row["label"]}
        if used - set(model.labels):
            self.status.setText("现有标注含模型之外的标签，请先修改或移除对应样本。")
            return
        self.label_editor.setText(", ".join(model.labels))
        self.feature_kind.setCurrentIndex(self.feature_kind.findData(model.encoder.kind))
        if model.encoder.kind == "char_hash":
            self.feature_dim.setValue(model.encoder.dimension)
        self.embedding_model.setEditText(model.encoder.embedding_model)
        for key in ("batch_size", "learning_rate", "validation_fraction", "l2", "seed"):
            if key in model.training:
                getattr(self, key).setValue(model.training[key])
        self.apply_labels()
        self.warm_start.setChecked(True)
        self._classifier_ready(model)
        self.status.setText("已载入模型标签与特征，勾选继续训练。新增样本仍需逐句人工标注。")

    def _classifier_ready(self, model):
        if isinstance(model, dict):
            return
        self.model_info.setText(f"分类器：{len(model.labels)} 个标签 · {model.encoder.kind} · {model.encoder.dimension} 维\n"
                                + " / ".join(model.labels))
        self.history = list(model.training.get("history") or [])
        self.training_result = {**model.training, "model_path": self.classifier_path.text()}
        self._chart_labels = list(model.labels)
        self._refresh_charts()
        self.status.setText("分类器已读取，可选择“本地训练分类器”进行分析。继续训练须使用模型中保存的标签和特征设置。")

    def start_analysis(self):
        try:
            config = self.collect()
            if config.provider == "main":
                raise ValueError("请先将分析方式切换到 LM Studio 语言模型或本地训练分类器。")
            texts = split_sentences(self.analysis_input.toPlainText())
            if not texts:
                raise ValueError("请输入待分析的文本。")
            self._start_job(lambda notify, check, stop: analyze_sentences(config, texts, check=check,
                            progress=lambda done, total: notify({"type": "analysis", "done": done, "total": total})),
                            self._analysis_ready, f"正在逐句分析 {len(texts)} 句…")
        except Exception as error:
            self.status.setText(str(error))

    def _analysis_ready(self, predictions):
        if isinstance(predictions, dict):
            self.status.setText("分析已停止。")
            return
        self.predictions.setRowCount(len(predictions))
        for row, prediction in enumerate(predictions):
            details = prediction.explanation
            if prediction.probabilities:
                details = " / ".join(f"{label} {value:.1%}" for label, value in sorted(prediction.probabilities.items(), key=lambda item: -item[1]))
            for column, value in enumerate((prediction.text, prediction.label,
                                           f"{prediction.confidence:.1%}" if prediction.confidence is not None else "—", details)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.predictions.setItem(row, column, item)
        self.status.setText(f"已完成 {len(predictions)} 句分析。")

    def start_training(self):
        try:
            from emotion_training import split_dataset, validate_samples
            config = self.collect()
            rows = self.sample_rows()
            clean = validate_samples(rows, config.labels)
            split_dataset(clean, config.labels, config.validation_fraction, config.seed)
            if not config.classifier_path:
                raise ValueError("请在“模型与分析”填写分类器输出路径。")
            if config.training_features == "lmstudio_embedding" and not config.embedding_model:
                raise ValueError("请在“模型与分析”选择嵌入模型。")
            if not config.dataset_path:
                raise ValueError("请在“逐句标注”填写标注文件保存路径。")
            warm_start = config.classifier_path if self.warm_start.isChecked() else None
            write_dataset(config.dataset_path, rows)
            self.history, self.training_result = [], {}
            self._chart_labels = list(config.labels)
            self._refresh_charts()
            self.progress.setRange(0, config.epochs)
            self.progress.setValue(0)
            self._start_job(lambda notify, check, stop: train_classifier(config, rows, warm_start=warm_start,
                            stop=stop, on_progress=notify), self._training_ready, "正在准备训练特征…")
        except Exception as error:
            self.status.setText("训练未开始：" + str(error))

    def _training_ready(self, result):
        self.training_result = result
        self.history = result.get("history", self.history)
        self._refresh_charts()
        prefix = "训练已停止" if result.get("cancelled") else "训练完成"
        if result.get("model_path"):
            self.classifier_path.setText(result["model_path"])
            self.status.setText(f"{prefix}。已保存第 {result['best_epoch']} 轮最佳分类器：{result['model_path']}")
        else:
            self.status.setText(prefix + "，尚无完整训练轮次，未写入分类器。")

    def _on_progress(self, event):
        kind = event["type"]
        if kind == "epoch":
            self.history.append({key: value for key, value in event.items() if key != "type"})
            self.progress.setValue(event["epoch"])
            self._refresh_charts()
            self.status.setText(f"正在训练第 {event['epoch']} 轮…")
        elif kind in ("features", "analysis"):
            prefix = "提取训练特征" if kind == "features" else "分析句子"
            self.status.setText(f"{prefix}：{event['done']} / {event['total']}")
        elif kind == "split":
            text = f"训练 {event['train_samples']} 句 / 验证 {event['validation_samples']} 句，去重 {event['duplicates_removed']} 句。"
            if event["untrained_labels"]:
                text += " 未提供样本的标签：" + "、".join(event["untrained_labels"])
            self.metrics_summary.setText(text)

    def _refresh_charts(self):
        self.loss_chart.set_history(self.history)
        self.accuracy_chart.set_history(self.history)
        if not self.history:
            self.confusion_chart.set_matrix([], [], "")
            self.label_metrics.setRowCount(0)
            return
        latest = self.history[-1]
        labels = getattr(self, "_chart_labels", self._labels)
        source = "验证集" if latest["confusion_source"] == "validation" else "训练集（无验证集）"
        self.confusion_chart.set_matrix(labels, latest["confusion_matrix"], f"{source} · 第 {latest['epoch']} 轮")
        validation = (f" · 验证损失 {latest['val_loss']:.4f} · 验证准确率 {latest['val_accuracy']:.1%}"
                      if latest["val_loss"] is not None else " · 未划分验证集")
        self.metrics_summary.setText(f"第 {latest['epoch']} 轮 · 训练损失 {latest['train_loss']:.4f}"
                                     f" · 训练准确率 {latest['train_accuracy']:.1%}{validation} · {source}宏 F1 {latest['macro_f1']:.3f}")
        if self.training_result.get("best_epoch"):
            result = self.training_result
            detail = (f"\n保存第 {result['best_epoch']} 轮最佳权重 · 训练 {result['train_samples']} 句"
                      f" / 验证 {result['validation_samples']} 句 · 去重 {result['duplicates_removed']} 句")
            if result.get("untrained_labels"):
                detail += " · 无样本标签：" + "、".join(result["untrained_labels"])
            self.metrics_summary.setText(self.metrics_summary.text() + detail)
        self.label_metrics.setRowCount(len(latest["per_label"]))
        for row, item in enumerate(latest["per_label"]):
            for column, value in enumerate((item["label"], f"{item['precision']:.1%}", f"{item['recall']:.1%}",
                                           f"{item['f1']:.3f}", str(item["support"]))):
                self.label_metrics.setItem(row, column, QTableWidgetItem(value))

    def use_trained_model(self):
        self.provider.setCurrentIndex(self.provider.findData("local_classifier"))
        self.pages.setCurrentIndex(0)
        self.status.setText("已切换到本地分类器。可在此测试；保存配置并重启机器人后应用于 /emotion。")

    def export_metrics(self):
        if not self.history:
            self.status.setText("请先完成训练或读取带有训练指标的分类器。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出训练指标", self.classifier_path.text() + ".metrics.json", "JSON (*.json)")
        if path:
            try:
                atomic_json(path, {**self.training_result, "labels": getattr(self, "_chart_labels", self._labels), "history": self.history})
                self.status.setText("训练指标已导出：" + path)
            except Exception as error:
                self.status.setText("指标导出失败：" + str(error))

    def _start_job(self, operation, callback, message):
        if self._worker is not None:
            return
        worker = EmotionWorker(operation, self)
        self._worker = worker
        worker.result.connect(callback)
        worker.failed.connect(self._job_failed)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._worker_finished)
        self._set_busy(True)
        self.status.setText(message)
        worker.start()

    def _set_busy(self, busy):
        for widget in (self.model_settings, self.annotation_page, self.training_settings, self.provider,
                       self.save_button, self.analysis_button, self.train_button, self.use_model_button):
            widget.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    def _job_failed(self, message):
        self.status.setText("操作失败：" + message)

    def _worker_finished(self):
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker:
            worker.deleteLater()
        if self._close_pending:
            self._close_pending = False
            QTimer.singleShot(0, self.window().close)

    def cancel_work(self):
        if self._worker is not None:
            self._worker.stop.set()
            self.stop_button.setEnabled(False)
            self.status.setText("正在停止，将在当前模型请求或批次结束后生效；保留已完成轮次的最佳分类器。")

    def prepare_close(self):
        if self._worker is not None:
            self._close_pending = True
            self.cancel_work()
            return False
        return True

    def closeEvent(self, event):
        if not self.prepare_close():
            event.ignore()
            return
        super().closeEvent(event)
