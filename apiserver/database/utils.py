import hashlib
from inspect import ismethod, getmembers
from typing import Sequence, Tuple, Set, Optional, Callable, Any, Mapping
from uuid import uuid4

from mongoengine import EmbeddedDocumentField, ListField, Document, Q
from mongoengine.base import BaseField

from .errors import translate_errors_context, ParseCallError


def get_fields(cls, of_type=BaseField, return_instance=False, subfields=False):
    return _get_fields(
        cls,
        of_type=of_type,
        subfields=subfields,
        selector=lambda k, v: (k, v) if return_instance else k,
    )


def get_fields_attr(cls, attr):
    """ get field names from a class containing mongoengine fields """
    return dict(
        _get_fields(cls, with_attr=attr, selector=lambda k, v: (k, getattr(v, attr)))
    )


def get_fields_choices(cls, attr):
    def get_choices(field_name: str, field: BaseField) -> Tuple:
        if isinstance(field, ListField):
            return field_name, field.field.choices
        return field_name, field.choices

    return dict(_get_fields(cls, with_attr=attr, subfields=True, selector=get_choices))


def _get_fields(
    cls,
    with_attr=None,
    of_type=BaseField,
    subfields=False,
    selector: Optional[Callable[[str, BaseField], Any]] = None,
    path: Tuple[str, ...] = (),
):
    fields = []
    for field_name, field in cls._fields.items():
        field_path = path + (field_name,)
        if isinstance(field, of_type) and (not with_attr or hasattr(field, with_attr)):
            full_name = "__".join(field_path)
            fields.append(selector(full_name, field) if selector else full_name)

        if subfields and isinstance(field, EmbeddedDocumentField):
            fields.extend(
                _get_fields(
                    field.document_type,
                    with_attr=with_attr,
                    of_type=of_type,
                    subfields=subfields,
                    selector=selector,
                    path=field_path,
                )
            )

    return fields


def get_items(cls):
    """ get key/value items from an enum-like class (members represent enumeration key/value) """

    res = {k: v for k, v in getmembers(cls) if not (k.startswith("_") or ismethod(v))}
    return res


def get_options(cls):
    """ get options from an enum-like class (members represent enumeration key/value) """
    return list(get_items(cls).values())


# return a dictionary of items which:
# 1. are in the call_data
# 2. are in the fields dictionary, and their value in the call_data matches the type in fields
# 3. are in the cls_fields
def parse_from_call(call_data, fields, cls_fields, discard_none_values=True):
    if not isinstance(fields, dict):
        # fields should be key=>type dict
        fields = {k: None for k in fields}
    fields = {k: v for k, v in fields.items() if k in cls_fields}
    res = {}
    with translate_errors_context("parsing call data"):
        for field, desc in fields.items():
            value = call_data.get(field)
            if value is None:
                if not discard_none_values and field in call_data:
                    # we'll keep the None value in case the field actually exists in the call data
                    res[field] = None
                continue
            if desc:
                if issubclass(desc, Document):
                    if not desc.objects(id=value).only("id"):
                        raise ParseCallError(
                            "expecting %s id" % desc.__name__, id=value, field=field
                        )
                elif callable(desc):
                    try:
                        desc(value)
                    except TypeError:
                        raise ParseCallError(f"expecting {desc.__name__}", field=field)
                    except Exception as ex:
                        raise ParseCallError(str(ex), field=field)
            res[field] = value
        return res


def init_cls_from_base(cls, instance):
    return cls(
        **{
            k: v
            for k, v in instance.to_mongo(use_db_field=False).to_dict().items()
            if k[0] != "_"
        }
    )


def get_company_or_none_constraint(company=""):
    return Q(company__in=list({company, ""}))


def field_does_not_exist(field: str, empty_value=None, is_list=False) -> Q:
    """
    Creates a query object used for finding a field that doesn't exist, or has None or an empty value.
    :param field: Field name
    :param empty_value: The empty value to test for (None means no specific empty value will be used)
    :param is_list: Is this a list (array) field. In this case, instead of testing for an empty value,
                    the length of the array will be used (len==0 means empty)
    :return:
    """
    query = Q(**{f"{field}__exists": False}) | Q(
        **{f"{field}__in": {empty_value, None}}
    )
    if is_list:
        query |= Q(**{f"{field}__size": 0})
    return query


def field_exists(field: str, empty_value=None, is_list=False) -> Q:
    """
    Creates a query object used for finding a field that exists and is not None or empty.
    :param field: Field name
    :param empty_value: The empty value to test for (None means no specific empty value will be used)
    :param is_list: Is this a list (array) field. In this case, instead of testing for an empty value,
                    the length of the array will be used (len==0 means empty)
    :return:
    """
    query = Q(**{f"{field}__exists": True}) & Q(
        **{f"{field}__nin": {empty_value, None}}
    )
    if is_list:
        query &= Q(**{f"{field}__not__size": 0})
    return query


def get_subkey(d, key_path, default=None):
    """ Get a key from a nested dictionary. kay_path is a '.' separated string of keys used to traverse
        the nested dictionary.
    """
    keys = key_path.split(".")
    for i, key in enumerate(keys):
        if not isinstance(d, dict):
            raise KeyError(
                "Expecting a dict (%s)" % (".".join(keys[:i]) if i else "bad input")
            )
        d = d.get(key)
        if d is None:
            return default
    return d


def id():
    return str(uuid4()).replace("-", "")


def hash_field_name(s):
    """ Hash field name into a unique safe string """
    return hashlib.md5(s.encode()).hexdigest()


def merge_dicts(*dicts):
    base = {}
    for dct in dicts:
        base.update(dct)
    return base


def filter_fields(cls, fields):
    """From the fields dictionary return only the fields that match cls fields"""
    return {key: fields[key] for key in fields if key in get_fields(cls)}


def _names_set(*names: str) -> Set[str]:
    """
    Given a list of names return set with names and '-names'
    """
    return set(names) | set(f"-{name}" for name in names)


_system_tag_names = {
    "model": _names_set("active", "archived"),
    "project": _names_set("archived", "public", "default"),
    "task": _names_set("active", "archived", "development"),
    "queue": _names_set("default"),
}

_system_tag_prefixes = {"task": _names_set("annotat")}


def partition_tags(
    entity: str,
    tags: Sequence[str],
    system_tags: Optional[Sequence[str]] = (),
    system_tag_names: Mapping = _system_tag_names,
    system_tag_prefixes: Mapping = _system_tag_prefixes,
) -> Tuple[Sequence[str], Sequence[str]]:
    """
    Partition the given tags sequence into system and user-defined tags
    :param entity: The name of the entity that defines the list of the system tags
    :param tags: The tags to partition
    :param system_tags: Optional. If passed then these tags are considered system together
    with those defined for the entity.
    :return: a tuple where the first element is the sequence of user-defined tags and
    the second element is the sequence of system tags
    """
    tags = set(tags)
    system_tags = set(system_tags)
    system_tags |= tags & system_tag_names[entity]

    prefixes = system_tag_prefixes.get(entity, [])
    system_tags |= {t for t in tags for p in prefixes if t.lower().startswith(p)}

    return list(tags - system_tags), list(system_tags)
