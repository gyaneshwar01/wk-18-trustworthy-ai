# Architecture

```mermaid
flowchart TD
    U[Untrusted user text] --> B[Type and length bounds]
    B --> N[Unicode normalization]
    N --> D[PII and secret span detection]
    D --> I[Injection and sensitive-topic rules]
    I -->|crisis| C[Fixed supportive response]
    I -->|injection or wrongdoing| R[Fixed refusal]
    I -->|medical / legal / financial| H[Fixed professional escalation]
    I -->|allow / redact| P[Sanitized prompt]
    F[(Approved public handbook facts)] --> G
    P --> G[Local extractive provider OR explicit remote model]
    G -->|provider error| X[Fixed safe fallback]
    G --> J[Untrusted candidate JSON]
    J --> S[Size bound + strict parser + exact schema]
    S --> O[Scan all response strings for PII / secrets]
    O --> V[Exact cited fact + answer consistency checks]
    F --> V
    S -->|invalid| X
    O -->|sensitive| X
    V -->|unsupported| X
    V -->|valid| A[Answer with source IDs]
    A --> E[Category-only audit metadata]
    C --> E
    R --> E
    H --> E
    X --> E
    E --> Z[CLI JSON response; optional metadata-only log]
```

1. **Before generation:** validate input bounds, normalize full-width/invisible characters, detect original normalized intent, and redact merged sensitive spans. Classifiers see the original normalized text so redaction cannot erase an injection cue. A blocked request never reaches the provider.
2. **Generation boundary:** the local baseline selects known facts using keywords; the optional adapter sends sanitized text and public facts to a configured endpoint. Treat both providers' outputs as untrusted. The provider has no filesystem, shell, account, or action tools.
3. **After generation:** parse only complete JSON, validate exact fields/types/enums, scan decoded strings for private information, and verify exact provenance and answer composition. Raw candidate content is discarded on any error.
4. **Response boundary:** only verified answers or trusted fixed controller messages leave the pipeline. Diagnostics are predefined reason/category names. Audit content excludes raw input and output, but the caller must protect the local log path.

The fact store is an explicitly trusted, curated input to grounding; grounding does not certify that these facts are true or current. Replacing the store with web retrieval would add an unimplemented trust boundary. References containing detectable PII still fail the output privacy check. The CLI does not execute returned text or render HTML.
