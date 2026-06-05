from memory.rag import RAGEngine


class _Embed:
    enabled = True

    def get_embedding(self, text):
        return [0.1, 0.2]


class _Vector:
    client = object()

    def __init__(self):
        self.upserts = []
        self.queries = []

    def upsert(self, text, vector, metadata=None):
        self.upserts.append({"text": text, "vector": vector, "metadata": metadata or {}})
        return True

    def query(self, vector, top_k, filter_criteria=None):
        self.queries.append(
            {"vector": vector, "top_k": top_k, "filter_criteria": filter_criteria or {}}
        )
        return []


def _rag():
    rag = RAGEngine.__new__(RAGEngine)
    rag.embed_client = _Embed()
    rag.vector_store = _Vector()
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
