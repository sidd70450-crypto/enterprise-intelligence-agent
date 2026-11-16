# ADR 0004: Document ingestion

- Status: accepted
- Date: 2026-11-16

## Context

Users upload PDF, DOCX and plain-text or Markdown files. Each must be parsed,
split into passages, embedded and indexed for keyword and vector search. Uploads
are untrusted: they may be corrupt, huge, disguised as another type, or built to
crash or exhaust a parser.

## Decisions

**Upload path.** `POST /collections/{id}/documents` refuses a missing or
oversized `Content-Length` before reading the body, streams the file to disk
while hashing it and enforcing the limit (default 25 MB), detects the type from
the bytes rather than the filename, and stores the file under
`<storage>/<org_id>/<document_id>`. The user's filename is kept for display only
and is never part of a path. A file already in the collection (same SHA-256)
returns 409 with the existing document's ID. The response is 202; processing
happens in the background.

**The document row is the job.** Instead of a separate queue, the worker
claims `queued` documents with `SELECT ... FOR UPDATE SKIP LOCKED`. The upload and
its job are one row written in one transaction, so a crash can never leave an
upload without a job or a job without an upload. The design doc proposed a Redis
queue; this is simpler and stronger. Each claim sets a fresh `lock_token`, and
every later write checks it. A worker that stops reporting progress for 10
minutes loses the job to another worker, and its late writes are discarded. A job
is attempted at most three times.

**Parsing is contained.** Parsers run in a child process with a CPU-time limit,
an address-space limit (default 1 GB), a wall-clock timeout (default 120 s) and
no `EIA_` environment variables, so the database URL and JWT secret are not
visible to it. A hang, memory blow-up or interpreter crash fails one document,
not the worker. DOCX archives are checked for entry count, total expanded size
and compression ratio before extraction (zip bombs). This contains failures; it
is not a security sandbox. A container boundary comes with code execution in
v0.3.

**Text and chunks.** Text is NFKC-normalised, control characters are removed,
hyphenated line breaks are rejoined and whitespace is collapsed. Chunks are about
1,500 characters with 200 characters of overlap, end at the best break (blank
line, sentence end, space) and never cross a page. Each chunk stores its page and
exact character offsets, and `text == page_text[start:end]` is tested, so a quote
can be traced to its page. A PDF averaging under 20 characters per page is
rejected as probably scanned; OCR is not supported.

**Embeddings: FastEmbed, not PyTorch.** The design doc planned a
sentence-transformers model on PyTorch. FastEmbed runs the same family of models
(default BAAI/bge-small-en-v1.5, 384 dimensions) through ONNX Runtime: about
30 MB of runtime instead of several hundred for CPU PyTorch, faster on CPU, and
nothing to train. PyTorch stays reserved for custom models, of which there are
none yet. The model is downloaded from Hugging Face on first start and cached in a
volume. A deterministic word-hashing embedder exists for tests; it has no
semantic understanding and must never be used to report retrieval quality.

**Index.** Chunks carry `org_id` and `collection_id` for filtering, a 384-dimension
`vector` column with an HNSW cosine index, and a generated `tsvector` column with a
GIN index.

## Consequences and known limits

- The worker polls once a second when idle. `LISTEN/NOTIFY` can replace polling
  if latency matters.
- Changing to a model of another dimension needs a migration and re-embedding.
- Files are on a local volume shared by API and worker, which limits deployment
  to one host until object storage is added.
- Expired or failed documents' files are kept until the document is deleted.
