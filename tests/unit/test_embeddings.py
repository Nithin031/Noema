import json

from noema.infrastructure.embeddings import chunk_text, embed_chunks, embed_texts, estimate_tokens


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def fake_embedder(request, timeout):
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == "nomic-embed-text"
    calls.append(body["input"])
    return FakeResponse({"embeddings": [[float(len(text))] * 4 for text in body["input"]]})


calls = []


def setup_function(_):
    calls.clear()


def test_short_text_is_one_chunk():
    assert chunk_text("hello world") == ["hello world"]
    assert chunk_text("   ") == [""]


def test_long_text_splits_within_budget_and_overlaps():
    paras = ["Paragraph {} about retrieval testing.".format(i) * 8 for i in range(12)]
    text = "\n\n".join(paras)
    chunks = chunk_text(text, max_tokens=64, overlap_tokens=16)
    assert len(chunks) > 1
    assert all(estimate_tokens(chunk) <= 64 for chunk in chunks)
    # Overlap: the start of each chunk repeats the tail of the previous one.
    assert chunks[1][:30] in chunks[0]


def test_embed_texts_batches_one_request_and_tracks():
    tracked = []
    vectors = embed_texts(["alpha", "beta beta"], urlopen_fn=fake_embedder,
                          tracker=lambda model, tokens: tracked.append((model, tokens)))
    assert len(calls) == 1 and len(calls[0]) == 2
    assert [len(vector) for vector in vectors] == [4, 4]
    assert tracked == [("nomic-embed-text", estimate_tokens("alpha") + estimate_tokens("beta beta"))]


def test_embed_chunks_end_to_end():
    text = "\n\n".join("Sentence {} matters.".format(i) for i in range(60))
    result = embed_chunks(text, max_tokens=64, urlopen_fn=fake_embedder)
    assert result["model"] == "nomic-embed-text"
    assert len(result["chunks"]) == len(result["embeddings"]) > 1
    assert all(estimate_tokens(chunk) <= 64 for chunk in result["chunks"])
