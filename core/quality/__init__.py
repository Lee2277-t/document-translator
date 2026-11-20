"""质量引擎：术语一致性 / 角色人设 / 上下文连贯 / 自动校改。

这一包是"同一个基座模型，质量能否再上一个台阶"的答案。四类质量损失
（术语漂移、人设漂移、人称施受错误、叙事断裂）各有对应的模块：

* ``segments``  —— 按场景边界分片，而不是固定行数（批次切断裂叙事）；
* ``assembler``  —— 分层上下文装配：命中术语 → 在场角色卡 → 前文摘要 → 原文；
* ``characters`` —— 结构化角色卡（自称/对称/语气基线/禁用表达）；
* ``verifier``   —— 规则层 + LLM 层双重校验，产出带严重度的问题清单；
* ``pipeline``   —— 把上面一切串成 Translator→Verifier→Corrector 循环。

设计铁律：**规则能判的不麻烦模型**。LLM 层只在规则层触发或抽样时调用，
校验开销不应超过翻译本身——本地推理每一秒都是用户的时间。
"""

from .assembler import ContextAssembler
from .characters import CharacterSheet
from .pipeline import Corrector, QualityPipeline
from .segments import Scene, SceneSegmenter, Segment
from .verifier import Issue, LLMVerifier, RuleVerifier

__all__ = [
    "CharacterSheet",
    "ContextAssembler",
    "Corrector",
    "Issue",
    "LLMVerifier",
    "QualityPipeline",
    "RuleVerifier",
    "Scene",
    "SceneSegmenter",
    "Segment",
]
