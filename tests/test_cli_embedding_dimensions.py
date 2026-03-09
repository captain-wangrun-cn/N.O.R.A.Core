import cli


class _FakeResponse:
    def __init__(self, embedding):
        self._embedding = embedding

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"embedding": self._embedding}]}


def test_detect_embedding_dimensions_success(monkeypatch):
    monkeypatch.setattr(
        cli.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse([0.1, 0.2, 0.3, 0.4]),
    )

    dim = cli.detect_embedding_dimensions(
        base_url="https://api.example.com/v1",
        api_key="k",
        model="m",
    )

    assert dim == 4


def test_detect_embedding_dimensions_failure_returns_none(monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.requests, "post", _raise)

    dim = cli.detect_embedding_dimensions(
        base_url="https://api.example.com/v1",
        api_key="k",
        model="m",
    )

    assert dim is None
