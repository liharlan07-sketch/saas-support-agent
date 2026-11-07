"""Fixed model snapshots; inference never fetches missing artifacts."""

from hashlib import file_digest
from pathlib import Path
from time import perf_counter


SNAPSHOTS = {
    "embedding": (
        "Qdrant/bge-small-zh-v1.5", "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59",
        "model_optimized.onnx", "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38",
    ),
    "rerank": (
        "onnx-community/bge-reranker-v2-m3-ONNX", "90213ffc6a8e6f051a6331269a0f5526cdd896f6",
        "onnx/model_int8.onnx", "912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d",
    ),
}


def digest(path):
    with Path(path).open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


class LocalModels:
    dimension = 512

    def __init__(self, root):
        from fastembed import TextEmbedding
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        start = perf_counter()
        for role, (_, _, filename, checksum) in SNAPSHOTS.items():
            if digest(Path(root) / role / filename) != checksum:
                raise ValueError(f"{role} 模型文件校验失败")
        self.embedding = TextEmbedding(
            "BAAI/bge-small-zh-v1.5", specific_model_path=str(Path(root) / "embedding"),
            threads=2, local_files_only=True,
        )
        self.reranker = TextCrossEncoder(
            "BAAI/bge-reranker-v2-m3-int8", specific_model_path=str(Path(root) / "rerank"),
            threads=2, local_files_only=True,
        )
        self.load_seconds = perf_counter() - start

    def documents(self, texts):
        return [vector.tolist() for vector in self.embedding.embed(texts)]

    def query(self, text):
        return next(self.embedding.query_embed(text)).tolist()

    def scores(self, text, candidates):
        return [float(score) for score in self.reranker.rerank(text, candidates)]


def prepare_models(root, *, ranged=False):
    from huggingface_hub import snapshot_download

    for role, (repo, revision, filename, checksum) in SNAPSHOTS.items():
        target = Path(root) / role
        weight = target / filename
        if weight.is_file() and digest(weight) == checksum and (target / "tokenizer.json").is_file():
            continue
        files = ["*.json", "*.txt", "*.model"]
        snapshot_download(repo, revision=revision, local_dir=target, token=False,
                          allow_patterns=files if ranged else [*files, filename])
        if ranged and (not weight.is_file() or digest(weight) != checksum):
            from saas_support_agent.knowledge.download import official_ranges

            official_ranges(repo, revision, filename, weight, checksum)
        if digest(weight) != checksum:
            raise ValueError(f"{role} 模型下载后的 SHA-256 不匹配")


if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser(description="从官方快照准备离线模型")
    parser.add_argument("--model-dir", type=Path, default=Path("runtime/rag/models"))
    parser.add_argument("--ranged", action="store_true", help="使用系统代理和官方 HTTPS 分段下载")
    args = parser.parse_args()
    prepare_models(args.model_dir, ranged=args.ranged)
