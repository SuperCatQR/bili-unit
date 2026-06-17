# models — typed model registry.
#
# Each model module exports a dataclass with from_raw() / to_dict() / from_dict()
# plus collect_image_jobs() / apply_image_results() for the optional image
# download step.
#
# PARSERS maps model_name → parser class.  ParsingCommand uses this registry
# to iterate over all models.

from __future__ import annotations

# Lazy imports — models are only loaded when the registry is first accessed.
# This avoids circular imports and keeps the parsing package importable even
# before all model modules have been written.

_PARSER_NAMES: dict[str, str] = {
    "user_profile": ".models.up_profile",
    "video_work": ".models.video_detail",
    "video_subtitle": ".models.video_subtitle",
    "article_post": ".models.article",
    "opus_post": ".models.opus",
    "dynamic_event": ".models.dynamic",
}


def get_parser(model_name: str):
    """Return the parser class for a given model name.

    Raises KeyError if the model name is not registered.
    """
    import importlib

    module_path = _PARSER_NAMES[model_name]
    module = importlib.import_module(module_path, package="bili_unit.parsing")
    return module.PARSER


def all_parser_names() -> list[str]:
    """Return all registered model names."""
    return list(_PARSER_NAMES.keys())
