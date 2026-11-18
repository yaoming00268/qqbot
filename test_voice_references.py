"""Speech annotation preservation and lossless training-reference promotion."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from voice_config import VoiceConfig
from voice_dataset import atomic_json, file_hash, import_dataset, update_annotations
from voice_emotions import EMOTIONS, presets
from voice_references import training_reference
from voice_text import completion_limit, japanese_tokens, prepare_chunks
from voice_training import VoiceTraining


def make_wav(path, duration=2.2, frequency=220):
    pcm=b"".join(struct.pack("<h",int(4000*math.sin(i*math.tau*frequency/32000)))
                 for i in range(round(duration*32000)))
    with wave.open(str(path), "wb") as output:
        output.setparams((1,2,32000,0,"NONE","not compressed"))
        output.writeframes(pcm)
    return pcm


class ReferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        self.source=self.home/"source.wav"
        self.pcm=make_wav(self.source)
        self.row={"id":"clip1","sha256":file_hash(self.source),"text":"どうしたのですか。",
                  "language":"ja","emotion":"surprised","reviewed":False,"delivery":"soft","intensity":"strong"}

    def test_short_reference_pads_only_copy_and_preserves_every_sample_and_annotation(self):
        before=self.source.read_bytes()
        reference=training_reference(self.source,self.row,self.home/"references","dataset")
        self.assertEqual(self.source.read_bytes(),before)
        with wave.open(reference["reference_audio"]) as stream:
            pcm=stream.readframes(stream.getnframes())
            self.assertAlmostEqual(stream.getnframes()/stream.getframerate(),3.1)
        self.assertEqual(pcm[:len(self.pcm)],self.pcm)
        self.assertEqual(pcm[len(self.pcm):],bytes(len(pcm)-len(self.pcm)))
        self.assertEqual(reference["prompt_text"],self.row["text"])
        self.assertFalse(reference["reviewed"])
        self.assertEqual(reference["delivery"],"soft")
        self.assertIn("惊讶",reference["description"])
        self.assertIn("待复核",reference["description"])
        self.source.unlink()
        self.assertTrue(Path(reference["reference_audio"]).is_file())

    def test_audio_hash_and_real_duration_are_checked_instead_of_manifest_duration(self):
        self.row["duration"]=4
        make_wav(self.source,duration=10.2)
        with self.assertRaisesRegex(ValueError,"变化"):
            training_reference(self.source,self.row,self.home/"references","dataset")
        self.row["sha256"]=file_hash(self.source)
        with self.assertRaisesRegex(ValueError,"重新切分"):
            training_reference(self.source,self.row,self.home/"references","dataset")

    def test_reference_promotion_checks_both_revisions_and_requires_explicit_replacement(self):
        root=self.home/"training"
        dataset=root/"datasets/dataset/manifest.json"
        clip=dataset.parent/"audio/source.wav"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(self.source.read_bytes())
        atomic_json(dataset,{"id":"dataset","state":"ready","rows":[{**self.row,"audio":"audio/source.wav"}]})
        catalog=self.home/"catalog.json"
        atomic_json(catalog,{"version":1,"characters":{"chieri":{"emotions":{"surprised":{"reference_audio":"old.wav"}}}}})
        config=VoiceConfig(catalog_path=str(catalog),training={"directory":str(root)})
        manager=VoiceTraining(config,self.home)
        kwargs={"dataset_revision":file_hash(dataset),"catalog_revision":file_hash(catalog)}
        before=catalog.read_bytes()
        with self.assertRaisesRegex(ValueError,"确认替换"):
            manager.use_reference("dataset","clip1","chieri",**kwargs)
        self.assertEqual(before,catalog.read_bytes())
        result=manager.use_reference("dataset","clip1","chieri",replace=True,**kwargs)
        self.assertNotEqual(result["revision"],kwargs["catalog_revision"])
        self.assertEqual(result["reference"]["training_dataset_id"],"dataset")
        with self.assertRaisesRegex(ValueError,"角色目录已更新"):
            manager.use_reference("dataset","clip1","chieri",replace=True,**kwargs)
        with self.assertRaisesRegex(ValueError,"数据集已更新"):
            manager.use_reference("dataset","clip1","chieri",dataset_revision="stale",catalog_revision=result["revision"])


class AnnotationAndCompletionTests(unittest.TestCase):
    def test_chinese_glossary_has_distinct_dimensions_and_many_emotions(self):
        self.assertGreaterEqual(len(EMOTIONS),40)
        for kind,choices in presets().items():
            self.assertEqual(len({r["value"] for r in choices}),len(choices),kind)
            self.assertTrue(all(r["description"] and any("\u4e00"<=c<="\u9fff" for c in r["label"]) for r in choices))

    def test_annotations_round_trip_all_dimensions_and_keep_custom_emotions(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)
            rows=[]
            for i in range(3):
                path=home/f"{i}.wav"
                make_wav(path,frequency=220+i*37)
                rows.append({"audio":path.name,"speaker":"character","language":"ja","text":"こんにちは。",
                             "emotion":"my-custom-tag","delivery":"whisper","intensity":"subtle","reviewed":False})
            annotation=home/"source.jsonl"
            annotation.write_text("\n".join(json.dumps(r) for r in rows),encoding="utf-8")
            data=import_dataset(home/"training",annotation,dataset_id="fixture")
            row=data["rows"][0]
            self.assertEqual(row["delivery"],"whisper")
            path=home/"training/datasets/fixture/manifest.json"
            result=update_annotations(home/"training","fixture",[{"id":row["id"],"intensity":"strong"}],file_hash(path))
            self.assertEqual(result["rows"][0]["emotion"],"my-custom-tag")
            self.assertEqual(result["rows"][0]["delivery"],"whisper")
            self.assertEqual(result["rows"][0]["intensity"],"strong")
            self.assertFalse(result["rows"][0]["reviewed"])
            before=path.read_bytes()
            with self.assertRaisesRegex(ValueError,"未知"):
                update_annotations(home/"training","fixture",[{"id":row["id"],"delivery":"not-valid"}],file_hash(path))
            self.assertEqual(before,path.read_bytes())

    def test_short_clause_synthesis_keeps_tail_emotions_and_language_spans(self):
        text="[ja]あたらしいほんをさがして、そのあといっしょにおちゃをのみながら、[emotion:happy]さいごまできいてくれてありがとう。[/ja][en]Every last word survives.[/en]"
        config=VoiceConfig(default_language="mixed")
        old=prepare_chunks(text,config,"neutral")
        chunks=prepare_chunks(text,config,"neutral",clause_limit=completion_limit({"experimental":True}))
        self.assertEqual("".join(c.text for c in chunks),"".join(c.text for c in old))
        self.assertTrue(all(len(c.text)<=32 for c in chunks))
        self.assertEqual([(c.emotion,c.language) for c in chunks][-1],("happy","en"))
        self.assertTrue(any(c.boundary=="clause" for c in chunks))
        self.assertIsNone(completion_limit({}))
        with self.assertRaises(ValueError):
            completion_limit({"synthesis":{"max_chunk_chars":0}})


if __name__=="__main__":
    unittest.main()
