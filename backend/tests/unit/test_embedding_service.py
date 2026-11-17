import sys
import types
from pathlib import Path
from typing import ClassVar

import pytest
import torch

from app.core.config import Settings
from app.services import embedding as embedding_module
from app.services.embedding import EmbeddingModelError, EmbeddingService


class _FakeSentenceTransformer:
    instances: ClassVar[list["_FakeSentenceTransformer"]] = []
    values: ClassVar[torch.Tensor] = torch.tensor([[1.0, 0.0]], dtype=torch.float32)
    load_failure: ClassVar[Exception | None] = None
    encode_failure: ClassVar[Exception | None] = None

    def __init__(self, model_name_or_path: str, **kwargs: bool | str) -> None:
        failure = type(self).load_failure
        if failure is not None:
            raise failure
        self.model_name_or_path: str = model_name_or_path
        self.kwargs: dict[str, bool | str] = kwargs
        self.encode_calls: int = 0
        type(self).instances.append(self)

    def encode(
        self,
        inputs: list[str],
        *,
        normalize_embeddings: bool,
        device: str,
    ) -> torch.Tensor:
        assert inputs
        assert normalize_embeddings is True
        assert device
        self.encode_calls += 1
        failure = type(self).encode_failure
        if failure is not None:
            raise failure
        return self.values


def _settings(model_path: str | None, *, offline: bool = False) -> Settings:
    return Settings(
        # Hermetic unit tests: pin the provider so .env / environment
        # variables (e.g. EMBEDDING_PROVIDER=ollama on a deployed machine)
        # cannot redirect these tests onto the network path.
        EMBEDDING_PROVIDER="local",
        EMBEDDING_MODEL_PATH=model_path,
        EMBEDDING_MODEL_REVISION="5617a9f61b028005a4858fdac845db406aefb181",
        EMBEDDING_OFFLINE=offline,
        EMBEDDING_LOCAL_FILES_ONLY=False,
        EMBEDDING_DIMENSION=2,
    )


def _patch_model(monkeypatch: pytest.MonkeyPatch, config: Settings) -> None:
    _FakeSentenceTransformer.instances.clear()
    _FakeSentenceTransformer.load_failure = None
    _FakeSentenceTransformer.encode_failure = None
    monkeypatch.setattr(embedding_module, "settings", config)
    # EmbeddingService now imports SentenceTransformer lazily inside
    # load_model(), so the fake must be injected into sys.modules rather
    # than patched as a module attribute.
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer),
    )


def test_local_materialized_path_uses_pinned_offline_constructor_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _settings(str(tmp_path))
    _patch_model(monkeypatch, config)

    service = EmbeddingService()
    service.load_model()

    model = _FakeSentenceTransformer.instances[0]
    assert model.model_name_or_path == str(tmp_path)
    assert model.kwargs == {
        "device": "cpu",
        "trust_remote_code": False,
        "revision": config.EMBEDDING_MODEL_REVISION,
        "local_files_only": True,
    }


def test_configured_materialized_path_absence_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _settings(str(tmp_path / "missing"))
    _patch_model(monkeypatch, config)

    with pytest.raises(EmbeddingModelError) as error:
        EmbeddingService().load_model()

    assert str(error.value) == "embedding_model_unavailable"
    assert _FakeSentenceTransformer.instances == []


def test_offline_mode_without_materialized_path_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _settings(None, offline=True)
    _patch_model(monkeypatch, config)

    with pytest.raises(EmbeddingModelError) as error:
        EmbeddingService().load_model()

    assert str(error.value) == "embedding_model_unavailable"
    assert _FakeSentenceTransformer.instances == []


@pytest.mark.parametrize("failure", [OSError("missing model"), RuntimeError("model error")])
def test_preflight_wraps_model_load_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception,
) -> None:
    config = _settings(str(tmp_path))
    _patch_model(monkeypatch, config)
    _FakeSentenceTransformer.load_failure = failure

    with pytest.raises(EmbeddingModelError) as error:
        EmbeddingService().preflight()

    assert str(error.value) == "embedding_model_unavailable"


@pytest.mark.parametrize("failure", [OSError("encode error"), RuntimeError("encode error")])
def test_preflight_wraps_model_encode_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception,
) -> None:
    config = _settings(str(tmp_path))
    _patch_model(monkeypatch, config)
    _FakeSentenceTransformer.encode_failure = failure

    with pytest.raises(EmbeddingModelError) as error:
        EmbeddingService().preflight()

    assert str(error.value) == "embedding_model_unavailable"


@pytest.mark.parametrize(
    ("values", "expected_error"),
    [
        (torch.ones((1, 1)), "dimension"),
        (torch.tensor([[float("nan"), 0.0]]), "finite"),
        (torch.zeros((1, 2)), "normalization"),
    ],
)
def test_preflight_rejects_invalid_vectors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    values: torch.Tensor,
    expected_error: str,
) -> None:
    config = _settings(str(tmp_path))
    _FakeSentenceTransformer.values = values
    _patch_model(monkeypatch, config)

    with pytest.raises(EmbeddingModelError, match=expected_error):
        EmbeddingService().preflight()


def test_preflight_loads_and_encodes_once_with_a_normalized_vector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _settings(str(tmp_path))
    _FakeSentenceTransformer.values = torch.tensor([[1.0, 0.0]], dtype=torch.float32)
    _patch_model(monkeypatch, config)

    service = EmbeddingService()
    service.preflight()
    service.preflight()

    model = _FakeSentenceTransformer.instances[0]
    assert model.encode_calls == 1
