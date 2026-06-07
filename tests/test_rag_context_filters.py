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


# ---------------------------------------------------------------------------
# Phase 3: 跨平台共享作用域 (memory_scope_id)
# ---------------------------------------------------------------------------

SCOPE = "relationship:owner:default"


def test_retrieve_memory_scope_mode_filters_by_memory_scope_id():
    """传入 memory_scope_id 时，主查询只按 memory_scope_id 过滤，跨平台聚合。"""
    rag = _rag(query_results=[
        [
            {"text": "from telegram", "memory_scope_id": SCOPE, "score": 0.9},
            {"text": "from web", "memory_scope_id": SCOPE, "score": 0.8},
        ],
    ])

    results = rag.retrieve_memory(
        "hi",
        user_id="storage-1",
        top_k=2,
        platform="telegram",
        chat_id="chat-1",
        storage_id="storage-1",
        chat_type="private",
        memory_scope_id=SCOPE,
    )

    # 主查询只用 memory_scope_id，忽略 platform/chat_id（跨平台聚合）
    assert rag.vector_store.queries[0]["filter_criteria"] == {"memory_scope_id": SCOPE}
    assert [item["text"] for item in results] == ["from telegram", "from web"]


def test_retrieve_memory_scope_mode_falls_back_to_legacy_payload_without_scope():
    """scope 模式下结果不足时，回退按 user_id 检索，但只纳入未标记 scope 的旧记录。"""
    rag = _rag(query_results=[
        [],  # scope 查询无结果（旧库未回填向量库）
        [
            {"text": "legacy no-scope", "user_id": "storage-1", "score": 0.9},
            {"text": "other scope", "user_id": "storage-1", "memory_scope_id": "relationship:someone-else", "score": 0.9},
        ],
    ])

    results = rag.retrieve_memory(
        "hi",
        user_id="storage-1",
        top_k=5,
        memory_scope_id=SCOPE,
    )

    # 旧的无 scope 记录纳入；属于别的 scope 的记录排除
    assert [item["text"] for item in results] == ["legacy no-scope"]
    assert rag.vector_store.queries[0]["filter_criteria"] == {"memory_scope_id": SCOPE}
    assert rag.vector_store.queries[1]["filter_criteria"] == {"user_id": "storage-1"}


def test_get_context_string_threads_memory_scope_id():
    """get_context_string 应把 memory_scope_id 透传给 retrieve_memory。"""
    rag = _rag(query_results=[
        [{"text": "shared memory", "memory_scope_id": SCOPE, "role": "user", "score": 0.9}],
    ])

    ctx = rag.get_context_string(
        "hi",
        user_id="storage-1",
        top_k=2,
        memory_scope_id=SCOPE,
    )

    assert "shared memory" in ctx
    assert rag.vector_store.queries[0]["filter_criteria"] == {"memory_scope_id": SCOPE}
