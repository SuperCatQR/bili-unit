# _schema — declarative key ↔ path grammar for the JSON KV stores.
#
# Every stage (fetching / parsing / processing) used to hand-roll a KeyMapper
# whose ``to_path`` / ``to_key`` / ``prefix_to_scan_dir`` triplet repeated the
# same grammar skeleton — split ``uid:{uid}:{section}:…``, special-case the
# ``task`` key, fall back to ``_misc`` for malformed keys, normalise a trailing
# colon in prefixes — and differed only in how each *section* maps its tail
# tokens onto directory segments.
#
# This module owns that skeleton once.  A stage declares a :class:`KvSchema`
# table (its "adapter") describing each section's layout, and
# :class:`SchemaKeyMapper` drives the shared engine against it.  Storage-path
# changes now live in one place; a stage's key grammar is data, not code.
#
# A key has the form ``{namespace}:{…}``.  The engine knows two namespace
# kinds:
#
#   * the *id namespace* (``id_prefix``, conventionally ``"uid"``):
#     ``uid:{uid}:{section}:{tail…}`` → ``{base}/{uid}/<dirs>/<stem>.json``
#   * *flat namespaces* (e.g. ``rate_limit``):
#     ``rate_limit:{key}`` → ``{base}/rate_limit/{key}.json``
#
# For a section, ``tokens = [section, *tail]``.  A :class:`PathShape` selects
# which token indices become directory segments (``dir_indices``) and which
# token is the filename stem (``file_index``); index 0 is the section literal,
# so a shape that omits it (e.g. parsing's ``parse``) drops the section word
# from the path.  ``overflow_join`` lets the stem absorb any extra
# colon-joined tail tokens (fetching item ids).

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PathShape:
    """How one ``tokens = [section, *tail]`` list maps onto a path.

    ``dir_indices`` — token indices, in path order, that become directory
    segments.  ``file_index`` — the token whose value is the filename stem.
    Index 0 is the section literal; omit it to drop the section word from the
    path.  ``overflow_join`` — the stem joins ``tokens[file_index:]`` with
    ``":"`` so a section can accept a variable-length tail (e.g. item ids that
    themselves contain colons).
    """

    dir_indices: tuple[int, ...]
    file_index: int
    overflow_join: bool = False


@dataclass(frozen=True)
class KvSchema:
    """A stage's full key grammar.

    ``id_prefix`` — the namespace carrying a numeric id dir (``"uid"``).
    ``sections`` — ``section → {tail_len: PathShape}``; ``tail_len`` is the
    number of tokens after the section.  ``flat`` — ``namespace → PathShape``
    for keys that live outside the id namespace (e.g. ``rate_limit``).
    """

    id_prefix: str
    sections: Mapping[str, Mapping[int, PathShape]]
    flat: Mapping[str, PathShape] = field(default_factory=dict)


class SchemaKeyMapper:
    """KeyMapper Protocol impl driven by a class-level :class:`KvSchema`.

    Subclasses set ``schema``; instances are interchangeable, so a single
    mapper can be shared across stores or constructed ad-hoc in tests.
    """

    schema: KvSchema

    def to_path(self, base: Path, key: str) -> Path:
        return _to_path(self.schema, base, key)

    def to_key(self, base: Path, path: Path) -> str:
        return _to_key(self.schema, base, path)

    def prefix_to_scan_dir(self, base: Path, prefix: str) -> Path:
        return _prefix_to_scan_dir(self.schema, base, prefix)


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


def _misc_path(base: Path, key: str) -> Path:
    safe = key.replace(":", "__")
    return base / "_misc" / f"{safe}.json"


def _select_shape(
    shapes: Mapping[int, PathShape], tail_len: int,
) -> PathShape | None:
    """Pick the shape for ``tail_len``: exact match, else widest overflow."""
    exact = shapes.get(tail_len)
    if exact is not None:
        return exact
    best: tuple[int, PathShape] | None = None
    for nominal, shape in shapes.items():
        if shape.overflow_join and nominal <= tail_len and (
            best is None or nominal > best[0]
        ):
            best = (nominal, shape)
    return best[1] if best else None


def _build_path(
    base: Path,
    uid: str | None,
    shape: PathShape,
    tokens: list[str],
) -> Path:
    p = base
    if uid is not None:
        p = p / uid
    for i in shape.dir_indices:
        p = p / tokens[i]
    stem = (
        ":".join(tokens[shape.file_index:])
        if shape.overflow_join
        else tokens[shape.file_index]
    )
    return p / f"{stem}.json"


def _to_path(schema: KvSchema, base: Path, key: str) -> Path:
    parts = key.split(":")
    if not parts:
        return _misc_path(base, key)

    # flat namespace (e.g. rate_limit:{key})
    flat_shape = schema.flat.get(parts[0])
    if flat_shape is not None and len(parts) >= 2:
        return _build_path(base, None, flat_shape, parts)

    # id namespace (uid:{uid}:{section}:{tail…})
    if len(parts) >= 3 and parts[0] == schema.id_prefix:
        uid, section, tail = parts[1], parts[2], parts[3:]
        shapes = schema.sections.get(section)
        if shapes is not None:
            shape = _select_shape(shapes, len(tail))
            if shape is not None:
                return _build_path(base, uid, shape, [section, *tail])

    return _misc_path(base, key)


def _reverse_one(
    seg_parts: list[str],
    stem: str,
    shape: PathShape,
    *,
    section: str,
) -> list[str] | None:
    """Reverse one shape: path segments → ``[section, *tail]`` or None.

    ``seg_parts`` are the path parts after the uid dir (or all parts, for a
    flat namespace).  Returns the reconstructed token list when the shape
    matches, else None.
    """
    if len(seg_parts) != len(shape.dir_indices) + 1:
        return None
    token_values: dict[int, str] = {}
    for k, ti in enumerate(shape.dir_indices):
        token_values[ti] = seg_parts[k]
    token_values[shape.file_index] = stem
    # token 0 is the section literal; verify when the shape uses it.
    if 0 in token_values and token_values[0] != section:
        return None
    tail = [token_values[i] for i in sorted(token_values) if i >= 1]
    return [section, *tail]


def _to_key(schema: KvSchema, base: Path, path: Path) -> str:
    rel = path.relative_to(base)
    parts = list(rel.parts)
    if not parts:
        return f"_unknown:{rel}"
    stem = parts[-1][:-5] if parts[-1].endswith(".json") else parts[-1]

    # flat namespaces: full path, no uid dir.
    for ns, shape in schema.flat.items():
        tokens = _reverse_one(parts, stem, shape, section=ns)
        if tokens is not None:
            return ":".join(tokens)

    # id namespace: parts[0] is the uid dir.
    if len(parts) >= 2:
        uid = parts[0]
        remaining = parts[1:]
        # Prefer shapes that pin the section as a literal (token 0) so a
        # variable-first-dir shape (e.g. parsing's ``parse``) only wins when
        # no literal shape of the same arity matches.
        candidates: list[tuple[str, PathShape]] = [
            (section, shape)
            for section, shapes in schema.sections.items()
            for shape in shapes.values()
        ]
        candidates.sort(
            key=lambda sc: 0
            if (0 in sc[1].dir_indices or sc[1].file_index == 0)
            else 1,
        )
        for section, shape in candidates:
            tokens = _reverse_one(remaining, stem, shape, section=section)
            if tokens is not None:
                return ":".join([schema.id_prefix, uid, *tokens])

    return f"_unknown:{rel}"


def _dir_template(shapes: Mapping[int, PathShape]) -> list[int | None]:
    """Directory template from a section's widest shape.

    Each entry is ``None`` for the section literal or ``j`` for ``tail[j]``.
    """
    widest = shapes[max(shapes)]
    template: list[int | None] = []
    for ti in widest.dir_indices:
        template.append(None if ti == 0 else ti - 1)
    return template


def _prefix_to_scan_dir(schema: KvSchema, base: Path, prefix: str) -> Path:
    parts = prefix.split(":")
    while parts and parts[-1] == "":
        parts = parts[:-1]

    if parts:
        if parts[0] in schema.flat:
            return base / parts[0]
        if parts[0] == schema.id_prefix:
            if len(parts) == 1:
                return base
            uid = parts[1]
            if len(parts) == 2:
                return base / uid
            section = parts[2]
            shapes = schema.sections.get(section)
            if shapes is not None:
                tail = parts[3:]
                p = base / uid
                for node in _dir_template(shapes):
                    if node is None:
                        p = p / section
                    elif node < len(tail) and tail[node] != "":
                        p = p / tail[node]
                    else:
                        break
                return p

    # Fallback: resolve as a full key; scan the dir itself or its parent.
    target = _to_path(schema, base, prefix)
    return target if target.is_dir() else target.parent
