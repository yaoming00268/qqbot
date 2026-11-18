"""Offline real data import, source isolation, live switching and training publication."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from config import AppConfig
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from ui.studio_api import StudioAPI
from voice_config import VoiceConfig
from voice_dataset import (atomic_json, file_hash, import_dataset, load_dataset, split_dataset, update_annotations)
from voice_live import applied_status, current_voice, reload_config
from voice_training import VoiceTraining


def audio(path, frequency=220, duration=4):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1,2,32000,0,"NONE","not compressed"))
        stream.writeframes(b"".join(struct.pack("<h",int(6000*math.sin(i*frequency*math.tau/32000)))
            for i in range(int(32000*duration))))


class VoiceLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.path = self.home / "config.yaml"
        self.config = AppConfig()
        self.config.save(str(self.path))
        self.client = SimpleNamespace(config_path=str(self.path), app_config=self.config.model_copy(deep=True))

    def test_backwards_timestamp_switch_is_applied_without_touching_identity(self):
        self.client.ai = SimpleNamespace(identity="same-session")
        reload_config(self.client)
        old_time = self.path.stat().st_mtime
        self.config.voice.character = "new-character"
        self.config.voice.enabled = True
        self.config.save(str(self.path))
        os.utime(self.path, (old_time-5,old_time-5))
        self.assertTrue(reload_config(self.client))
        self.assertEqual(self.client.app_config.voice.character,"new-character")
        self.assertEqual(self.client.ai.identity,"same-session")
        live = applied_status(self.home,self.config.voice,os.getpid())
        self.assertEqual(live["state"],"applied")
        self.assertEqual(applied_status(self.home,self.config.voice,os.getpid()+1)["state"],"pending")

    def test_old_broker_context_queries_saved_character_and_new_emotions(self):
        catalog = self.home / "catalog.json"
        atomic_json(catalog,{"version":1,"characters":{"old":{"emotions":{"neutral":{}}},"new":{"emotions":{"happy":{}}}}})
        self.config.voice.enabled=True
        self.config.voice.character="old"
        self.config.voice.catalog_path=str(catalog)
        self.config.save(str(self.path))
        broker=ConsoleBroker(self.home/"broker.sqlite3")
        ctx=broker.issue_context({"namespace":"fixture","scope":"private","sender_id":"123","transport":"onebot",
            "is_group":False,"voice_config_path":str(self.path),"voice_config":self.config.voice.model_dump(),
            "voice":{"enabled":True,"character":"old","emotions":["neutral"]}},self.home/"contexts")
        _,token=ConsoleBroker.from_context(ctx)
        self.config.voice.character="new"
        self.config.save(str(self.path))
        self.assertEqual(broker.context(token)["voice"]["character"],"new")
        self.assertEqual(broker.context(token)["voice"]["emotions"],["happy"])
        self.assertEqual(current_voice(self.client)["character"] if isinstance(current_voice(self.client),dict)
                         else current_voice(self.client).character,"new")
        execute("reply",ctx,text="hello",voice=True,emotion="happy")
        with self.assertRaises(ValueError):
            execute("reply",ctx,text="old-emotion",voice=True,emotion="neutral")

    def test_apply_merges_only_voice_change_and_rejects_stale_window(self):
        self.config.voice.singing.accompaniment_gain_db=-7
        self.config.bot.mention_probability=.37
        self.config.save(str(self.path))
        api=StudioAPI(self.home)
        initial=api.bootstrap()["data"]
        result=api.voice("apply",{"changes":{"character":"atri"},"revision":initial["revision"]})
        self.assertTrue(result["ok"],result)
        saved=AppConfig.load(str(self.path))
        self.assertEqual(saved.voice.character,"atri")
        self.assertEqual(saved.bot.mention_probability,.37)
        self.assertEqual(saved.voice.singing.accompaniment_gain_db,-7)
        stale=api.voice("apply",{"changes":{"character":"other"},"revision":initial["revision"]})
        self.assertFalse(stale["ok"])


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        self.root=self.home/"training"
        self.rows=[]
        for i in range(6):
            path=self.home/f"clip-{i}.wav"
            audio(path,frequency=220+i*37)
            self.rows.append({"audio":path.name,"text":f"テストです{i}。","speaker":"character","language":"JA",
                "emotion":"happy" if i%2 else "neutral","source_group":str(i//2),"reviewed":False,
                "annotation":{"method":"suggestion","needs_review":True}})
        self.annotation=self.home/"annotations.jsonl"
        self.annotation.write_text("\n".join(json.dumps(r,ensure_ascii=False) for r in self.rows),encoding="utf-8")

    def dataset(self):
        return import_dataset(self.root,self.annotation,dataset_id="fixture")

    def test_real_import_quality_hashes_and_explicit_uncertainty(self):
        result=self.dataset()
        self.assertEqual(result["summary"]["count"],6)
        self.assertEqual(result["summary"]["unreviewed"],6)
        self.assertEqual(result["summary"]["sources"],3)
        self.assertEqual(set(result["summary"]["emotions"]),{"neutral","happy"})
        self.assertEqual(load_dataset(self.root,"fixture",verify=True)["speaker"],"character")
        self.assertTrue(all(3.99<r["duration"]<4.01 for r in result["rows"]))
        (self.root/"datasets/fixture"/result["rows"][0]["audio"]).write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError,"修改"):
            load_dataset(self.root,"fixture",verify=True)

    def test_source_groups_and_identical_transcripts_do_not_cross_split(self):
        rows=[{**r,"id":str(i)} for i,r in enumerate(self.rows)]
        rows[2]["text"]=rows[0]["text"]
        split=split_dataset(rows,ratio=.2,seed=42)
        train=[r for r in rows if r["id"] in split["train"]]
        held=[r for r in rows if r["id"] in split["validation"]]
        self.assertFalse({r["source_group"] for r in train}&{r["source_group"] for r in held})
        self.assertFalse({r["text"] for r in train}&{r["text"] for r in held})
        self.assertEqual(split,split_dataset(rows,ratio=.2,seed=42))
        with self.assertRaisesRegex(ValueError,"独立音源"):
            split_dataset([{**r,"source_group":"one"} for r in rows])

    def test_review_revision_guard_and_no_silent_approval(self):
        result=self.dataset()
        path=self.root/"datasets/fixture/manifest.json"
        original=file_hash(path)
        row=result["rows"][0]
        reviewed=update_annotations(self.root,"fixture",[{"id":row["id"],"emotion":"sad","reviewed":True}],original)
        self.assertEqual(reviewed["summary"]["unreviewed"],5)
        self.assertEqual(reviewed["rows"][0]["emotion"],"sad")
        with self.assertRaisesRegex(ValueError,"更新"):
            update_annotations(self.root,"fixture",[{"id":row["id"],"reviewed":True}],original)

    def test_duplicate_and_invalid_audio_rejected_without_losing_valid_data(self):
        self.rows.append(dict(self.rows[0]))
        silent=self.home/"silent.wav"
        audio(silent,frequency=0)
        self.rows.append({**self.rows[1],"audio":silent.name})
        self.annotation.write_text("\n".join(json.dumps(r) for r in self.rows),encoding="utf-8")
        result=self.dataset()
        self.assertEqual(result["summary"]["count"],6)
        self.assertEqual(result["summary"]["rejected"],2)

    def test_training_requires_explicit_unreviewed_opt_in_and_keeps_holdout_out_of_worker(self):
        self.dataset()
        config=VoiceConfig(training={"directory":str(self.root)})
        manager=VoiceTraining(config,self.home)
        with self.assertRaisesRegex(ValueError,"未复核"):
            manager.run("fixture")
        def fake_worker(operation,job,**kwargs):
            self.assertEqual(operation,"train")
            saved=json.loads(Path(job["dataset_manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(len(saved["rows"]),6)
            self.assertTrue(all("emotion" in r for r in saved["rows"]))
            return {"version":"v2ProPlus","gpt_weights":"fixture.ckpt","sovits_weights":"fixture.pth"}
        with patch.object(manager,"worker",side_effect=fake_worker),patch("voice_engine.engine_status",return_value={"running":False}):
            result=manager.run("fixture",allow_unreviewed=True)
        self.assertEqual(result["state"],"completed")
        self.assertTrue(result["experimental"])
        self.assertFalse(set(result["split"]["train"])&set(result["split"]["validation"]))
        with self.assertRaisesRegex(ValueError,"权重"):
            manager.character(result["id"])

    def test_cancelled_job_preserves_source_data(self):
        self.dataset()
        stop=threading.Event()
        stop.set()
        manager=VoiceTraining(VoiceConfig(training={"directory":str(self.root)}),self.home)
        with self.assertRaises(Exception):
            manager.run("fixture",allow_unreviewed=True,stop=stop)
        self.assertEqual(load_dataset(self.root,"fixture",verify=True)["summary"]["count"],6)


if __name__=="__main__":
    unittest.main()
