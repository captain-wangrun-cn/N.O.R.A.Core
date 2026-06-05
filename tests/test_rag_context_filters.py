from memory.rag import RAGEngine


class _Embed:
    enabled = True

    def get_embedding(self, text):
        return [0.1, 0.2]


class _Vector:
    client = object()

    def __init__(self, query_results=None):
        self.upserts = []
        self.queries = []
        self.query_results = query_results or []

    def upsert(self, text, vector, metadata=None):
        self.upserts.append({"text": text, "vector": vector, "metadata": metadata or {}})
        return True

    def query(self, vector, top_k, filter_criteria=None):
        self.queries.append(
            {"vector": vector, "top_k": top_k, "filter_criteria": filter_criteria or {}}
        )
        idx = len(self.queries) - 1
        if idx < len(self.query_results):
            return self.query_results[idx]
        return []


def _rag(query_results=None):
    rag = RAGEngine.__new__(RAGEngine)
    rag.embed_client = _Embed()
    rag.vector_store = _Vector(query_results=query_results)
    rag.enabled = True
    return rag


def test_add_memory_preserves_context_metadata():
    rag = _rag()

    assert rag.add_memory(
        "hello",
        user_id="storage-1",
        metadata={
            "role": "user",
            "platform": "telegram",
            "chat_id": "chat-1",
            "storage_id": "storage-1",
            "chat_type": "private",
        },
    )

    metadata = rag.vector_store.upserts[0]["metadata"]
    assert metadata["user_id"] == "storage-1"
    assert metadata["platform"] == "telegram"
    assert metadata["chat_id"] == "chat-1"
    assert metadata["storage_id"] == "storage-1"
    assert metadata["chat_type"] == "private"


def test_retrieve_memory_filters_by_context_dimensions():
    rag = _rag()

    rag.retrieve_memory(
        "hello",
        user_id="storage-1",
        platform="telegram",
        chat_id="chat-1",
        storage_id="storage-1",
        chat_type="private",
    )

    filters = rag.vector_store.queries[0]["filter_criteria"]
    assert filters == {
        "user_id": "storage-1",
        "platform": "telegram",
        "chat_id": "chat-1",
        "storage_id": "storage-1",
        "chat_type": "private",
    }


def test_retrieve_memory_falls_back_to_legacy_user_payload_without_context_fields():
    rag = _rag(query_results=[
        [],
        [
            {"text": "legacy ok", "user_id": "storage-1", "score": 0.9},
            {
                "text": "wrong platform",
                "user_id": "storage-1",
                "platform": "discord",
                "score": 0.9,
            },
        ],
    ])

    results = rag.retrieve_memory(
        "hello",
        user_id="storage-1",
        platform="telegram",
        chat_id="chat-1",
        storage_id="storage-1",
        chat_type="private",
    )

    assert [item["text"] for item in results] == ["legacy ok"]
    assert rag.vector_store.queries[0]["filter_criteria"]["platform"] == "telegram"
    assert rag.vector_store.queries[1]["filter_criteria"] == {"user_id": "storage-1"}
