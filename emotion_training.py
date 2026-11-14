"""Supervised sentence labels, real softmax SGD, held-out metrics and checkpoints."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import threading
import uuid

from emotion_models import FeatureEncoder, LocalEmotionClassifier, normalized_text, numpy, split_sentences


class TrainingCancelled(Exception):
    pass


def read_dataset(path, labels):
    """Import labeled files or unlabelled text for human annotation."""
    from emotion_analysis import read_text_attachment
    path = Path(path)
    text = read_text_attachment(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        rows = list(csv.DictReader(io.StringIO(text)))
    elif suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif suffix == ".json":
        rows = json.loads(text)
        if not isinstance(rows, list):
            raise ValueError("JSON 标注数据必须为包含 text 和 label 的对象数组。")
    elif suffix == ".txt":
        rows = [{"text": sentence, "label": ""} for sentence in split_sentences(text)]
    else:
        raise ValueError("支持 CSV、JSONL、JSON 标注文件，或待标注的 TXT 文本。")
    result = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条样本必须为对象。")
        sentence, label = row.get("text", row.get("sentence")), row.get("label", "")
        if not isinstance(sentence, str) or not sentence.strip() or not isinstance(label, str):
            raise ValueError(f"第 {index} 条样本必须包含非空 text 和文字 label。")
        label = label.strip()
        if label and label not in labels:
            raise ValueError(f"第 {index} 条样本的标签「{label}」不在固定标签集中。请先调整标签集。")
        result.append({"text": sentence.strip(), "label": label})
    return result


def write_dataset(path, rows):
    path = Path(path).expanduser().resolve()
    suffix = path.suffix.lower()
    if suffix == ".csv":
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=["text", "label"])
        writer.writeheader()
        writer.writerows(rows)
        content = "\ufeff" + output.getvalue()
    elif suffix == ".json":
        content = json.dumps(rows, ensure_ascii=False, indent=2)
    elif suffix == ".jsonl":
        content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    else:
        raise ValueError("标注数据请保存为 .jsonl、.csv 或 .json。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
    try:
        temporary.write_text(content, encoding="utf-8", newline="")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_samples(rows, labels):
    seen, clean = {}, []
    for index, row in enumerate(rows, 1):
        text, label = row.get("text"), row.get("label")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"第 {index} 行缺少句子。")
        if label not in labels:
            raise ValueError(f"第 {index} 行尚未选择固定情感标签。")
        key = normalized_text(text)
        if not key:
            raise ValueError(f"第 {index} 行句子为空。")
        if key in seen:
            if seen[key] != label:
                raise ValueError(f"同一句话存在冲突标签（第 {index} 行），请统一后再训练。")
            continue
        seen[key] = label
        clean.append({"text": text.strip(), "label": label})
    if len({row["label"] for row in clean}) < 2:
        raise ValueError("训练至少需要两种不同情感标签的有效样本。")
    return clean


def split_dataset(rows, labels, validation_fraction, seed):
    """Duplicates are removed before splitting to prevent train/validation leakage."""
    np = numpy()
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[row["label"]].append(index)
    rng = np.random.default_rng(seed)
    train, validation = [], []
    for label in labels:
        indices = groups[label]
        if not indices:
            continue
        if validation_fraction and len(indices) < 2:
            raise ValueError(f"标签「{label}」至少需要 2 条不重复样本才能划分验证集；也可将验证比例设为 0。")
        shuffled = rng.permutation(indices).tolist()
        count = max(1, min(len(indices) - 1, round(len(indices) * validation_fraction))) if validation_fraction else 0
        validation.extend(shuffled[:count])
        train.extend(shuffled[count:])
    return np.array(train, dtype=int), np.array(validation, dtype=int)


def classification_metrics(probabilities, targets, labels):
    np = numpy()
    predictions = probabilities.argmax(axis=1)
    matrix = np.zeros((len(labels), len(labels)), dtype=int)
    np.add.at(matrix, (targets, predictions), 1)
    details = []
    for index, label in enumerate(labels):
        tp = int(matrix[index, index])
        support, predicted = int(matrix[index].sum()), int(matrix[:, index].sum())
        precision, recall = tp / predicted if predicted else 0., tp / support if support else 0.
        details.append({"label": label, "precision": precision, "recall": recall, "support": support,
                        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.})
    # Unsupported labels remain visible in the matrix but do not dilute macro F1.
    observed = [row["f1"] for row in details if row["support"]]
    return {"loss": float(-np.log(np.maximum(probabilities[np.arange(len(targets)), targets], 1e-12)).mean()),
            "accuracy": float((predictions == targets).mean()),
            "macro_f1": float(np.mean(observed)), "confusion_matrix": matrix.tolist(), "per_label": details}


def train_classifier(config, rows, *, output_path=None, warm_start=None, stop=None, on_progress=lambda event: None):
    """Train a classification head, retaining the best completed epoch on stop.

    The embedding model stays frozen. Validation examples never enter gradients.
    All plots and histories are computed from actual model probabilities.
    """
    from config import EmotionConfig
    config = EmotionConfig.model_validate(config.model_dump())
    np = numpy()
    stop = stop if stop is not None else threading.Event()

    def check():
        if stop.is_set():
            raise TrainingCancelled()

    clean = validate_samples(rows, config.labels)
    train_indices, val_indices = split_dataset(clean, config.labels, config.validation_fraction, config.seed)
    counts = Counter(row["label"] for row in clean)
    history, best_model, best_loss = [], None, float("inf")
    summary = {"labels": list(config.labels), "train_samples": len(train_indices), "validation_samples": len(val_indices),
               "duplicates_removed": len(rows) - len(clean), "label_counts": dict(counts),
               "untrained_labels": [label for label in config.labels if not counts[label]],
               "best_epoch": None, "history": history, "cancelled": False, "model_path": None,
               "seed": config.seed, "learning_rate": config.learning_rate, "l2": config.l2,
               "batch_size": config.batch_size, "requested_epochs": config.epochs,
               "validation_fraction": config.validation_fraction,
               "started_at": datetime.now(timezone.utc).isoformat()}
    on_progress({"type": "split", **{key: value for key, value in summary.items() if key != "history"}})
    try:
        check()
        model = LocalEmotionClassifier.load(warm_start, host=config.lms_host) if warm_start else None
        if model and (model.labels != config.labels or model.encoder.kind != config.training_features
                      or model.encoder.kind == "char_hash" and model.encoder.dimension != config.feature_dim
                      or model.encoder.kind == "lmstudio_embedding" and model.encoder.embedding_model != config.embedding_model):
            raise ValueError("继续训练时，标签顺序、特征类型、字符维度及嵌入模型必须与现有分类器一致。")
        encoder = model.encoder if model else FeatureEncoder(config.training_features, config.feature_dim,
                                                             config.embedding_model, config.lms_host)
        original_dimension = encoder.dimension
        features = encoder.encode([row["text"] for row in clean], check=check,
                                  progress=lambda done, total: on_progress({"type": "features", "done": done, "total": total}))
        if model and encoder.dimension != original_dimension:
            raise ValueError("嵌入模型维度与继续训练的分类器不一致。")
        model = model or LocalEmotionClassifier(config.labels, encoder)
        targets = np.array([config.labels.index(row["label"]) for row in clean], dtype=int)
        rng = np.random.default_rng(config.seed)
        for epoch in range(1, config.epochs + 1):
            check()
            indices = rng.permutation(train_indices)
            for start in range(0, len(indices), config.batch_size):
                check()
                batch = indices[start:start + config.batch_size]
                x, y = features[batch], targets[batch]
                delta = model.probabilities(x)
                delta[np.arange(len(batch)), y] -= 1
                delta /= len(batch)
                model.weights -= config.learning_rate * (x.T @ delta + config.l2 * model.weights)
                model.bias -= config.learning_rate * delta.sum(axis=0)
                if not np.isfinite(model.weights).all() or not np.isfinite(model.bias).all():
                    raise ValueError("训练出现非有限权重，请降低学习率。")
            check()
            train = classification_metrics(model.probabilities(features[train_indices]), targets[train_indices], config.labels)
            validation = classification_metrics(model.probabilities(features[val_indices]), targets[val_indices], config.labels) if len(val_indices) else None
            record = {"epoch": epoch, "train_loss": train["loss"], "train_accuracy": train["accuracy"],
                      "val_loss": validation["loss"] if validation else None,
                      "val_accuracy": validation["accuracy"] if validation else None,
                      "macro_f1": (validation or train)["macro_f1"], "confusion_source": "validation" if validation else "train",
                      "confusion_matrix": (validation or train)["confusion_matrix"],
                      "per_label": (validation or train)["per_label"]}
            history.append(record)
            loss = (validation or train)["loss"]
            if loss < best_loss:
                best_loss = loss
                best_model = LocalEmotionClassifier(config.labels, encoder, model.weights.copy(), model.bias.copy())
                summary["best_epoch"] = epoch
            on_progress({"type": "epoch", **record})
    except TrainingCancelled:
        summary["cancelled"] = True
    if best_model is not None:
        summary["completed_epochs"] = len(history)
        summary["features"] = best_model.encoder.metadata()
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        best_model.training = {key: value for key, value in summary.items() if key != "model_path"}
        summary["model_path"] = best_model.save(output_path or config.classifier_path)
    return summary
