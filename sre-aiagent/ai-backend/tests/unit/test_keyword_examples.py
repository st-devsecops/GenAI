import json
import re
from pathlib import Path


source = Path(__file__).resolve().parents[2] / "app" / "nodes" / "rag.py"
text = source.read_text()
start = text.index("def keyword_query(")
end = text.index("\n\ndef index_scope(", start)
namespace = {"re": re, "json": json, "RetrievalError": type("RetrievalError", (Exception,), {})}
exec(text[start:end], namespace)
keyword_query = namespace["keyword_query"]


def test_keyword_examples():
    assert keyword_query("show me the logs of scxml of 5hrs") == '("scxml")'
    assert keyword_query("show me 500 errors in apisix logs") == '("500" OR "error")'
    assert keyword_query("show me the errors of chatwoot nginx in past 2 hrs") == '("error" OR "chatwoot" OR "nginx")'
