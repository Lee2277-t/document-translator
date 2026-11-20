from __future__ import annotations

import gc
import os
import re
from pathlib import Path

from .common import TranslationError, app_root

SOURCE_ROOT = app_root()
ENGLISH_DIR = SOURCE_ROOT / "models" / "opus-mt-en-zh-ct2"
MULTILINGUAL_DIR = SOURCE_ROOT / "models" / "nllb-200-zh-ct2"
#: NLLB 的目标语标记默认值（译入中文）。译入英文时由 ``NllbEngine`` 的实例
#: 参数覆盖——模型是同一个，只有前缀不同。
TARGET_LANG_TOKEN = "zho_Hans"
UNK_RE = re.compile(r"⁇+")

# 解码参数取自 argos-translate 与 CTranslate2 官方默认值：
# no_repeat_ngram_size 从源头掐断小模型的复读式跑偏，比事后清洗可靠；
# disable_unk + replace_unknowns 让生词回退成原词而不是 ⁇。
DECODE = {
    "no_repeat_ngram_size": 3,
    "repetition_penalty": 1.05,
    "disable_unk": True,
    "replace_unknowns": True,
    "max_decoding_length": 512,
    "batch_type": "tokens",
    "max_batch_size": 4096,
}
MARIAN_BEAM = 4
NLLB_BEAM = 4


def _threads() -> int:
    return max(1, min(8, os.cpu_count() or 4))


def _load_sp(model_path: Path):
    import sentencepiece as spm

    # SentencePiece 在中文路径下无法按文件名加载，改为直接读取字节
    return spm.SentencePieceProcessor(model_proto=model_path.read_bytes())


def _load_engine(model_dir: Path):
    import ctranslate2

    return ctranslate2.Translator(
        str(model_dir),
        device="cpu",
        compute_type="int8",
        inter_threads=1,
        intra_threads=_threads(),
    )


def _missing(model_dir: Path, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if not (model_dir / name).is_file()]


def _readout(item, sp, target_token: str = TARGET_LANG_TOKEN) -> tuple[str, float]:
    """取第一条假设并附上束搜索得分，得分后面用来判断这句是不是模型在瞎编。"""
    if not item.hypotheses:
        return "", 0.0
    tokens = [t for t in item.hypotheses[0] if t != target_token]
    score = item.scores[0] if item.scores else 0.0
    return UNK_RE.sub("", sp.decode(tokens)), float(score)


class MarianEngine:
    """Helsinki OPUS-MT 英译中模型，中文表达更地道，速度最快。"""

    label = "英文"

    def __init__(self, model_dir: Path = ENGLISH_DIR, log=lambda _m: None) -> None:
        self.model_dir = Path(model_dir)
        self.log = log
        self._engine = None
        self._source_sp = None
        self._target_sp = None

    def available(self) -> bool:
        return not _missing(self.model_dir, ("model.bin", "source.spm", "target.spm"))

    def load(self) -> None:
        if self._engine is not None:
            return
        if not self.available():
            raise TranslationError(
                "离线翻译模型不完整，请确认 models\\opus-mt-en-zh-ct2 文件夹存在且完整。"
            )
        self.log("正在加载本地翻译模型（首次约需数秒）…")
        try:
            self._source_sp = _load_sp(self.model_dir / "source.spm")
            self._target_sp = _load_sp(self.model_dir / "target.spm")
            self._engine = _load_engine(self.model_dir)
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(
                "本地翻译模型启动失败，请确认程序文件完整后重试。"
            ) from exc
        self.log("本地模型已就绪，处理过程不会联网。")

    def translate(self, texts: list[str], language: str = "") -> list[tuple[str, float]]:
        self.load()
        rows = []
        for text in texts:
            tokens = self._source_sp.encode(text, out_type=str)
            tokens.append("</s>")
            rows.append(tokens)
        results = self._engine.translate_batch(
            rows, beam_size=MARIAN_BEAM, return_scores=True, **DECODE
        )
        return [_readout(item, self._target_sp) for item in results]

    def close(self) -> None:
        self._engine = None
        self._source_sp = None
        self._target_sp = None
        gc.collect()


class NllbEngine:
    """Meta NLLB-200 多语种模型，覆盖一百多种语言，需要单独安装。

    ``target_token`` 决定译入哪种语言（``zho_Hans`` / ``eng_Latn``）。
    默认跟随 ``core.direction`` 的当前方向，也可以显式传死——精细模式的
    「英文段再译一遍做对比」要固定成中文，不能跟着全局方向漂。
    """

    label = "多语种"

    def __init__(
        self,
        model_dir: Path = MULTILINGUAL_DIR,
        log=lambda _m: None,
        target_token: str | None = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.log = log
        self._target_token = target_token
        self._engine = None
        self._sp = None

    @property
    def target_token(self) -> str:
        if self._target_token:
            return self._target_token
        from . import direction

        return direction.nllb_target()

    def available(self) -> bool:
        return not _missing(
            self.model_dir, ("model.bin", "sentencepiece.bpe.model", "shared_vocabulary.json")
        )

    def load(self) -> None:
        if self._engine is not None:
            return
        if not self.available():
            raise TranslationError(
                "多语种离线模型未安装，请运行「安装多语种离线模型.bat」后再翻译非英文文档。"
            )
        self.log("正在加载多语种离线模型（约需 10 秒，占用 800MB 内存）…")
        try:
            self._sp = _load_sp(self.model_dir / "sentencepiece.bpe.model")
            self._engine = _load_engine(self.model_dir)
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(
                "多语种离线模型启动失败，请确认 models\\nllb-200-zh-ct2 文件夹完整。"
            ) from exc
        self.log("多语种模型已就绪，处理过程不会联网。")

    def translate(self, texts: list[str], language: str) -> list[tuple[str, float]]:
        self.load()
        target = self.target_token
        rows = []
        for text in texts:
            tokens = [language] + self._sp.encode(text, out_type=str) + ["</s>"]
            rows.append(tokens)
        prefix = [[target]] * len(rows)
        results = self._engine.translate_batch(
            rows,
            target_prefix=prefix,
            beam_size=NLLB_BEAM,
            return_scores=True,
            **DECODE,
        )
        return [_readout(item, self._sp, target) for item in results]

    def close(self) -> None:
        self._engine = None
        self._sp = None
        gc.collect()
