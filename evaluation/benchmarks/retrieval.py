"""Retrieval benchmark: ``python -m evaluation.benchmarks.retrieval``.

Indexes a dataset's corpus into a throwaway organization, asks every question
in every search mode through the production ``Retriever``, scores the results,
and writes JSON and Markdown reports. The organization and everything in it is
deleted afterwards. Needs EIA_DATABASE_URL pointing at a migrated database.

Every number in a report comes from this run. The report records the dataset,
models, chunk size and git commit so a run can be reproduced.
"""

import argparse
import asyncio
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.documents import Collection, Document, DocumentChunk, DocumentStatus
from backend.database.models import Organization
from backend.database.session import create_engine, create_sessionmaker
from backend.ingestion.chunking import chunk_pages
from backend.ingestion.embedding import Embedder, build_embedder
from backend.ingestion.parsing import Page, normalize_text
from backend.retrieval.rerank import Reranker, build_reranker
from backend.retrieval.search import Retriever, SearchMode
from evaluation.metrics.retrieval import QuestionScores, aggregate, percentile, score_question

KS = (1, 3, 5, 10)


@dataclass(frozen=True)
class Variant:
    name: str
    mode: SearchMode
    rerank: bool


VARIANTS = [
    Variant("vector", "vector", False),
    Variant("keyword", "keyword", False),
    Variant("hybrid", "hybrid", False),
    Variant("hybrid + rerank", "hybrid", True),
]


def load_dataset(directory: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    corpus = {p.name: p.read_text() for p in sorted((directory / "corpus").glob("*"))}
    questions = [
        json.loads(line)
        for line in (directory / "questions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return corpus, questions


async def index_corpus(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    corpus: dict[str, str],
    chunk_chars: int,
) -> tuple[uuid.UUID, uuid.UUID, int]:
    org_id, collection_id = uuid.uuid4(), uuid.uuid4()
    total = 0
    async with sessions() as session:
        session.add(Organization(id=org_id, name="Benchmark", slug=f"benchmark-{org_id.hex[:12]}"))
        await session.flush()
        session.add(Collection(id=collection_id, org_id=org_id, name="benchmark"))
        await session.flush()
        for filename, content in corpus.items():
            document_id = uuid.uuid4()
            chunks = chunk_pages(
                [Page(None, normalize_text(content))],
                target_chars=chunk_chars,
                overlap_chars=min(200, chunk_chars // 4),
            )
            vectors = await asyncio.to_thread(embedder.embed_passages, [c.text for c in chunks])
            session.add(
                Document(
                    id=document_id, org_id=org_id, collection_id=collection_id,
                    filename=filename, kind="text", size_bytes=len(content.encode()),
                    sha256=uuid.uuid4().hex, status=DocumentStatus.READY,
                    chunk_count=len(chunks), embedding_model=embedder.name,
                )
            )  # fmt: skip
            await session.flush()
            await session.execute(
                insert(DocumentChunk),
                [
                    {
                        "id": uuid.uuid4(), "org_id": org_id, "collection_id": collection_id,
                        "document_id": document_id, "ordinal": c.ordinal, "page": c.page,
                        "char_start": c.char_start, "char_end": c.char_end, "text": c.text,
                        "embedding": v,
                    }
                    for c, v in zip(chunks, vectors, strict=True)
                ],
            )  # fmt: skip
            total += len(chunks)
        await session.commit()
    return org_id, collection_id, total


async def evaluate(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    reranker: Reranker | None,
    org_id: uuid.UUID,
    collection_id: uuid.UUID,
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for variant in VARIANTS:
        if variant.rerank and reranker is None:
            continue
        per_question: list[tuple[dict[str, Any], QuestionScores]] = []
        latencies: list[float] = []
        for question in questions:
            async with sessions() as session:
                started = time.perf_counter()
                found = await Retriever(session, embedder, reranker).search(
                    org_id=org_id, collection_ids=[collection_id], query=question["question"],
                    top_k=max(KS), mode=variant.mode, rerank=variant.rerank,
                )  # fmt: skip
                latencies.append((time.perf_counter() - started) * 1000)
            scores = score_question([h.text for h in found.hits], question["evidence"], KS)
            per_question.append((question, scores))
        by_type: dict[str, dict[str, float]] = {}
        for kind in sorted({q["type"] for q, _ in per_question}):
            by_type[kind] = aggregate([s for q, s in per_question if q["type"] == kind], KS)
        results[variant.name] = {
            "overall": aggregate([s for _, s in per_question], KS),
            "by_type": by_type,
            "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
            "misses_at_5": [q["id"] for q, s in per_question if s.recall[5] < 1.0],
        }
    return results


def git_commit() -> str:
    if sha := os.getenv("GITHUB_SHA"):
        return sha
    try:
        command = ["git", "rev-parse", "HEAD"]  # fixed command, no user input
        result = subprocess.run(command, capture_output=True, text=True, check=True)  # noqa: S603
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def render_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    type_counts = ", ".join(f"{k}: {v}" for k, v in meta["question_types"].items())
    lines = [
        f"# Retrieval benchmark: {meta['dataset']}",
        "",
        f"Generated {meta['generated_at']} from commit `{meta['commit'][:12]}` by "
        "`python -m evaluation.benchmarks.retrieval`. "
        "Every number below was measured in that run.",
        "",
        f"- Questions: {meta['questions']} ({type_counts})",
        f"- Corpus: {meta['documents']} documents",
        f"- Embedding model: `{meta['embedder']}`; reranker: `{meta['reranker']}`",
        "",
    ]
    for run in report["runs"]:
        lines += [
            f"## Chunk size {run['chunk_chars']} characters ({run['chunks']} chunks indexed)",
            "",
            "| Variant | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR | p50 ms | p95 ms |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for name, result in run["variants"].items():
            o, lat = result["overall"], result["latency_ms"]
            lines.append(
                f"| {name} | {o['recall@1']:.2f} | {o['recall@3']:.2f} | {o['recall@5']:.2f} | "
                f"{o['recall@10']:.2f} | {o['mrr']:.3f} | {lat['p50']:.0f} | {lat['p95']:.0f} |"
            )
        header = "| Variant | " + " | ".join(run["types"]) + " |"
        lines += [
            "",
            "Recall@3 by question type:",
            "",
            header,
            "| --- |" + " --- |" * len(run["types"]),
        ]
        for name, result in run["variants"].items():
            cells = [f"{result['by_type'][t]['recall@3']:.2f}" for t in run["types"]]
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        misses = "; ".join(
            f"{name}: {', '.join(r['misses_at_5']) or 'none'}"
            for name, r in run["variants"].items()
        )
        lines += ["", f"Questions missed at k=5: {misses}", ""]
    lines += [
        "## Limitations",
        "",
        f"- The dataset is small ({meta['questions']} questions over {meta['documents']} short "
        "documents) and was written by the system's author, so results are indicative only and "
        "may not transfer to other corpora. With so few chunks, Recall@10 is close to the whole "
        "corpus; Recall@1, Recall@3 and MRR are the informative columns.",
        "- Latency includes embedding the query on CPU and was measured on the machine that "
        "ran the benchmark, with the database on the same host.",
        "- Each configuration ran once; no confidence intervals are reported for this run.",
        "",
    ]
    return "\n".join(lines)


async def main(args: argparse.Namespace) -> None:
    # The benchmark issues no tokens and uses no Redis, but Settings requires both.
    os.environ.setdefault("EIA_JWT_SECRET", "benchmark-issues-no-tokens-" + "x" * 16)
    os.environ.setdefault("EIA_REDIS_URL", "redis://unused")
    settings = Settings(embedding_backend=args.embedder, reranker_backend=args.reranker)
    corpus, questions = load_dataset(args.dataset)
    embedder = build_embedder(settings)
    reranker = build_reranker(settings)
    engine = create_engine(settings.database_url)
    sessions = create_sessionmaker(engine)
    runs = []
    try:
        for chunk_chars in args.chunk_chars:
            org_id, collection_id, chunks = await index_corpus(
                sessions, embedder, corpus, chunk_chars
            )
            try:
                variants = await evaluate(
                    sessions, embedder, reranker, org_id, collection_id, questions
                )
            finally:
                async with sessions() as session:
                    await session.execute(delete(Organization).where(Organization.id == org_id))
                    await session.commit()
            runs.append({"chunk_chars": chunk_chars, "chunks": chunks, "variants": variants,
                         "types": sorted({q["type"] for q in questions})})  # fmt: skip
    finally:
        await engine.dispose()

    types: dict[str, int] = {}
    for q in questions:
        types[q["type"]] = types.get(q["type"], 0) + 1
    report = {
        "meta": {
            "dataset": args.dataset.name,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "commit": git_commit(), "questions": len(questions), "question_types": types,
            "documents": len(corpus), "embedder": embedder.name,
            "reranker": reranker.name if reranker else "none",
        },
        "runs": runs,
    }  # fmt: skip
    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"retrieval-{args.dataset.name}-{embedder.name.replace('/', '_')}"
    (args.out / f"{stem}.json").write_text(json.dumps(report, indent=2))
    markdown = render_markdown(report)
    (args.out / f"{stem}.md").write_text(markdown)
    print(markdown)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/datasets/retrieval-v1"))
    parser.add_argument("--out", type=Path, default=Path("evaluation/reports"))
    parser.add_argument("--embedder", choices=["fastembed", "hashing"], default="fastembed")
    parser.add_argument("--reranker", choices=["fastembed", "none"], default="fastembed")
    parser.add_argument("--chunk-chars", type=int, nargs="+", default=[400, 1500])
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
