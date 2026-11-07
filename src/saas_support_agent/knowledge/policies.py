"""Versioned local policy documents. Scope and dates come from the trusted catalog."""

from datetime import date
import json
from pathlib import Path
import re

from saas_support_agent.knowledge.models import digest


def sections(path):
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        with path.open("rb") as stream:
            reader = PdfReader(stream)
            if reader.is_encrypted:
                raise ValueError(f"不能索引加密 PDF：{path.name}")
            for page, obj in enumerate(reader.pages, 1):
                body = obj.extract_text().strip()
                if not body:
                    raise ValueError(f"PDF 没有可解析文本：{path.name} 第 {page} 页")
                yield f"第 {page} 页", page, body
    elif path.suffix.lower() == ".md":
        matches = re.split(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*$", path.read_text(encoding="utf-8"))
        if matches[0].strip():
            yield "正文", None, matches[0].strip()
        for title, body in zip(matches[1::2], matches[2::2]):
            if body.strip():
                yield title.strip(), None, body.strip()
    else:
        raise ValueError(f"不支持的政策文件：{path.name}")


def split_text(body, limit=320):
    while body:
        if len(body) <= limit:
            yield body
            return
        cut = max(body.rfind(mark, 0, limit) for mark in ("\n", "。", "；", ". "))
        end = cut + 1 if cut >= limit // 2 else limit
        yield body[:end].strip()
        body = body[end:].strip()


def read_chunks(catalog):
    root = catalog.resolve().parent
    entries = json.loads(catalog.read_text(encoding="utf-8"))
    seen = set()
    ranges = {}
    chunks = []
    for doc in entries:
        key = doc["document_id"], doc["version"]
        if key in seen:
            raise ValueError(f"重复政策版本：{key}")
        seen.add(key)
        start = date.fromisoformat(doc["effective_from"])
        end = date.fromisoformat(doc["effective_until"]) if doc["effective_until"] else date.max
        if end <= start or (doc["shop_id"] and not doc["workspace_id"]):
            raise ValueError(f"无效的生效期或授权范围：{key}")
        scope = key[0], doc["workspace_id"], doc["shop_id"]
        if any(start < old_end and old_start < end for old_start, old_end in ranges.get(scope, [])):
            raise ValueError(f"同一范围的政策版本生效期重叠：{scope}")
        ranges.setdefault(scope, []).append((start, end))
        relative = Path(doc["file"])
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"政策文件不在目录内：{relative}")
        checksum = digest(path)
        count = 0
        for section, page, body in sections(path):
            for text in split_text(body):
                count += 1
                chunks.append({
                    **doc, "id": len(chunks) + 1, "section": section, "page": page, "text": text,
                    "checksum": checksum, "source_id": f"{key[0]}:{key[1]}:{count}",
                })
        if count == 0 or digest(path) != checksum:
            raise ValueError(f"政策为空或索引期间文件发生变化：{relative}")
    if not chunks:
        raise ValueError("政策目录为空")
    return chunks
