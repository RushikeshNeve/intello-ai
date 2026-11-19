"""Load and validate an evaluation corpus (``manifest.json`` + ``files/``) and a golden dataset.

The seed corpus and a user-supplied corpus directory share this layout::

    <corpus>/manifest.json     orgs, workspaces, personas, documents (versions, visibility, deleted)
    <corpus>/files/<file>      the raw bytes of every version
    <dataset>.jsonl            one item per line, validated against datasets/schema.json

Pure standard library (a small JSON-Schema subset validator), so it runs anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_CORPUS = HERE / "corpus"
DEFAULT_DATASET = HERE / "datasets" / "golden.jsonl"
SCHEMA_PATH = HERE / "datasets" / "schema.json"
SUPPORTED_TYPES = frozenset({"application/pdf", "text/plain", "text/markdown", "text/html"})
KINDS = (
    "answerable",
    "unanswerable",
    "permission",
    "injection",
    "updated-fact",
    "deleted-doc",
    "multi-hop",
)


class DatasetError(ValueError):
    """The corpus or dataset is malformed (message names the offending item)."""


@dataclass(frozen=True)
class VersionSpec:
    version: int
    file: str


@dataclass(frozen=True)
class DocumentSpec:
    id: str
    title: str
    workspace: str
    content_type: str
    visibility: str
    deleted: bool
    hostile: bool
    versions: tuple[VersionSpec, ...]

    @property
    def current(self) -> VersionSpec:
        return max(self.versions, key=lambda v: v.version)


@dataclass(frozen=True)
class Persona:
    id: str
    org: str
    workspaces: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    root: Path
    name: str
    orgs: tuple[str, ...]
    workspaces: dict[str, str]  # workspace id -> org id
    personas: dict[str, Persona]
    documents: tuple[DocumentSpec, ...]

    def document(self, doc_id: str) -> DocumentSpec:
        for doc in self.documents:
            if doc.id == doc_id:
                return doc
        raise KeyError(doc_id)

    def read(self, version: VersionSpec) -> bytes:
        return (self.root / "files" / version.file).read_bytes()


@dataclass(frozen=True)
class Item:
    id: str
    question: str
    kind: str
    persona: str
    expected_docs: tuple[str, ...]
    expected_facts: tuple[str, ...]
    forbidden: tuple[str, ...]
    expect_abstain: bool
    notes: str = ""


# ------------------------------------------------------------------------------- schema


def validate_schema(value: Any, schema: dict[str, Any], where: str = "$") -> list[str]:
    """Errors of ``value`` against a JSON-Schema subset (type, enum, required, properties, items,
    additionalProperties, minLength, maxLength). Empty list means valid."""
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
        return errors
    expected = schema.get("type")
    checks: dict[str, type | tuple[type, ...]] = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
    }
    if expected is not None:
        python_type = checks[expected]
        if not isinstance(value, python_type) or (
            expected != "boolean" and isinstance(value, bool)
        ):
            return [f"{where}: expected {expected}, got {type(value).__name__}"]
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{where}: shorter than {schema['minLength']}")
        if len(value) > schema.get("maxLength", 10**9):
            errors.append(f"{where}: longer than {schema['maxLength']}")
    if isinstance(value, list) and "items" in schema:
        for i, element in enumerate(value):
            errors += validate_schema(element, schema["items"], f"{where}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}: missing required field {name!r}")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    errors.append(f"{where}: unexpected field {name!r}")
        for name, sub in props.items():
            if name in value:
                errors += validate_schema(value[name], sub, f"{where}.{name}")
    return errors


# ------------------------------------------------------------------------------ manifest


def load_manifest(corpus_dir: Path = DEFAULT_CORPUS) -> Manifest:
    path = corpus_dir / "manifest.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot read {path}: {exc}") from exc
    if raw.get("schema_version") != 1:
        raise DatasetError("manifest.schema_version must be 1")
    orgs = tuple(o["id"] for o in raw["orgs"])
    workspaces = {w["id"]: w["org"] for w in raw["workspaces"]}
    for ws, org in workspaces.items():
        if org not in orgs:
            raise DatasetError(f"workspace {ws!r} references unknown org {org!r}")
    personas: dict[str, Persona] = {}
    for pid, p in raw["personas"].items():
        if p["org"] not in orgs:
            raise DatasetError(f"persona {pid!r}: unknown org {p['org']!r}")
        for ws in p["workspaces"]:
            if workspaces.get(ws) != p["org"]:
                raise DatasetError(f"persona {pid!r}: workspace {ws!r} is not in org {p['org']!r}")
        personas[pid] = Persona(pid, p["org"], tuple(p["workspaces"]))
    docs: list[DocumentSpec] = []
    seen: set[str] = set()
    for d in raw["documents"]:
        if d["id"] in seen:
            raise DatasetError(f"duplicate document id {d['id']!r}")
        seen.add(d["id"])
        if d["workspace"] not in workspaces:
            raise DatasetError(f"document {d['id']!r}: unknown workspace {d['workspace']!r}")
        if d["content_type"] not in SUPPORTED_TYPES:
            raise DatasetError(f"document {d['id']!r}: unsupported type {d['content_type']!r}")
        versions = tuple(VersionSpec(int(v["version"]), v["file"]) for v in d["versions"])
        if sorted(v.version for v in versions) != list(range(1, len(versions) + 1)):
            raise DatasetError(f"document {d['id']!r}: versions must be 1..n")
        for v in versions:
            if not (corpus_dir / "files" / v.file).is_file():
                raise DatasetError(f"document {d['id']!r}: missing file {v.file!r}")
        docs.append(
            DocumentSpec(
                id=d["id"],
                title=d["title"],
                workspace=d["workspace"],
                content_type=d["content_type"],
                visibility=d.get("visibility", "workspace"),
                deleted=bool(d.get("deleted", False)),
                hostile=bool(d.get("hostile", False)),
                versions=versions,
            )
        )
    return Manifest(
        corpus_dir, raw.get("name", corpus_dir.name), orgs, workspaces, personas, tuple(docs)
    )


# ------------------------------------------------------------------------------- dataset


def load_dataset(path: Path = DEFAULT_DATASET, manifest: Manifest | None = None) -> list[Item]:
    """Parse ``path`` (JSONL), validate every line, and (with a manifest) resolve references."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    items: list[Item] = []
    ids: set[str] = set()
    titles: dict[str, str] = {}
    if manifest is not None:
        titles = {d.title: d.id for d in manifest.documents}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path.name}:{number}: invalid JSON: {exc}") from exc
        errors = validate_schema(raw, schema)
        if errors:
            raise DatasetError(f"{path.name}:{number}: " + "; ".join(errors))
        if raw["id"] in ids:
            raise DatasetError(f"{path.name}:{number}: duplicate id {raw['id']!r}")
        ids.add(raw["id"])
        docs = tuple(titles.get(d, d) for d in raw["expected_docs"])
        item = Item(
            id=raw["id"],
            question=raw["question"],
            kind=raw["kind"],
            persona=raw["persona"],
            expected_docs=docs,
            expected_facts=tuple(raw["expected_facts"]),
            forbidden=tuple(raw["forbidden"]),
            expect_abstain=raw["expect_abstain"],
            notes=raw.get("notes", ""),
        )
        if manifest is not None:
            _check_references(item, manifest, f"{path.name}:{number}")
        items.append(item)
    if not items:
        raise DatasetError(f"{path} has no items")
    return items


def _check_references(item: Item, manifest: Manifest, where: str) -> None:
    if item.persona not in manifest.personas:
        raise DatasetError(f"{where}: unknown persona {item.persona!r}")
    known = {d.id for d in manifest.documents}
    for doc in item.expected_docs:
        if doc not in known:
            raise DatasetError(f"{where}: unknown expected doc {doc!r}")
    if item.expect_abstain and (item.expected_docs or item.expected_facts):
        raise DatasetError(f"{where}: an abstain item cannot expect docs or facts")
    if not item.expect_abstain and not item.expected_facts:
        raise DatasetError(f"{where}: an answer item needs expected_facts")
