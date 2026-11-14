"""Render an offline Studio walkthrough using synthetic labeled examples only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import AppConfig, EmotionConfig
    from emotion_models import analyze_sentences, atomic_json
    from emotion_training import train_classifier, write_dataset
    from ui.components.emotion_tab import EmotionTabWidget
    from ui.theme import build_frosted_stylesheet

    config = AppConfig(emotion=EmotionConfig(provider="local_classifier", labels=["喜悦", "悲伤", "中性"],
        epochs=45, learning_rate=.7, batch_size=4,
        classifier_path=str(output / "synthetic-model.npz"), dataset_path=str(output / "synthetic-labels.jsonl")))
    rows = []
    for label, sentence in (("喜悦", "开心快乐，觉得很幸福"), ("悲伤", "难过伤心，觉得很悲伤"), ("中性", "普通日常，描述一件事情")):
        rows.extend({"text": sentence + "，" + suffix + "。", "label": label} for suffix in ("今天", "刚才", "现在", "下午", "晚上", "周末"))
    write_dataset(config.emotion.dataset_path, rows)
    trained = train_classifier(config.emotion, rows)
    atomic_json(output / "synthetic-metrics.json", trained)
    predictions = analyze_sentences(config.emotion, ["今天开心快乐，觉得很幸福！", "现在难过伤心，觉得很悲伤。", "普通日常，描述一件事情。"])
    app = QApplication.instance() or QApplication([])
    widget = EmotionTabWidget(config)
    widget.setStyleSheet(build_frosted_stylesheet())
    widget.resize(1080, 1120)
    widget.show()
    widget._analysis_ready(predictions)
    widget._training_ready(trained)
    widget._chart_labels = config.emotion.labels
    widget._refresh_charts()
    files = []
    for page, name in ((0, "analysis.png"), (1, "annotation.png"), (2, "training.png")):
        widget.pages.setCurrentIndex(page)
        app.processEvents()
        path = output / name
        if not widget.grab().save(str(path)):
            raise RuntimeError(f"Cannot save GUI preview: {path}")
        files.append(str(path))
    widget.close()
    print(json.dumps({"synthetic_examples_only": True, "epochs": len(trained["history"]),
                      "predictions": [row.as_dict() for row in predictions], "screenshots": files}, ensure_ascii=False))


if __name__ == "__main__":
    main()
