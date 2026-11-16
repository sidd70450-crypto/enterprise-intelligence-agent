# ADR 0007: Evidence, report writing and verification

- Status: accepted
- Date: 2026-11-17

## Decision

Four stages follow retrieval. Each model call sees the least it needs, and
everything that can be checked mechanically is checked in code.

1. **extract_evidence.** The model reads the retrieved passages and returns
   quotes with the passage ID. Code keeps a quote only if its words appear in
   that passage in order (whitespace may differ), and stores the passage's exact
   text and page offsets for it. Invented quotes, paraphrases and quotes
   attributed to a passage that was not shown are discarded and counted.
2. **write_report.** The model sees only the verified quotes, each on one line
   with an ID, never the raw passages. Every claim must cite quote IDs. Claims
   citing nothing, or only IDs that do not exist, are dropped. With no verified
   evidence the stage does not call the model and the report states that the
   documents do not answer the question.
3. **verify.** One further call sees each claim with only the quotes it cites and
   returns supported, partially supported, unsupported or contradicted. A claim the
   verifier skips is treated as unsupported.
4. **assemble_report.** Supported and partially supported claims form the answer;
   the rest are listed as removed, with the verifier's reason. The report carries
   counts of quotes verified and rejected and claims checked and removed.

**Untrusted text.** Passages reach the model inside `<source>` tags, with any
`<source` or `</source` inside the text defused, under an instruction that the
contents are material to quote, never instructions. None of these stages has
tools. The Markdown export neutralises image syntax and raw HTML from documents
and model output, closing the exfiltration path in the threat model.

## Consequences and limits

- Verification is done by a model judging a model. It reduces unsupported claims;
  it does not remove them. Its error rate must be measured against hand labels
  (M11) before any claim about it is made.
- One verification call covers every claim, which is cheaper than one call per
  claim but lets claims share context. Per-claim calls are a variant to benchmark.
- Abstention depends on the extraction stage finding no relevant quotes. A model
  that quotes irrelevant passages will produce a report that the verifier has to
  catch.
- PDF export (design doc F10) is not implemented; Markdown is.
