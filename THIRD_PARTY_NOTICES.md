# 第三方组件与模型

本项目源码按 AGPL-3.0-only 发布。下列组件及模型各自适用其上游许可。

| 组件 | 许可与来源 |
| --- | --- |
| PyMuPDF 2.0（含 Layout）和 MuPDF | AGPL-3.0 或商业授权。本项目使用 AGPL 方式分发。对应源码可从 [PyMuPDF 2.0 的 PyPI 源码包](https://pypi.org/project/PyMuPDF/2.0/#files) 和 [PyMuPDF 构建说明](https://pymupdf.readthedocs.io/en/latest/installation.html) 获取。其构建脚本使用对应版本的 [MuPDF 源码](https://github.com/ArtifexSoftware/mupdf)。 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | Apache-2.0。用于本机图像识别，包含其发布的 OCR 模型。 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT。 |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) 与 [SentencePiece](https://github.com/google/sentencepiece) | Apache-2.0。 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | GPL-2.0-or-later，附带分发例外。 |

普通 ZIP 版自带本机 OCR 运行时。用于翻译的下列模型需另行获取，并保留上游许可和署名。

| 翻译模型 | 许可与来源 |
| --- | --- |
| OPUS-MT 英译中 | Apache-2.0。[Helsinki-NLP 原版](https://huggingface.co/Helsinki-NLP/opus-mt-en-zh)，[gaudi CTranslate2 版本](https://huggingface.co/gaudi/opus-mt-en-zh-ctranslate2)。 |
| NLLB-200 Distilled 600M | CC BY-NC 4.0。[Meta 原版](https://huggingface.co/facebook/nllb-200-distilled-600M)，[osa911 INT8 版本](https://huggingface.co/osa911/nllb-200-distilled-600M-ct2-int8)。适用非商业限制。 |

本项目的构建脚本及依赖版本清单位于对应 Release 标签的源码中。分发目录中的第三方许可文件由各组件提供。
