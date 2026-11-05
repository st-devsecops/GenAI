# Builds the investigation prompt containing the user's question and collected evidence.


def build_analyst_prompt(
    query: str,
    environment: str,
    service: str | None,
    start_time: str,
    end_time: str,
    evidence: str,
) -> str:
    """
    Build the prompt sent to the LLM for final SRE incident analysis.
    """

    service_text = service or "Not specified"

    return f"""
Investigate the following production SRE incident.

==================================================
USER QUESTION
==================================================

{query}

==================================================
ENVIRONMENT
==================================================

{environment}

==================================================
SERVICE
==================================================

{service_text}

==================================================
TIME RANGE
==================================================

{start_time}
    ->
{end_time}

==================================================
COLLECTED EVIDENCE
==================================================

{evidence}

==================================================
ANALYSIS INSTRUCTIONS
==================================================

Analyze only the evidence provided above.

For every important finding:

1. State what was observed.
2. Identify the source.
3. Identify the relevant timestamp.
4. Explain why the evidence is relevant.
5. Clearly separate observation from inference.

Correlate evidence across:

- Prometheus metrics
- Prometheus alerts
- Elasticsearch logs
- Elasticsearch errors
- Elasticsearch 5xx responses
- Elasticsearch timeout events
- Qdrant historical knowledge

Look for temporal correlation.

Identify the earliest relevant symptom.

Determine the most likely root cause only when the evidence
supports that conclusion.

Consider alternative explanations when appropriate.

Do not invent missing information.

If the evidence is insufficient, explicitly state:

"Root cause could not be determined from the available evidence."

==================================================
EXPECTED OUTPUT
==================================================

Return:

1. Summary
2. Findings
3. Root Cause
4. Confidence
5. Alternative Causes
6. Recommendations
7. Evidence References

Confidence must be one of:

HIGH
MEDIUM
LOW

Do not claim HIGH confidence unless multiple independent
pieces of evidence support the conclusion.

Recommendations must be based on the collected evidence.

Do not recommend destructive or irreversible production actions.
"""