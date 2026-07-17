# SEC EDGAR Research Collector

PR36 provides an explicitly invoked, research-only SEC EDGAR collector for
`PLTR`, `LMT`, `RTX`, `GD`, `AVAV`, `XOM`, `OXY`, `SLB`, `COP`, and `VLO`.
It supports `8-K`, `8-K/A`, `4`, and `4/A`; it is not a scheduler or daemon.

## Setup and fair access

Add only these contact-identification values to the ignored `.env`:

```text
SEC_ORGANIZATION=
SEC_CONTACT_EMAIL=
```

EDGAR public data access requires no API key. The email is contact text in the
declared HTTP User-Agent only; the program does not access an inbox and needs no
email password, Gmail access, or email authentication. Missing contact values
cause live mode to fail before a request.

Requests are sequential with monotonic pacing. The default is 2 requests per
second and configuration above the hard 8-per-second maximum is rejected. The
client honors bounded `Retry-After` delays on HTTP 429, uses bounded exponential
backoff for retryable 5xx/transport failures, and stops the run immediately on
a potential fair-access HTTP 403. It never downloads concurrently. Exceeding
the SEC's published threshold may cause temporary traffic limiting or blocking;
see the [SEC fair-access guidance](https://www.sec.gov/about/developer-resources).

## Archive and durable state

The ignored local archive is:

```text
data_lake/context/sec_edgar/
  objects/<document_sha256>/original.<ext>
  objects/<document_sha256>/normalized.txt
  objects/<document_sha256>/sections/item_<number>_<section_sha256>.txt
  filings/<accession>.json
  form4/<accession>.json
  manifests/sec_filings.json
```

Objects and filing/Form 4 metadata are write-once. The mutable manifest is
atomically replaced. It stores complete safe reusable `VALID`/`ABSTAINED`
classification results and ledger state; it never stores section text, prompts,
raw provider responses, credentials, or tracebacks.

The SEC manifest owns persistent suppression across process restarts. Its key
uses accession, official document identity, item, complete-section and excerpt
hashes, extraction/prompt/model/schema versions, and relevant classification
configuration. PR35's existing LRU remains same-process duplicate protection.
Generated contract IDs are lineage only, not the durable identity.

## 8-K processing

The complete original filing and complete normalized relevant sections are
archived and hashed. Before constructing `ContextClassificationRequest`, PR36
creates a deterministic `HEAD_V1` excerpt no longer than PR35's configured
Gemini input limit. The archive records complete/excerpt character counts and
hashes, whether truncation occurred, and extraction/truncation versions. PR36
does not add chunking or multi-call result aggregation.

Gemini is invoked only with `--classify`, through the unchanged PR35 classifier.
Python retains ticker, CIK, accession, URL, timestamps, item, and hashes.
Provider or validation failures remain retryable and are not marked complete.

## Form 4 processing

Form 4 bypasses Gemini. The collector resolves and archives the official XML
document even when SEC discovery names an HTML-renderer path. It normalizes both
non-derivative and derivative transactions. Only non-derivative transaction
codes `P` and `S` are promoted as initial research events; derivative `P`/`S`,
`M`, `C`, `O`, `X`, grants, withholding, gifts, and other codes remain archived
without being treated as equivalent common-share purchases or sales.
The promoted event types are the venue-neutral `SEC_FORM4_PURCHASE` and
`SEC_FORM4_SALE`. [SEC defines P/S](https://www.sec.gov/edgar/searchedgar/ownershipformcodes.html)
as open-market or private transactions. PR36 does not infer transaction venue
from those codes or from footnotes.

An unresolved `4/A` is retained with
`aggregate_eligibility=AMENDMENT_UNRESOLVED` and excluded from default insider
purchase/sale aggregates. The collector does not guess an amended accession.
Missing or footnote-dependent facts remain unset.

## QuestDB ordering and safety

After `VALID` or `ABSTAINED`, the complete safe reusable result is saved locally
first. An explicitly requested QuestDB write follows. On failure, the existing
emergency JSONL fallback receives the safe classification-attempt row, while
the manifest remains QuestDB-pending. A later `--questdb` run retries only that
saved ledger row and never repeats Gemini for a completed result.

Nothing here enters `approved_risk_context`, alters `RiskDecision`, changes
features or sizing, calls Alpaca, or submits an order.

## Commands

Offline validation (no network):

```powershell
python scripts/check_sec_edgar.py
```

Manually gated, bounded SEC read smoke check (one actionable filing; no Gemini,
QuestDB, or broker action):

```powershell
python scripts/check_sec_edgar.py --live --ticker LMT --form 8-K --max-filings 1
```

The command uses the ignored local `.env`, performs SEC GET requests, and may
write the local immutable archive. It does not mutate SEC or any trading system.
In normal mode, `--max-filings` limits filings for which this run performs
missing archive, normalization, or classification work. Completed accessions
may be examined without consuming that actionable cap.

Discover only, without archive writes:

```powershell
python scripts/check_sec_edgar.py --live --ticker PLTR --form 4 --max-filings 1 --dry-run
```

Because dry-run performs no processing from which to determine actionability,
its `--max-filings` value limits raw discoveries instead.

Gemini classification and QuestDB writing are separate explicit opt-ins:

```powershell
python scripts/check_sec_edgar.py --live --ticker LMT --form 8-K --max-filings 1 --classify --questdb
```

PR37 remains responsible for the broader persistent research cache, as-of
selection, and shadow-policy evaluation. PR36's SEC checkpoint is source-local
paid-call suppression and ledger retry state, not that generic research cache.
