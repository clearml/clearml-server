from typing import Sequence, Tuple, Any, Union, Callable, Optional, Protocol


def flatten_nested_items(
    dictionary: dict, nesting: int = None, include_leaves=None, prefix=None
) -> Sequence[Tuple[Tuple[str, ...], Any]]:
    """
    iterate through dictionary and return with nested keys flattened into a tuple
    """
    next_nesting = None if nesting is None else (nesting - 1)
    prefix = prefix or ()
    for key, value in dictionary.items():
        path = prefix + (key,)
        if isinstance(value, dict) and nesting != 0:
            yield from flatten_nested_items(
                value, next_nesting, include_leaves, prefix=path
            )
        elif include_leaves is None or key in include_leaves:
            yield path, value


def deep_merge(source: dict, override: dict) -> dict:
    """
    Merge the override dict into the source in-place
    Contrary to the dpath.merge the sequences are not expanded
    If override contains the sequence with the same name as source
    then the whole sequence in the source is overridden
    """
    for key, value in override.items():
        if key in source and isinstance(source[key], dict) and isinstance(value, dict):
            deep_merge(source[key], value)
        else:
            source[key] = value

    return source


class GetItem(Protocol):
    def __getitem__(self, key: Any) -> Any:
        pass


def nested_get(
    dictionary: GetItem,
    path: Sequence[str],
    default: Optional[Union[Any, Callable]] = None,
) -> Any:
    node = dictionary
    for key in path:
        if not node or key not in node:
            if callable(default):
                return default()
            return default
        node = node.get(key)

    return node


def nested_delete(dictionary: dict, path: Union[Sequence[str], str]) -> bool:
    """
    Return 'True' if the element was deleted
    """
    if isinstance(path, str):
        path = [path]

    *parent_path, last_key = path
    parent = nested_get(dictionary, parent_path)
    if not parent or last_key not in parent:
        return False

    del parent[last_key]
    return True


def nested_set(dictionary: dict, path: Union[Sequence[str], str], value: Any):
    if isinstance(path, str):
        path = [path]

    *parent_path, last_key = path
    node = dictionary
    for key in parent_path:
        if key not in node:
            node[key] = {}
        node = node.get(key)

    node[last_key] = value


def exclude_fields_from_dict(data: dict, fields: Sequence[str], separator="."):
    """
    Performs in place fields exclusion on the passed dict
    """
    assert isinstance(data, dict)
    if not fields:
        return

    exclude_paths = [e.split(separator) for e in fields]
    for path in sorted(exclude_paths):
        nested_delete(data, path)


def project_dict(data: dict, projection: Sequence[str], separator=".") -> dict:
    """
    Project partial data from a dictionary into a new dictionary
    :param data: Input dictionary
    :param projection: List of dictionary paths (each a string with field names separated using a separator)
    :param separator: Separator (default is '.')
    :return: A new dictionary containing only the projected parts from the original dictionary
    """
    assert isinstance(data, dict)
    result = {}

    def copy_path(path_parts, source, destination):
        src, dst = source, destination
        try:
            for depth, path_part in enumerate(path_parts[:-1]):
                src_part = src[path_part]
                if isinstance(src_part, dict):
                    src = src_part
                    dst = dst.setdefault(path_part, {})
                elif isinstance(src_part, (list, tuple)):
                    if path_part not in dst:
                        dst[path_part] = [{} for _ in range(len(src_part))]
                    elif not isinstance(dst[path_part], (list, tuple)):
                        raise TypeError(
                            "Incompatible destination type %s for %s (list expected)"
                            % (type(dst), separator.join(path_parts[: depth + 1]))
                        )
                    elif not len(dst[path_part]) == len(src_part):
                        raise ValueError(
                            "Destination list length differs from source length for %s"
                            % separator.join(path_parts[: depth + 1])
                        )

                    dst[path_part] = [
                        copy_path(path_parts[depth + 1 :], s, d)
                        for s, d in zip(src_part, dst[path_part])
                    ]

                    return destination
                else:
                    raise TypeError(
                        "Unsupported projection type %s for %s"
                        % (type(src), separator.join(path_parts[: depth + 1]))
                    )

            last_part = path_parts[-1]
            dst[last_part] = src[last_part]
        except KeyError:
            # Projection field not in source, no biggie.
            pass
        return destination

    for projection_path in sorted(projection):
        copy_path(
            path_parts=projection_path.split(separator), source=data, destination=result
        )
    return result
