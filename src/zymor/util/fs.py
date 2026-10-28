from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """入参：目标路径与 UTF-8 文本。出参：无。同目录临时文件 + fsync + 原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """入参：目标路径与字节。出参：无。原子写二进制文件。"""
    atomic_write_text(path, data.decode("utf-8"))


def read_text(path: Path) -> str:
    """入参：文件路径。出参：UTF-8 文本。读取文件并禁止 BOM 之外的编码猜测。"""
    return path.read_text(encoding="utf-8")


def sha256_hex(data: bytes | str) -> str:
    """入参：字节或文本。出参：sha256 十六进制摘要。用于 content_hash 与幂等判定。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def ensure_dir(path: Path) -> Path:
    """入参：目录路径。出参：同一路径。递归创建目录。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_size(path: Path) -> int:
    """入参：文件路径。出参：字节数。"""
    return path.stat().st_size
