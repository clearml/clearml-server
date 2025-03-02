from enum import Enum
from numbers import Number
from typing import Union, Type, Iterable, Mapping

import jsonmodels.errors
import six
from jsonmodels import fields
from jsonmodels.fields import _LazyType, NotSet, EmbeddedField
from jsonmodels.models import Base as ModelBase
from jsonmodels.validators import Enum as EnumValidator
from mongoengine.base import BaseDocument
from validators import email as email_validator, domain as domain_validator

from apiserver.apierrors import errors
from apiserver.utilities.json import loads, dumps


class EmailField(fields.StringField):
    def validate(self, value):
        super().validate(value)
        if value is None:
            return
        if email_validator(value) is not True:
            raise errors.bad_request.InvalidEmailAddress()


class DomainField(fields.StringField):
    def validate(self, value):
        super().validate(value)
        if value is None:
            return
        if domain_validator(value) is not True:
            raise errors.bad_request.InvalidDomainName()


def make_default(field_cls, default_value):
    class _FieldWithDefault(field_cls):
        def get_default_value(self):
            return default_value

    return _FieldWithDefault


class OneOfEmbeddedField(EmbeddedField):
    def __init__(
        self,
        *args,
        discriminator_property: str,
        discriminator_mapping: Mapping[str, type],
        **kwargs,
    ):
        self.discriminator_property = discriminator_property
        self.discriminator_mapping = discriminator_mapping
        model_types = tuple(set(self.discriminator_mapping.values()))

        super().__init__(model_types, *args, **kwargs)

    def parse_value(self, value):
        """Parse value to proper model type."""
        if not isinstance(value, dict) or self.discriminator_property not in value:
            return super().parse_value(value)

        property_value = value.get(self.discriminator_property)
        embed_type = self.discriminator_mapping.get(property_value)
        if not embed_type:
            raise jsonmodels.errors.ValidationError(
                f"Could not find type matching discriminator property value: {property_value}"
            )
        return embed_type(**value)


class ListField(fields.ListField):
    def __init__(self, items_types=None, *args, default=NotSet, **kwargs):
        if default is not NotSet and callable(default):
            default = default()

        super(ListField, self).__init__(items_types, *args, default=default, **kwargs)

    def _cast_value(self, value):
        try:
            return super(ListField, self)._cast_value(value)
        except TypeError:
            if len(self.items_types) == 1 and issubclass(self.items_types[0], Enum):
                return self.items_types[0](value)
            return value

    def validate_single_value(self, item):
        super(ListField, self).validate_single_value(item)
        if isinstance(item, ModelBase):
            item.validate()


class ScalarField(fields.BaseField):

    """String field."""

    types = (str, int, float, bool)


class SafeStringField(fields.StringField):
    """String field that can also accept numbers as input"""
    def parse_value(self, value):
        if isinstance(value, Number):
            value = str(value)

        return super().parse_value(value)


class DictField(fields.BaseField):
    types = (dict,)

    def __init__(self, value_types=None, *args, **kwargs):
        self.value_types = self._assign_types(value_types)
        super(DictField, self).__init__(*args, **kwargs)

    def get_default_value(self):
        default = super(DictField, self).get_default_value()
        if default is None and not self.required:
            return {}
        return default

    @staticmethod
    def _assign_types(value_types):
        if value_types:
            try:
                value_types = tuple(value_types)
            except TypeError:
                value_types = (value_types,)
        else:
            value_types = tuple()

        return tuple(
            _LazyType(type_) if isinstance(type_, six.string_types) else type_
            for type_ in value_types
        )

    def parse_value(self, values):
        """Cast value to proper collection."""
        result = self.get_default_value()

        if values is None:
            return result

        if not self.value_types or not isinstance(values, dict):
            return values

        return {key: self._cast_value(value) for key, value in values.items()}

    def _cast_value(self, value):
        if isinstance(value, self.value_types):
            return value
        else:
            if len(self.value_types) != 1:
                tpl = 'Cannot decide which type to choose from "{types}".'
                raise jsonmodels.errors.ValidationError(
                    tpl.format(types=", ".join([t.__name__ for t in self.value_types]))
                )
            return self.value_types[0](**value)

    def validate(self, value):
        super(DictField, self).validate(value)

        if not self.value_types:
            return

        if not value:
            return

        for item in value.values():
            self.validate_single_value(item)

    def validate_single_value(self, item):
        if not self.value_types:
            return

        if not isinstance(item, self.value_types):
            raise jsonmodels.errors.ValidationError(
                "All items must be instances "
                'of "{types}", and not "{type}".'.format(
                    types=", ".join([t.__name__ for t in self.value_types]),
                    type=type(item).__name__,
                )
            )

    def _elem_to_struct(self, value):
        try:
            return value.to_struct()
        except AttributeError:
            return value

    def to_struct(self, values):
        return {k: self._elem_to_struct(v) for k, v in values.items()}


class IntField(fields.IntField):
    def parse_value(self, value):
        try:
            return super(IntField, self).parse_value(value)
        except (ValueError, TypeError):
            return value


class NullableEnumValidator(EnumValidator):
    """Validator for enums that allows a None value."""

    def validate(self, value):
        if value is not None:
            super(NullableEnumValidator, self).validate(value)


class EnumField(fields.StringField):
    def __init__(
        self,
        values_or_type: Union[Iterable, Type[Enum]],
        *args,
        required=False,
        default=None,
        **kwargs,
    ):
        choices = list(map(self.parse_value, values_or_type))
        validator_cls = EnumValidator if required else NullableEnumValidator
        kwargs.setdefault("validators", []).append(validator_cls(*choices))
        super().__init__(
            default=self.parse_value(default), required=required, *args, **kwargs
        )

    def parse_value(self, value):
        if isinstance(value, Enum):
            return str(value.value)
        return super().parse_value(value)


class ActualEnumField(fields.StringField):
    def __init__(
        self,
        enum_class: Type[Enum],
        *args,
        validators=None,
        required=False,
        default=None,
        **kwargs,
    ):
        self.__enum = enum_class
        self.types = (enum_class,)
        # noinspection PyTypeChecker
        choices = list(enum_class)
        validator_cls = EnumValidator if required else NullableEnumValidator
        validators = [*(validators or []), validator_cls(*choices)]
        super().__init__(
            default=self.parse_value(default) if default else NotSet,
            *args,
            required=required,
            validators=validators,
            **kwargs,
        )

    def parse_value(self, value):
        if value is NotSet and not self.required:
            return self.get_default_value()
        try:
            # noinspection PyArgumentList
            return self.__enum(value)
        except ValueError:
            return value

    def to_struct(self, value):
        return super().to_struct(value.value)


class JsonSerializableMixin:
    def to_json(self: ModelBase):
        return dumps(self.to_struct())

    @classmethod
    def from_json(cls: Type[ModelBase], s):
        return cls(**loads(s))


def callable_default(cls: Type[fields.BaseField]) -> Type[fields.BaseField]:
    class _Wrapped(cls):
        _callable_default = None

        def get_default_value(self):
            if self._callable_default:
                return self._callable_default()
            return super(_Wrapped, self).get_default_value()

        def __init__(self, *args, default=None, **kwargs):
            if default and callable(default):
                self._callable_default = default
                default = default()
            super(_Wrapped, self).__init__(*args, default=default, **kwargs)

    return _Wrapped


class MongoengineFieldsDict(DictField):
    """
    DictField representing mongoengine field names/value mapping.
    Used to convert mongoengine-style field/subfield notation to user-presentable syntax, including handling update
        operators.
    """

    mongoengine_update_operators = (
        "inc",
        "dec",
        "push",
        "push_all",
        "pop",
        "pull",
        "pull_all",
        "add_to_set",
    )

    @staticmethod
    def _normalize_mongo_value(value):
        if isinstance(value, BaseDocument):
            return value.to_mongo()
        return value

    @classmethod
    def _normalize_mongo_field_path(cls, path, value):
        parts = path.split("__")
        if len(parts) > 1:
            if parts[0] == "set":
                parts = parts[1:]
            elif parts[0] == "unset":
                parts = parts[1:]
                value = None
            elif parts[0] in cls.mongoengine_update_operators:
                return None, None
        return ".".join(parts), cls._normalize_mongo_value(value)

    def parse_value(self, value):
        value = super(MongoengineFieldsDict, self).parse_value(value)
        return {
            k: v
            for k, v in (self._normalize_mongo_field_path(*p) for p in value.items())
            if k is not None
        }
