"""FastAPI dependency for the object store (M02.01).

Routes declare ``store: ObjectStoreDep`` (or ``Depends(get_object_store)``). The real store is built
once per process from the ``S3_*`` environment. Tests replace it::

    app.dependency_overrides[get_object_store] = lambda: InMemoryObjectStore()
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from pydantic import ValidationError

from app.modules.storage.ports import ObjectStore, StorageUnavailable


@lru_cache(maxsize=1)
def _build_object_store() -> ObjectStore:
    # Imported here so importing the port/deps never pulls boto3 in.
    from app.infrastructure.object_storage.s3 import S3ObjectStore
    from app.infrastructure.object_storage.settings import ObjectStoreSettings

    try:
        settings = ObjectStoreSettings()
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
        raise StorageUnavailable(f"object store settings invalid: {', '.join(fields)}") from None
    return S3ObjectStore(settings)


def get_object_store() -> ObjectStore:
    """The process-wide object store (cached). Raises ``StorageUnavailable`` if unconfigured."""
    return _build_object_store()


def reset_object_store_cache() -> None:
    """Drop the cached store (tests that change the environment)."""
    _build_object_store.cache_clear()


ObjectStoreDep = Annotated[ObjectStore, Depends(get_object_store)]
