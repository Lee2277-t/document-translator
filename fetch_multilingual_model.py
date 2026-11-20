# -*- coding: utf-8 -*-
"""下载保密模式用的多语种离线模型（只在安装时联网，翻译文档时不联网）。"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO = "osa911/nllb-200-distilled-600M-ct2-int8"
TARGET = Path(__file__).resolve().parent / "models" / "nllb-200-zh-ct2"
HOSTS = ("https://huggingface.co", "https://hf-mirror.com")
FILES = {
    "model.bin": (619704329, "ca3362e6e81906c0cf9c33bd6917674222c71d69617d0afb18507ce0b6c2e2e8"),
    "sentencepiece.bpe.model": (
        4852054,
        "14bb8dfb35c0ffdea7bc01e56cea38b9e3d5efcdcb9c251d6b40538e1aab555a",
    ),
    "shared_vocabulary.json": (
        5921176,
        "af53bfd0e6f726209e7325e45b87ab3b14e5856f7d42d7b9be91de3287c45267",
    ),
    "config.json": (223, "8f6496adfc930cbfecbe8281112197705c488fab47d34b4829b06d7f478909af"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified(name: str) -> bool:
    path = TARGET / name
    size, digest = FILES[name]
    return path.is_file() and path.stat().st_size == size and sha256(path) == digest


def download(name: str) -> bool:
    size, digest = FILES[name]
    temp = TARGET / (name + ".part")
    for host in HOSTS:
        url = f"{host}/{REPO}/resolve/main/{name}"
        try:
            started = time.time()
            done = 0
            percent = -1
            with urllib.request.urlopen(url, timeout=120) as response, temp.open("wb") as out:
                while True:
                    chunk = response.read(1 << 22)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    step = int(done * 20 / max(size, 1))
                    if step != percent:
                        percent = step
                        print(f"    {name}: {done * 100 // max(size, 1)}%", flush=True)
            if temp.stat().st_size != size or sha256(temp) != digest:
                print(f"    {name}: 校验不通过，换源重试")
                continue
            os.replace(temp, TARGET / name)
            print(f"    {name}: 完成（{(time.time() - started) / 60:.1f} 分钟）")
            return True
        except Exception as exc:
            print(f"    {name}: {host.split('//')[1]} 失败（{type(exc).__name__}）")
    temp.unlink(missing_ok=True)
    return False


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    missing = [name for name in FILES if not verified(name)]
    if not missing:
        print("多语种离线模型已安装，无需重复下载。")
        return 0
    total = sum(FILES[name][0] for name in missing) / 1e6
    print("即将下载多语种离线模型（NLLB-200，约 %.0f MB）。" % total)
    print("只有模型文件联网，文档内容仍然只在本机处理。")
    for name in missing:
        print(f"  下载 {name} …")
        if not download(name):
            print("下载失败：请确认本机可以访问 huggingface.co 或 hf-mirror.com 后重试。")
            return 1
    print("安装完成，现在可以翻译日文、韩文、俄文、法文、德文等非英文文档。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
