"""Every setting that exists, walked out of the schema rather than listed by hand.

`frank configure` used to be able to show only what a file already contained, which is exactly
backwards: a person reaching for it wants to know what they *could* set, and the file they have
is by definition the part they already know about. Nothing enumerated the rest, because nothing
knew what the rest was — the settings existed as Pydantic fields and the descriptions for them
existed as comments beside those fields, in a form no program could read.

So this module walks the models to produce the list: every path there is, what it holds, what it
ships at. What each one is *called* and what it is *for* are not here — they are words, and words
belong in the message catalogue (`shared/messages/`) where they can be translated, and in the
configuration reference. Keeping them beside the fields put the interface in one language
whatever the person had chosen, and put the same sentence in the repository twice.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from enum import Enum
from types import UnionType
from typing import Any, Optional

from pydantic import BaseModel
from pydantic_core import PydanticUndefined


#: What a setting holds, in the vocabulary a control is chosen from.
KIND_SECTION = "section"    # a place to put things, not a thing itself
KIND_BOOLEAN = "boolean"
KIND_INTEGER = "integer"
KIND_NUMBER = "number"
KIND_STRING = "string"
KIND_CHOICE = "choice"      # a fixed set of values: a Literal or an Enum
KIND_LIST = "list"          # a list of strings — paths, names, patterns
KIND_MAP = "map"            # keys the user invents


@dataclass(frozen=True)
class Setting:
    """One thing a person may set, addressed the way they would write it."""

    path: str
    default: Any
    # A map that accepts any key — `providers`, `mcp.servers`.
    open_ended: bool = False
    #: What it holds, so an interface can choose a control without a table of its own.
    kind: str = KIND_STRING
    #: The values a `choice` accepts, in the order the schema declares them.
    choices: tuple[str, ...] = ()
    #: Whether the field may hold nothing at all, which is not the same as holding "".
    optional: bool = False
    #: A credential.
    secret: bool = False


def _plain(value: Any) -> Any:
    """A default as YAML and JSON would hold it."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _is_secret(field) -> bool:
    """Whether this field holds a credential, as its own declaration says."""
    extra = getattr(field, "json_schema_extra", None)
    return bool(extra.get("secret")) if isinstance(extra, dict) else False


def _field_default(field) -> Any:
    if field.default_factory is not None:
        return _plain(field.default_factory())
    if field.default is PydanticUndefined:
        return None
    return _plain(field.default)


def _model_of(annotation: Any) -> Optional[type[BaseModel]]:
    """The model an annotation resolves to, seeing through ``Optional``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for argument in typing.get_args(annotation):
        if isinstance(argument, type) and issubclass(argument, BaseModel):
            return argument
    return None


def _is_open_ended_map(annotation: Any) -> bool:
    """A ``dict`` whose keys the user invents, rather than a fixed set of named entries."""
    return typing.get_origin(annotation) is dict and _model_of(typing.get_args(annotation)[1]) is not None


def _unwrapped(annotation: Any) -> tuple[Any, bool]:
    """An annotation with ``Optional`` peeled off, and whether it was there."""
    arguments = typing.get_args(annotation)
    if typing.get_origin(annotation) in (typing.Union, UnionType) and type(None) in arguments:
        remaining = [argument for argument in arguments if argument is not type(None)]
        return (remaining[0] if len(remaining) == 1 else annotation), True
    return annotation, False


def _choices(annotation: Any) -> tuple[str, ...]:
    """The values a fixed-set annotation accepts: a ``Literal``'s arguments, or an ``Enum``'s."""
    if typing.get_origin(annotation) is typing.Literal:
        return tuple(str(argument) for argument in typing.get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return tuple(str(member.value) for member in annotation)
    return ()


def _kind_of(annotation: Any, default: Any) -> tuple[str, tuple[str, ...], bool]:
    """What this field holds, how to offer it, and whether it may be nothing.

    Read from the annotation, with the default as the tie-breaker for the one case an
    annotation cannot answer: a bare `dict` or a `list` says nothing about what goes in it."""
    inner, optional = _unwrapped(annotation)
    choices = _choices(inner)
    if choices:
        return KIND_CHOICE, choices, optional
    origin = typing.get_origin(inner)
    if origin in (list, tuple, set, frozenset):
        return KIND_LIST, (), optional
    if origin is dict:
        return KIND_MAP, (), optional
    if inner is bool:
        return KIND_BOOLEAN, (), optional
    if inner is int:
        return KIND_INTEGER, (), optional
    if inner is float:
        return KIND_NUMBER, (), optional
    if isinstance(default, bool):
        return KIND_BOOLEAN, (), optional
    if isinstance(default, list):
        return KIND_LIST, (), optional
    if isinstance(default, dict):
        return KIND_MAP, (), optional
    return KIND_STRING, (), optional


# The one map whose keys are neither fixed by a model nor invented by the user: they are the names in `Tunable`.
TUNING_DEFAULTS = "tuning.defaults"


def _tuning_defaults(prefix: str) -> list[Setting]:
    """The individual tunables, expanded under ``tuning.defaults``.

    This is the one field whose keys are neither fixed by a model nor invented by the user:
    they are the names in :class:`~frank.base.tuning.Tunable`, each of which already carries its
    own shipped value and its own explanation. Enumerating them here is what lets somebody ask
    what may go under `defaults` without reading the source."""
    from frank.base.tuning import Tunable

    return [
        Setting(
            path=f"{prefix}.{tunable.name}",
            default=tunable.default,
            # Every tunable is a number: an amount, a duration, or a fraction.
            kind=KIND_INTEGER if isinstance(tunable.default, int) and not isinstance(tunable.default, bool) else KIND_NUMBER,
        )
        for tunable in Tunable
    ]


def _walk(model: type[BaseModel], prefix: str) -> list[Setting]:
    settings: list[Setting] = []
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        annotation = field.annotation
        if path == TUNING_DEFAULTS:
            settings.append(Setting(path=path, default={}, open_ended=True, kind=KIND_SECTION))
            settings.extend(_tuning_defaults(path))
            continue
        if _is_open_ended_map(annotation):
            settings.append(Setting(
                path=path, default=_field_default(field), open_ended=True, kind=KIND_MAP,
            ))
            continue
        nested = _model_of(annotation)
        if nested is not None:
            settings.append(Setting(path=path, default=_field_default(field), kind=KIND_SECTION))
            settings.extend(_walk(nested, path))
            continue
        kind, choices, optional = _kind_of(annotation, _field_default(field))
        settings.append(Setting(
            path=path, default=_field_default(field),
            kind=kind, choices=choices, optional=optional, secret=_is_secret(field),
        ))
    return settings


#: The order a person meets the sections in, from what they decide to what they tune.
SECTION_ORDER = (
    # What the agent may do, and where.
    "agent", "workspace", "sandbox", "toolbox", "permission_reviewer",
    # What it carries between turns, and what it knows about you.
    "compaction", "user_context",
    # The surfaces it can reach for.
    "computer_control", "dictation",
    # Who it talks to, and with what credentials.
    "providers", "exa", "jina", "firecrawl", "web_fetch", "composio", "mcp", "remote_agents",
    # What is watching, and the numbers underneath everything.
    "telemetry", "tuning",
)


def settings() -> list[Setting]:
    """Every setting, sections ordered from what a person decides to what they tune, and within
    a section in the order the schema declares them."""
    from frank.base.configuration import Configuration

    walked = _walk(Configuration, "")
    position = {name: index for index, name in enumerate(SECTION_ORDER)}
    return sorted(walked, key=lambda setting: position.get(setting.path.split(".")[0], len(position)))


def leaf_settings() -> list[Setting]:
    """Only the settings that hold a value — no section headers. A section is a setting whose
    path is a prefix of another's, so it is a place to put things rather than a thing itself."""
    everything = settings()
    prefixes = {setting.path.rsplit(".", 1)[0] for setting in everything if "." in setting.path}
    return [
        setting for setting in everything
        if setting.path not in prefixes or setting.open_ended
    ]


def setting_for(path: str) -> Optional[Setting]:
    """The setting at a dotted path, or ``None`` if the schema does not define one.

    A path under an open-ended map — ``providers.anthropic.api_key`` — resolves to the setting
    the map's value model defines, since the key in the middle is the user's own name for one
    of them and cannot be enumerated in advance."""
    for setting in settings():
        if setting.path == path:
            return setting
    if path.startswith(TUNING_DEFAULTS + "."):
        # Every valid name under it was in the list just searched, so this one is a typo.
        return None
    from frank.base.configuration import Configuration

    return _descend(Configuration, path.split("."), path)


def _descend(model: type[BaseModel], segments: list[str], full_path: str) -> Optional[Setting]:
    """Resolve a path through the models, stepping over the user's own keys in open-ended maps."""
    field = model.model_fields.get(segments[0])
    if field is None:
        return None
    remaining = segments[1:]
    if not remaining:
        return Setting(
            path=full_path,
            default=_field_default(field),
            open_ended=_is_open_ended_map(field.annotation),
        )
    annotation = field.annotation
    if typing.get_origin(annotation) is dict:
        value_model = _model_of(typing.get_args(annotation)[1])
        if value_model is None:
            # A map of scalars — `sandbox.limits`, `telemetry.exporter.headers`.
            if len(remaining) != 1:
                return None
            shipped = _field_default(field)
            return Setting(
                path=full_path,
                    default=shipped.get(remaining[0]) if isinstance(shipped, dict) else None,
            )
        # The next segment is the user's own name for an entry; what follows it is that entry's own field.
        if not remaining[1:]:
            return Setting(path=full_path, default=None)
        return _descend(value_model, remaining[1:], full_path)
    nested = _model_of(annotation)
    return _descend(nested, remaining, full_path) if nested is not None else None
