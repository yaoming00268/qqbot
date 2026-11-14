"""Learning, checkpoint, SDK contract and Studio workflow checks without live models."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import json
import math
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from config import AppConfig, EmotionConfig
from emotion_models import LocalEmotionClassifier, analyze_sentences, list_lms_models, literal_report, normalized_text
from emotion_training import read_dataset, split_dataset, train_classifier, validate_samples, write_dataset


def samples():
    return [{"text": f"开心快乐幸福！{suffix}", "label": "喜悦"} for suffix in ("今天", "刚才", "现在", "周末", "朋友", "礼物")] + [
        {"text": f"难过伤心悲伤。{suffix}", "label": "悲伤"} for suffix in ("今天", "刚才", "现在", "周末", "朋友", "礼物")]


class LocalEmotionTrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = EmotionConfig(labels=["喜悦", "悲伤"], epochs=50, batch_size=4, learning_rate=.7,
                                 feature_dim=128, classifier_path=str(self.root / "emotion.npz"))

    def test_real_learning_heldout_metrics_and_portable_prediction(self):
        events = []
        result = train_classifier(self.cfg, samples(), on_progress=events.append)
        history = result["history"]
        self.assertEqual(len(history), self.cfg.epochs)
        self.assertLess(history[-1]["train_loss"], history[0]["train_loss"] * .3)
        self.assertGreaterEqual(history[-1]["val_accuracy"], .9)
        self.assertEqual(result["train_samples"] + result["validation_samples"], len(samples()))
        self.assertEqual(sum(map(sum, history[-1]["confusion_matrix"])), result["validation_samples"])
        self.assertEqual(sum(row["support"] for row in history[-1]["per_label"]), result["validation_samples"])
        model = LocalEmotionClassifier.load(self.cfg.classifier_path)
        predictions = model.predict(["今天开心快乐幸福", "今天难过伤心悲伤"])
        self.assertEqual([row.label for row in predictions], self.cfg.labels)
        for row in predictions:
            self.assertAlmostEqual(sum(row.probabilities.values()), 1)
        clean = validate_samples(samples(), self.cfg.labels)
        _, val = split_dataset(clean, self.cfg.labels, self.cfg.validation_fraction, self.cfg.seed)
        features = model.encoder.encode([row["text"] for row in clean])
        probabilities = model.probabilities(features[val])
        targets = [self.cfg.labels.index(clean[index]["label"]) for index in val]
        loss = -sum(math.log(probabilities[index, label]) for index, label in enumerate(targets)) / len(val)
        self.assertAlmostEqual(loss, min(row["val_loss"] for row in history))
        model.save(self.root / "copied.npz")
        copy = LocalEmotionClassifier.load(self.root / "copied.npz")
        np.testing.assert_allclose(copy.weights, model.weights)
        self.assertEqual(copy.training["best_epoch"], result["best_epoch"])

    def test_deduplication_conflicts_and_reproducible_split(self):
        rows = samples() + [dict(samples()[0])]
        clean = validate_samples(rows, self.cfg.labels)
        self.assertEqual(len(clean), len(samples()))
        first = split_dataset(clean, self.cfg.labels, .2, 42)
        second = split_dataset(clean, self.cfg.labels, .2, 42)
        for a, b in zip(first, second):
            np.testing.assert_array_equal(a, b)
        train_text = {normalized_text(clean[index]["text"]) for index in first[0]}
        val_text = {normalized_text(clean[index]["text"]) for index in first[1]}
        self.assertFalse(train_text & val_text)
        with self.assertRaisesRegex(ValueError, "冲突标签"):
            validate_samples(rows + [{"text": rows[0]["text"], "label": "悲伤"}], self.cfg.labels)
        with self.assertRaisesRegex(ValueError, "尚未选择"):
            validate_samples(rows + [{"text": "待标注", "label": ""}], self.cfg.labels)
        with self.assertRaisesRegex(ValueError, "至少需要 2"):
            split_dataset([samples()[0], samples()[-1]], self.cfg.labels, .2, 42)

    def test_no_validation_resume_and_incompatible_label_order(self):
        cfg = self.cfg.model_copy(update={"validation_fraction": 0, "epochs": 2})
        original = train_classifier(cfg, samples())
        before = LocalEmotionClassifier.load(cfg.classifier_path).weights.copy()
        self.assertIsNone(original["history"][-1]["val_loss"])
        self.assertEqual(original["history"][-1]["confusion_source"], "train")
        resumed = train_classifier(cfg, samples(), warm_start=cfg.classifier_path)
        self.assertLess(resumed["history"][-1]["train_loss"], original["history"][-1]["train_loss"])
        self.assertFalse(np.array_equal(before, LocalEmotionClassifier.load(cfg.classifier_path).weights))
        with self.assertRaisesRegex(ValueError, "继续训练"):
            train_classifier(cfg.model_copy(update={"labels": list(reversed(cfg.labels))}), samples(), warm_start=cfg.classifier_path)

    def test_stop_preserves_existing_file_or_best_completed_epoch(self):
        stop = threading.Event()
        stop.set()
        target = Path(self.cfg.classifier_path)
        target.write_bytes(b"existing model")
        result = train_classifier(self.cfg, samples(), stop=stop)
        self.assertTrue(result["cancelled"])
        self.assertIsNone(result["model_path"])
        self.assertEqual(target.read_bytes(), b"existing model")
        stop.clear()
        def progress(event):
            if event["type"] == "epoch":
                stop.set()
        result = train_classifier(self.cfg, samples(), stop=stop, on_progress=progress)
        self.assertTrue(result["cancelled"])
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(LocalEmotionClassifier.load(target).training["completed_epochs"], 1)

    def test_dataset_roundtrip_and_text_is_not_pseudolabeled(self):
        rows = [{"text": '你好，"朋友"\n开心', "label": "喜悦"}, {"text": "待标注", "label": ""}]
        for suffix in (".csv", ".jsonl", ".json"):
            path = self.root / ("labels" + suffix)
            write_dataset(path, rows)
            self.assertEqual(read_dataset(path, self.cfg.labels), rows)
        path = self.root / "input.txt"
        path.write_text("今天真开心。\n明天也会好！", encoding="utf-8")
        imported = read_dataset(path, self.cfg.labels)
        self.assertEqual([row["label"] for row in imported], ["", ""])
        path = self.root / "bad.csv"
        path.write_text("text,label\n句子,未知", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不在固定标签集"):
            read_dataset(path, self.cfg.labels)

    def test_invalid_checkpoint_and_config_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "不能重复"):
            EmotionConfig(labels=["喜悦", " 喜悦 "])
        with self.assertRaisesRegex(ValueError, "host:port"):
            EmotionConfig(lms_host="http://localhost:1234/v1")
        train_classifier(self.cfg.model_copy(update={"epochs": 1}), samples())
        with np.load(self.cfg.classifier_path, allow_pickle=False) as original:
            meta = original["metadata"].copy()
        np.savez(self.root / "bad.npz", metadata=meta, weights=np.zeros((2, 2)), bias=np.zeros(2))
        with self.assertRaisesRegex(ValueError, "维度"):
            LocalEmotionClassifier.load(self.root / "bad.npz")


class LMSContractTests(unittest.TestCase):
    def test_discover_and_structured_prediction_use_explicit_model_key(self):
        client = MagicMock()
        client.list_downloaded_models.return_value = [SimpleNamespace(model_key="local/llm", type="llm", path="a.gguf",
                                                                     info=SimpleNamespace(display_name="Local LLM"))]
        client.llm.model.return_value.respond.return_value = SimpleNamespace(parsed={"label": "喜悦", "explanation": "表达开心"})
        with patch("lmstudio.Client") as factory:
            factory.return_value.__enter__.return_value = client
            self.assertEqual(list_lms_models("localhost:1234")[0]["key"], "local/llm")
            cfg = EmotionConfig(provider="lmstudio", llm_model="local/llm", labels=["喜悦", "悲伤"])
            predictions = analyze_sentences(cfg, ["真开心"])
            client.llm.model.assert_called_once_with("local/llm")
            request = client.llm.model.return_value.respond.call_args.kwargs
            self.assertEqual(request["response_format"]["properties"]["label"]["enum"], cfg.labels)
            self.assertEqual(predictions[0].label, "喜悦")
            self.assertIsNone(predictions[0].confidence)
            self.assertTrue(factory.return_value.__exit__.called)
            client.llm.model.return_value.respond.return_value = SimpleNamespace(parsed={"label": "未知"})
            with self.assertRaisesRegex(ValueError, "约定的情感标签"):
                analyze_sentences(cfg, ["句子"])

    def test_embedding_training_and_reload_keep_encoder_identity(self):
        client = MagicMock()
        client.embedding.model.return_value.embed.side_effect = lambda texts: [[1., 0., 0.] if "开心" in text else [0., 1., 0.] for text in texts]
        with tempfile.TemporaryDirectory() as tmp, patch("lmstudio.Client") as factory:
            factory.return_value.__enter__.return_value = client
            cfg = EmotionConfig(provider="local_classifier", training_features="lmstudio_embedding", embedding_model="local/embed",
                                labels=["喜悦", "悲伤"], epochs=15, learning_rate=.8, classifier_path=str(Path(tmp) / "model.npz"))
            result = train_classifier(cfg, samples())
            model = LocalEmotionClassifier.load(cfg.classifier_path)
            self.assertEqual(model.encoder.dimension, 3)
            self.assertEqual(model.encoder.embedding_model, "local/embed")
            self.assertEqual([row.label for row in analyze_sentences(cfg, ["开心", "难过"])], cfg.labels)
            self.assertEqual(result["history"][-1]["val_accuracy"], 1.)
            client.embedding.model.assert_called_with("local/embed")

    def test_report_neutralizes_transport_markup(self):
        text = literal_report("<@123> [CQ:at,qq=123] [CQ:reply,id=x] <qqbot-at-everyone />")
        self.assertNotIn("<@123>", text)
        self.assertNotIn("[CQ:at,", text)


class EmotionStudioWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def wait_worker(self, widget):
        from PySide6.QtTest import QTest
        deadline = time.monotonic() + 15
        while widget._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.assertIsNone(widget._worker, widget.status.text())

    def test_human_labels_real_worker_charts_and_reload(self):
        from ui.components.emotion_tab import EmotionTabWidget
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(emotion=EmotionConfig(labels=["喜悦", "悲伤"], feature_dim=128, epochs=20, learning_rate=.7,
                                                 classifier_path=str(Path(tmp) / "model.npz"), dataset_path=str(Path(tmp) / "labels.jsonl")))
            widget = EmotionTabWidget(cfg)
            widget.resize(820, 740)
            widget.show()
            widget.sample_input.setPlainText("先别自动标注。")
            widget.add_samples()
            self.assertEqual(widget.sample_rows()[0]["label"], "")
            widget.start_training()
            self.assertIsNone(widget._worker)
            self.assertIn("尚未选择", widget.status.text())
            widget.samples.setRowCount(0)
            widget.append_samples(samples())
            widget.start_training()
            self.assertIsNotNone(widget._worker)
            self.assertFalse(widget.annotation_page.isEnabled())
            self.wait_worker(widget)
            self.assertEqual(len(widget.loss_chart.history), 20)
            self.assertTrue(widget.confusion_chart.matrix)
            self.assertEqual(widget.label_metrics.rowCount(), 2)
            self.assertIn("最佳分类器", widget.status.text())
            self.assertTrue(Path(cfg.emotion.dataset_path).is_file())
            widget.pages.setCurrentIndex(2)
            self.app.processEvents()
            self.assertFalse(widget.grab().isNull())
            widget.use_trained_model()
            widget.analysis_input.setPlainText("开心快乐幸福！难过伤心悲伤。")
            widget.start_analysis()
            self.wait_worker(widget)
            self.assertEqual(widget.predictions.rowCount(), 2)
            self.assertEqual(widget.predictions.item(0, 1).text(), "喜悦")
            self.assertEqual(widget.predictions.item(1, 1).text(), "悲伤")
            widget.history = []
            widget.inspect_classifier()
            self.wait_worker(widget)
            self.assertEqual(len(widget.history), 20)
            widget.label_editor.setText("悲伤, 喜悦")
            widget.restore_training_settings()
            self.wait_worker(widget)
            self.assertEqual(widget.collect().labels, ["喜悦", "悲伤"])
            self.assertTrue(widget.warm_start.isChecked())
            self.assertTrue(widget.prepare_close())
            widget.close()


class BertServiceAdapterTests(unittest.TestCase):
    def test_real_local_http_transport_and_probability_validation(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from emotion_bert_client import predict
        returned = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.headers.get("X-Emotion-Token") != "fixture-token":
                    self.send_error(401)
                    return
                rows = returned or [{"text": text, "label": "喜悦", "probabilities": {"喜悦": .8, "悲伤": .2}}
                                    for text in payload["texts"]]
                body = json.dumps({"predictions": rows}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            path = Path(tmp) / "inference.json"
            path.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}", "token": "fixture-token"}))
            try:
                cfg = EmotionConfig(provider="bert", bert_service_file=str(path))
                predictions = analyze_sentences(cfg, ["今天真开心"])
                self.assertEqual(predictions[0].label, "喜悦")
                self.assertEqual(predictions[0].confidence, .8)
                returned.append({"text": "今天真开心", "label": "喜悦", "probabilities": {"喜悦": 2}})
                with self.assertRaisesRegex(ValueError, "概率无效"):
                    predict(path, ["今天真开心"])
            finally:
                server.shutdown()
                worker.join(3)

    def test_remote_service_rejected_and_missing_service_has_actionable_message(self):
        from emotion_bert_client import predict
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inference.json"
            with self.assertRaisesRegex(ValueError, "加载 BERT"):
                predict(path, ["句子"])
            path.write_text(json.dumps({"url": "https://example.com", "token": "secret"}))
            with self.assertRaisesRegex(ValueError, "本机"):
                predict(path, ["句子"])
