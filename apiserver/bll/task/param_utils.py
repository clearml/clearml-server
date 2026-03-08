import itertools
from typing import Sequence, Tuple, Optional


from apiserver.apierrors import errors
from apiserver.database.model.task.task import Task
from apiserver.utilities.dicts import nested_get, nested_delete, nested_set
from apiserver.utilities.parameter_key_escaper import ParameterKeyEscaper


hyperparams_default_section = "Args"
hyperparams_legacy_type = "legacy"
tf_define_section = "TF_DEFINE"


def split_param_name(full_name: str, default_section: str) -> Tuple[Optional[str], str]:
    """
    Return parameter section and name. The section is either TF_DEFINE or the default one
    """
    if default_section is None:
        return None, full_name

    section, _, name = full_name.partition("/")
    if section != tf_define_section:
        return default_section, full_name

    if not name:
        raise errors.bad_request.ValidationError("Parameter name cannot be empty")
    return section, name


def _get_full_param_name(param: dict) -> str:
    section = param.get("section")
    if section != tf_define_section:
        return param["name"]

    return "/".join((section, param["name"]))


def _remove_legacy_params(data: dict, with_sections: bool = False) -> int:
    """
    Remove the legacy params from the data dict and return the number of removed params
    If the path not found then return 0
    """
    removed = 0
    if not data:
        return removed

    if with_sections:
        for section, section_data in list(data.items()):
            removed += _remove_legacy_params(section_data)
            if not section_data:
                """If section is empty after removing legacy params then delete it"""
                del data[section]
    else:
        for key, param in list(data.items()):
            if param.get("type") == hyperparams_legacy_type:
                removed += 1
                del data[key]

    return removed


def _get_legacy_params(data: dict, with_sections: bool = False) -> Sequence[dict]:
    """
    Remove the legacy params from the data dict and return the number of removed params
    If the path not found then return 0
    """
    if not data:
        return []

    if with_sections:
        return list(
            itertools.chain.from_iterable(
                _get_legacy_params(section_data) for section_data in data.values()
            )
        )

    return [
        param for param in data.values() if param.get("type") == hyperparams_legacy_type
    ]


def params_prepare_for_save(fields: dict, previous_task: Task = None):
    """
    If legacy hyper params or configuration is passed then replace the corresponding section in the new structure
    Escape all the section and param names for hyper params and configuration to make it mongo sage
    """
    for old_params_field, new_params_field, default_section in (
        (("execution", "parameters"), "hyperparams", hyperparams_default_section),
        (("execution", "model_desc"), "configuration", None),
    ):
        legacy_params = nested_get(fields, old_params_field)
        if legacy_params is None:
            continue

        if (
            not fields.get(new_params_field)
            and previous_task
            and previous_task[new_params_field]
        ):
            previous_data = previous_task.to_proper_dict().get(new_params_field)
            removed = _remove_legacy_params(
                previous_data, with_sections=default_section is not None
            )
            if not legacy_params and not removed:
                # if we only need to delete legacy fields from the db
                # but they are not there then there is no point to proceed
                continue

            fields_update = {new_params_field: previous_data}
            params_unprepare_from_saved(fields_update)
            fields.update(fields_update)

        for full_name, value in legacy_params.items():
            section, name = split_param_name(full_name, default_section)
            new_path = list(filter(None, (new_params_field, section, name)))
            new_param = dict(name=name, type=hyperparams_legacy_type, value=str(value))
            if section is not None:
                new_param["section"] = section
            nested_set(fields, new_path, new_param)
        nested_delete(fields, old_params_field)

    def ensure_non_empty(k: str, desc: str) -> str:
        if not k:
            raise errors.bad_request.ValidationError(
                f"Empty {desc} name is not allowed"
            )
        return k

    params = fields.get("hyperparams")
    if params:
        escaped_params = {
            ParameterKeyEscaper.escape(ensure_non_empty(key, "section")): {
                ParameterKeyEscaper.escape(ensure_non_empty(k, "parameter")): v
                for k, v in value.items()
            }
            for key, value in params.items()
        }
        fields["hyperparams"] = escaped_params

    params = fields.get("configuration")
    if params:
        escaped_params = {
            ParameterKeyEscaper.escape(ensure_non_empty(key, "configuration")): value
            for key, value in params.items()
        }
        fields["configuration"] = escaped_params


def params_unprepare_from_saved(fields, copy_to_legacy=False):
    """
    Unescape all section and param names for hyper params and configuration
    If copy_to_legacy is set then copy hyperparams and configuration data to the legacy location for the old clients
    """
    for param_field in ("hyperparams", "configuration"):
        params = fields.get(param_field)
        if params:
            unescaped_params = {
                ParameterKeyEscaper.unescape(key): {
                    ParameterKeyEscaper.unescape(k): v for k, v in value.items()
                }
                if isinstance(value, dict)
                else value
                for key, value in params.items()
            }
            fields[param_field] = unescaped_params

    if copy_to_legacy:
        for new_params_field, old_params_field, use_sections in (
            ("hyperparams", ("execution", "parameters"), True),
            ("configuration", ("execution", "model_desc"), False),
        ):
            legacy_params = _get_legacy_params(
                fields.get(new_params_field), with_sections=use_sections
            )
            if legacy_params:
                nested_set(
                    fields,
                    old_params_field,
                    {_get_full_param_name(p): p["value"] for p in legacy_params},
                )


def _process_path(path: str):
    """
    Frontend does a partial escaping on the path so the all '.' in section and key names are escaped
    Need to unescape and apply a full mongo escaping
    """
    parts = path.split(".")
    if len(parts) < 2 or len(parts) > 4:
        raise errors.bad_request.ValidationError("invalid task field", path=path)
    return ".".join(
        ParameterKeyEscaper.escape(ParameterKeyEscaper.unescape(p)) for p in parts
    )


def escape_paths(paths: Sequence[str]) -> Sequence[str]:
    for old_prefix, new_prefix in (
        ("execution.parameters", f"hyperparams.{hyperparams_default_section}"),
        ("execution.model_desc", "configuration"),
        ("execution.docker_cmd", "container"),
    ):
        paths = [path.replace(old_prefix, new_prefix) for path in paths]

    for prefix in (
        "hyperparams.",
        "-hyperparams.",
        "configuration.",
        "-configuration.",
    ):
        paths = [
            _process_path(path) if path.startswith(prefix) else path for path in paths
        ]
    return paths
