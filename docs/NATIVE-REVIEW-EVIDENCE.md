# Source references for native review proposals

Unreleased source adds a small provenance connection to the existing Hermes
background review and skill evaluation path. It does not add a review worker,
recurrence queue, scheduler, automatic skill activation or new memory store.

The native request middleware captures references to structured tool failures
linked to actual assistant tool-call IDs. It uses the same participant scope
and inherited ContextVar mechanism as the existing background-review boundary.
User prose containing the word `error`, successful file contents and unlinked
tool results do not qualify. Guest, cron and subagent contexts are excluded.

The existing native pending skill payload retains at most sixteen references:
session/turn, tool call ID, tool name, failure classification and
`request_visible_result_sha256`. These hashes identify the result string in the
model request. They are not claims about canonical SessionDB bytes, an original
document, tool arguments or content before redaction/serialization. No raw
result or argument is copied into this metadata. Missing evidence is `null`.

Model-provided evidence fields are replaced with the captured references.
The existing evaluator carries those references into its native evaluation
ledger, including non-improvements. Its comparison, apply and rollback rules
remain unchanged. A link to a failure is evidence for investigating a proposal;
it does not prove the proposal fixes that failure or was used afterward.

The integration call belongs in the existing foreground
`capture_review_parent` request middleware, after participant resolution:

```python
from .review_evidence import capture
capture(scope, kwargs.get('request'))
```

Its existing early return for background review must remain: Hermes inherits
the foreground references into the review fork. This also preserves references
when native routing reduces the review's input to a conversation digest.

Focused packaged tests cover linked Chat Completions and Responses results,
missing authority/evidence, user-content rejection, independent review-context
snapshots, duplicate result references, model-provenance replacement, and the
unchanged native proposal/evaluation/recovery path. Controlled oracle outcomes
qualify those contracts, not a measured model improvement.

The motivating operational class is unsupported regular-expression features
in native file search. A stock native file/terminal experiment reproduced
lookahead, lookbehind and backreference failures; explicit `rg --pcre2` recovered
the intended matches, while simple and no-match controls already worked.
That establishes a supported recovery option. It does not establish learned
adoption, task impossibility without a correction, or an automatic skill gain.
No tool implementation or regular-expression meaning is changed by this work.
