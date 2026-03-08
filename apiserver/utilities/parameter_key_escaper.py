from boltons.dictutils import OneToOne
from mongoengine.queryset.transform import MATCH_OPERATORS

from apiserver.apierrors import errors


class ParameterKeyEscaper:
    """
    Makes the fields name ready for use with MongoDB and Mongoengine
    . and $ are replaced with their codes
    __ and leading _ are escaped
    Since % is used as an escape character the % is also escaped
    """

    _mapping = OneToOne({".": "%2E", "$": "%24", "__": "%_%_"})

    @classmethod
    def escape(cls, value: str):
        """ Quote a parameter key """
        value = value.strip()
        if not value:
            raise errors.bad_request.ValidationError(
                "Empty key is not allowed"
            )

        value = value.replace("%", "%%")
        for c, r in cls._mapping.items():
            value = value.replace(c, r)

        if value.startswith("_"):
            value = "%_" + value[1:]

        return value

    @classmethod
    def _unescape(cls, value: str):
        for c, r in cls._mapping.inv.items():
            value = value.replace(c, r)
        return value

    @classmethod
    def unescape(cls, value: str):
        """ Unquote a quoted parameter key """
        value = "%".join(map(cls._unescape, value.split("%%")))

        if value.startswith("%_"):
            value = "_" + value[2:]

        return value


def mongoengine_safe(field_name):
    if field_name in MATCH_OPERATORS:
        return field_name + "__"
    return field_name
