"""Every setting that exists, walked out of the schema rather than listed by hand.

`frank configure` used to be able to show only what a file already contained, which is exactly
backwards: a person reaching for it wants to know what they *could* set, and the file they have
is by definition the part they already know about. Nothing enumerated the rest, because nothing
knew what the rest was — the settings existed as Pydantic fields and the explanations for them
existed as comments beside those fields, in a form no program could read.

So the explanations moved into ``Field(description=...)`` and the tunables' into
:class:`~frank.base.tuning.Default`, and this module walks the models to produce the list. Both
the terminal listing and the generated reference file read it, which is what keeps them from
disagreeing with the code or with each other.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel
from pydantic_core import PydanticUndefined


@dataclass(frozen=True)
class Setting:
    """One thing a person may set, addressed the way they would write it."""

    path: str
    about: str
    default: Any
    # A map that accepts any key — `providers`, `mcp.servers`. The names under it are the
    # user's own, so the walk stops here rather than inventing entries that do not exist yet.
    open_ended: bool = False


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


# The one map whose keys are neither fixed by a model nor invented by the user: they are the
# names in `Tunable`. `_walk` enumerates them, so `_descend` must not also accept whatever it is
# handed under this prefix — that is what let a misspelled tunable resolve as if it existed.
TUNING_DEFAULTS = "tuning.defaults"


def _tuning_defaults(prefix: str) -> list[Setting]:
    """The individual tunables, expanded under ``tuning.defaults``.

    This is the one field whose keys are neither fixed by a model nor invented by the user:
    they are the names in :class:`~frank.base.tuning.Tunable`, each of which already carries its
    own shipped value and its own explanation. Enumerating them here is what lets somebody ask
    what may go under `defaults` without reading the source."""
    from frank.base.tuning import Tunable

    return [
        Setting(path=f"{prefix}.{tunable.name}", about=tunable.about, default=tunable.default)
        for tunable in Tunable
    ]


def _walk(model: type[BaseModel], prefix: str) -> list[Setting]:
    settings: list[Setting] = []
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        annotation = field.annotation
        if path == TUNING_DEFAULTS:
            settings.append(Setting(
                path=path,
                about=str(field.description or ""),
                default={},
                open_ended=True,
            ))
            settings.extend(_tuning_defaults(path))
            continue
        if _is_open_ended_map(annotation):
            settings.append(Setting(
                path=path,
                about=str(field.description or ""),
                default=_field_default(field),
                open_ended=True,
            ))
            continue
        nested = _model_of(annotation)
        if nested is not None:
            settings.append(Setting(
                path=path, about=str(field.description or ""), default=_field_default(field)
            ))
            settings.extend(_walk(nested, path))
            continue
        settings.append(Setting(
            path=path, about=str(field.description or ""), default=_field_default(field)
        ))
    return settings


def settings() -> list[Setting]:
    """Every setting, in the order the schema declares them — which is the order a person
    reading the file top to bottom would meet them, not alphabetical."""
    from frank.base.configuration import GlobalConfiguration

    return _walk(GlobalConfiguration, "")


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
    from frank.base.configuration import GlobalConfiguration

    return _descend(GlobalConfiguration, path.split("."), path)


def _descend(model: type[BaseModel], segments: list[str], full_path: str) -> Optional[Setting]:
    """Resolve a path through the models, stepping over the user's own keys in open-ended maps."""
    field = model.model_fields.get(segments[0])
    if field is None:
        return None
    remaining = segments[1:]
    if not remaining:
        return Setting(
            path=full_path,
            about=str(field.description or ""),
            default=_field_default(field),
            open_ended=_is_open_ended_map(field.annotation),
        )
    annotation = field.annotation
    if typing.get_origin(annotation) is dict:
        value_model = _model_of(typing.get_args(annotation)[1])
        if value_model is None:
            # A map of scalars — `sandbox.limits`, `telemetry.exporter.headers`. One segment
            # past it names an entry, which is settable; anything deeper is not.
            if len(remaining) != 1:
                return None
            shipped = _field_default(field)
            return Setting(
                path=full_path,
                about=str(field.description or ""),
                default=shipped.get(remaining[0]) if isinstance(shipped, dict) else None,
            )
        # The next segment is the user's own name for an entry; what follows it is that
        # entry's own field.
        if not remaining[1:]:
            return Setting(path=full_path, about=str(field.description or ""), default=None)
        return _descend(value_model, remaining[1:], full_path)
    nested = _model_of(annotation)
    return _descend(nested, remaining, full_path) if nested is not None else None
