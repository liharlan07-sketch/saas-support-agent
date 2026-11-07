"""可选的一次性准备工具：从官方锁定地址按段获取大文件。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from urllib.request import getproxies

from saas_support_agent.knowledge.models import digest


def official_ranges(repo, revision, filename, target, checksum):
    """沿用系统代理；逐段校验范围，最终校验整个模型再发布。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    proxy = getproxies().get("https")
    proxy_args = ["--proxy", proxy] if proxy else []
    url = f"https://huggingface.co/{repo}/resolve/{revision}/{filename}?download=true"

    def fetch(start, end, path):
        response = subprocess.run(
            ["curl", "-fsSL", *proxy_args, "--connect-timeout", "12", "--max-time", "65",
             "--speed-time", "20", "--speed-limit", "1024", "--max-filesize", str(max(1024**2, end - start + 1)),
             "--range", f"{start}-{end}", "--output", str(path),
             "--write-out", "%{http_code} %header{content-range}", f"{url}&part={start}"],
            text=True, capture_output=True, timeout=70,
        )
        match = re.fullmatch(r"206 bytes (\d+)-(\d+)/(\d+)", response.stdout.strip())
        if (response.returncode or not match or tuple(map(int, match.groups()[:2])) != (start, end)
                or path.stat().st_size != end - start + 1):
            raise OSError(f"官方模型文件片段下载失败（{start}–{end}）")
        return int(match[3])

    with TemporaryDirectory(dir=target.parent, prefix=".ranges-") as directory:
        staging = Path(directory)
        size = fetch(0, 0, staging / "probe")
        starts = range(0, size, 8 * 1024**2)

        def piece(start):
            path = staging / str(start)
            for attempt in range(3):
                try:
                    if fetch(start, min(size - 1, start + 8 * 1024**2 - 1), path) != size:
                        raise OSError("官方文件长度发生变化")
                    return path
                except (OSError, subprocess.TimeoutExpired):
                    if attempt == 2:
                        raise

        combined = staging / "model.onnx"
        with ThreadPoolExecutor(max_workers=6) as pool, combined.open("wb") as output:
            for path in pool.map(piece, starts):
                with path.open("rb") as stream:
                    shutil.copyfileobj(stream, output)
                path.unlink()
        if digest(combined) != checksum:
            raise ValueError("分段下载的模型 SHA-256 不匹配")
        combined.replace(target)
