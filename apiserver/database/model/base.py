import re
from collections import defaultdict
from datetime import datetime
from functools import reduce, partial
from typing import (
    Collection,
    Sequence,
    Union,
    Optional,
    Type,
    Tuple,
    Mapping,
    Any,
    Callable,
    Generator,
)

import attr
from boltons.iterutils import first, partition
from dateutil.parser import parse as parse_datetime
from mongoengine import Q, Document, ListField, StringField, IntField, QuerySet
from pymongo.command_cursor import CommandCursor

from apiserver.apierrors import errors, APIError
from apiserver.apierrors.base import BaseError
from apiserver.apierrors.errors.bad_request import FieldsValueError
from apiserver.bll.redis_cache_manager import RedisCacheManager
from apiserver.config_repo import config
from apiserver.database import Database
from apiserver.database.errors import MakeGetAllQueryError
from apiserver.database.projection import ProjectionHelper
from apiserver.database.props import PropsMixin
from apiserver.database.query import RegexQ, RegexWrapper, RegexQCombination
from apiserver.database.utils import (
    get_company_or_none_constraint,
    get_fields_choices,
    field_does_not_exist,
    field_exists,
)
from apiserver.redis_manager import redman
from apiserver.utilities.dicts import project_dict, exclude_fields_from_dict

log = config.logger("dbmodel")
mongo_conf = config.get("services._mongo")
ACCESS_REGEX = re.compile(r"^(?P<prefix>>=|>|<=|<)?(?P<value>.*)$")
ACCESS_MODIFIER = {">=": "gte", ">": "gt", "<=": "lte", "<": "lt"}

ABSTRACT_FLAG = {"abstract": True}


class AuthDocument(Document):
    meta = ABSTRACT_FLAG


class ProperDictMixin(object):
    def to_proper_dict(
        self: Union["ProperDictMixin", Document],
        strip_private=True,
        only=None,
        extra_dict=None,
        exclude=None,
    ) -> dict:
        return self.properize_dict(
            self.to_mongo(use_db_field=False).to_dict(),
            strip_private=strip_private,
            only=only,
            extra_dict=extra_dict,
            exclude=exclude,
        )

    @classmethod
    def properize_dict(
        cls,
        d,
        strip_private=True,
        only=None,
        extra_dict=None,
        exclude=None,
        normalize_id=True,
    ):
        res = d
        if normalize_id and "_id" in res:
            res["id"] = res.pop("_id")
        if strip_private:
            res = {k: v for k, v in res.items() if k[0] != "_"}
        if only:
            res = project_dict(res, only)
        if extra_dict:
            res.update(extra_dict)
        if exclude:
            exclude_fields_from_dict(res, exclude)

        return res


class GetMixin(PropsMixin):
    _text_score = "$text_score"
    _projection_key = "projection"
    _ordering_key = "order_by"
    _search_text_key = "search_text"

    _start_key = "start"
    _size_key = "size"

    _multi_field_param_sep = "__"
    _multi_field_param_prefix = {
        ("_any_", "_or_"): lambda a, b: a | b,
        ("_all_", "_and_"): lambda a, b: a & b,
    }

    @attr.s(auto_attribs=True)
    class MultiFieldParameters:
        fields: Sequence[str]
        pattern: str = None
        datetime: Union[list, str] = None

        def __attrs_post_init__(self):
            if not any(f is not None for f in (self.pattern, self.datetime)):
                raise ValueError("Either 'pattern' or 'datetime' should be provided")
            if all(f is not None for f in (self.pattern, self.datetime)):
                raise ValueError("Only one of the 'pattern' and 'datetime' can be provided")

    _numeric_locale = {"locale": "en_US", "numericOrdering": True}
    _field_collation_overrides = {}

    class QueryParameterOptions(object):
        def __init__(
            self,
            pattern_fields=("name",),
            list_fields=("tags", "system_tags", "id"),
            datetime_fields=None,
            fields=None,
            range_fields=None,
        ):
            """
            :param pattern_fields: Fields for which a "string contains" condition should be generated
            :param list_fields: Fields for which a "list contains" condition should be generated
            :param datetime_fields: Fields for which datetime condition should be generated (see ACCESS_MODIFIER)
            :param fields: Fields which which a simple equality condition should be generated (basically filters out all
                other unsupported query fields)
            """
            self.fields = fields
            self.datetime_fields = datetime_fields
            self.list_fields = list_fields
            self.range_fields = range_fields
            self.pattern_fields = pattern_fields

    class NewListFieldBucketHelper:
        op_prefix = "__$"
        _unary_operators = {
            "__$not": False,
        }
        _reset_operator = "__$nop"
        _operators = {
            "__$all": Q.AND,
            "__$and": Q.AND,
            "__$any": Q.OR,
            "__$or": Q.OR,
        }
        default_global_operator = Q.AND
        default_context = Q.OR
        # not_all modifier currently not supported due to the backwards compatibility
        mongo_modifiers = {
            Q.AND: {True: "all", False: "nin"},
            Q.OR: {True: "in", False: "nin"},
        }

        @attr.s(auto_attribs=True)
        class Term:
            operator: str = None
            reset: bool = False
            include: bool = True
            value: str = None

        def __init__(self, field: str, data: Sequence[str], legacy=False):
            self._field = field
            self.allow_empty = False
            self.global_operator = None
            self.actions = defaultdict(list)

            self._support_legacy = legacy
            current_context = self.default_context
            for d in self._get_next_term(data):
                if d.operator is not None:
                    current_context = d.operator
                    self._support_legacy = False
                    if self.global_operator is None:
                        self.global_operator = d.operator
                    continue

                if self.global_operator is None:
                    self.global_operator = self.default_global_operator

                if d.reset:
                    current_context = self.default_context
                    self._support_legacy = legacy
                    continue

                if d.value is None:
                    self.allow_empty = True
                    continue

                self.actions[self.mongo_modifiers[current_context][d.include]].append(
                    d.value
                )

            if self.global_operator is None:
                self.global_operator = self.default_global_operator

        def _get_next_term(self, data: Sequence[str]) -> Generator[Term, None, None]:
            unary_operator = None
            for value in data:
                if value is None:
                    unary_operator = None
                    yield self.Term()
                    continue

                if not isinstance(value, str):
                    raise FieldsValueError(
                        "invalid value type, string expected",
                        field=self._field,
                        value=str(value),
                    )

                if value == self._reset_operator:
                    unary_operator = None
                    yield self.Term(reset=True)
                    continue

                if value.startswith(self.op_prefix):
                    if unary_operator:
                        raise FieldsValueError(
                            "Value is expected after",
                            field=self._field,
                            operator=unary_operator,
                        )
                    if value in self._unary_operators:
                        unary_operator = value
                        continue

                    operator = self._operators.get(value)
                    if operator is None:
                        raise FieldsValueError(
                            "Unsupported operator",
                            field=self._field,
                            operator=value,
                        )
                    yield self.Term(operator=operator)
                    continue

                if (
                    not unary_operator
                    and self._support_legacy
                    and value.startswith("-")
                ):
                    value = value[1:]
                    if not value:
                        raise FieldsValueError(
                            "Missing value after the exclude prefix -",
                            field=self._field,
                            value=value,
                        )
                    yield self.Term(value=value, include=False)
                    continue

                term = self.Term(value=value)
                if unary_operator:
                    term.include = self._unary_operators[unary_operator]
                    unary_operator = None
                yield term

            if unary_operator:
                raise FieldsValueError(
                    "Value is expected after", operator=unary_operator
                )

    get_all_query_options = QueryParameterOptions()

    class GetManyScrollState(ProperDictMixin, Document):
        meta = {"db_alias": Database.backend, "strict": False}

        id = StringField(primary_key=True)
        position = IntField(default=0)

    _cache_manager = None

    @classmethod
    def get_cache_manager(cls):
        if not cls._cache_manager:
            cls._cache_manager = RedisCacheManager(
                state_class=cls.GetManyScrollState,
                redis=redman.connection("apiserver"),
                expiration_interval=mongo_conf.get(
                    "scroll_state_expiration_seconds", 600
                ),
            )

        return cls._cache_manager

    @classmethod
    def get(
        cls: Union["GetMixin", Document],
        company,
        id,
        *,
        _only=None,
        include_public=False,
        **kwargs,
    ) -> "GetMixin":
        q = cls.objects(
            cls._prepare_perm_query(company, allow_public=include_public)
            & Q(id=id, **kwargs)
        )
        if _only:
            q = q.only(*_only)
        return q.first()

    @classmethod
    def prepare_query(
        cls,
        company: str,
        parameters: dict = None,
        parameters_options: QueryParameterOptions = None,
        allow_public=False,
    ):
        """
        Prepare a query object based on the provided query dictionary and various fields.
        :param parameters_options: Specifies options for parsing the parameters (see ParametersOptions)
        :param company: Company ID (required)
        :param allow_public: Allow results from public objects
        :param parameters: Query dictionary (relevant keys are these specified by the various field names parameters).
            Supported parameters:
            - <field_name>: <value> Will query for items with this value in the field (see QueryParameterOptions for
                specific rules on handling values). Only items matching ALL of these conditions will be retrieved.
            - <any|all>: {fields: [<field1>, <field2>, ...], pattern: <pattern>} Will query for items where any or all
                provided fields match the provided pattern.
            - <any|all>: {fields: [<field1>, <field2>, ...], datetime: <datetime condition>} Will query for items where any or all
                provided datetime fields match the provided condition.
        :return: mongoengine.Q query object
        """
        return cls._prepare_query_no_company(
            parameters, parameters_options
        ) & cls._prepare_perm_query(company, allow_public=allow_public)

    @staticmethod
    def _pop_matching_params(
        patterns: Sequence[str], parameters: dict
    ) -> Mapping[str, Any]:
        """
        Pop the parameters that match the specified patterns and return
        the dictionary of matching parameters
        Pop None parameters since they are not the real queries
        """
        if not patterns:
            return {}

        fields = set()
        for pattern in patterns:
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                fields.update(
                    {field for field in parameters if field.startswith(prefix)}
                )
            elif pattern in parameters:
                fields.add(pattern)

        pairs = ((field, parameters.pop(field, None)) for field in fields)
        return {k: v for k, v in pairs if v is not None}

    @classmethod
    def _try_convert_to_numeric(cls, value: Union[str, Sequence[str]]):
        def convert_str(val: str) -> Union[float, str]:
            try:
                return float(val)
            except ValueError:
                return val

        if isinstance(value, str):
            return convert_str(value)

        if isinstance(value, (list, tuple)):
            return [convert_str(v) if isinstance(v, str) else v for v in value]

        return value

    @classmethod
    def _get_fixed_field_value(cls, field: str, value):
        if field.startswith("last_metrics."):
            return cls._try_convert_to_numeric(value)
        return value

    @classmethod
    def _get_dates_query(cls, field: str, data: Union[list, str]) -> Union[Q, dict]:
        """
        Return dates query for the field
        If the data is 2 values array and none of the values starts from dates comparison operations
        then return the simplified range query
        Otherwise return the dictionary of dates conditions
        """
        if not isinstance(data, list):
            data = [data]

        if len(data) == 2 and not any(
                d.startswith(mod)
                for d in data
                if d is not None
                for mod in ACCESS_MODIFIER
        ):
            return cls.get_range_field_query(field, data)

        dict_query = {}
        for d in data:
            m = ACCESS_REGEX.match(d)
            if not m:
                continue

            try:
                value = parse_datetime(m.group("value"))
                prefix = m.group("prefix")
                modifier = ACCESS_MODIFIER.get(prefix)
                f = (
                    field
                    if not modifier
                    else "__".join((field, modifier))
                )
                dict_query[f] = value
            except (ValueError, OverflowError):
                pass

        return dict_query

    @classmethod
    def _prepare_query_no_company(
        cls, parameters=None, parameters_options=QueryParameterOptions()
    ):
        """
        Prepare a query object based on the provided query dictionary and various fields.

        NOTE: BE VERY CAREFUL WITH THIS CALL, as it allows creating queries that span across companies.
        IMPLEMENTATION NOTE: Make sure that inside this function or the functions it depends on RegexQ is always
        used instead of Q. Otherwise we can and up with some combination that is not processed according to
        RegexQ rules
        :param parameters_options: Specifies options for parsing the parameters (see ParametersOptions)
        :param parameters: Query dictionary (relevant keys are these specified by the various field names parameters).
            Supported parameters:
            - <field_name>: <value> Will query for items with this value in the field (see QueryParameterOptions for
                specific rules on handling values). Only items matching ALL of these conditions will be retrieved.
            - <any|all>: {fields: [<field1>, <field2>, ...], pattern: <pattern>} Will query for items where any or all
                provided fields match the provided pattern.
        :return: mongoengine.Q query object
        """
        parameters_options = parameters_options or cls.get_all_query_options
        dict_query = {}
        query = RegexQ()
        field = None
        # noinspection PyBroadException
        try:
            if parameters:
                parameters = {
                    k: cls._get_fixed_field_value(k, v) for k, v in parameters.items()
                }
                filters = parameters.pop("filters", {})
                if not isinstance(filters, dict):
                    raise FieldsValueError(
                        "invalid value type, string expected",
                        field=filters,
                        value=str(filters),
                    )
                opts = parameters_options
                for field in opts.pattern_fields:
                    pattern = parameters.pop(field, None)
                    if pattern:
                        dict_query[field] = RegexWrapper(pattern)

                for field, data in cls._pop_matching_params(
                    patterns=opts.list_fields, parameters=filters
                ).items():
                    query &= cls.get_list_filter_query(field, data)
                    parameters.pop(field, None)

                for field, data in cls._pop_matching_params(
                    patterns=opts.list_fields, parameters=parameters
                ).items():
                    query &= cls.get_list_field_query(field, data)

                for field, data in cls._pop_matching_params(
                    patterns=opts.range_fields, parameters=parameters
                ).items():
                    query &= cls.get_range_field_query(field, data)

                for field, data in cls._pop_matching_params(
                    patterns=opts.fields or [], parameters=parameters
                ).items():
                    if "._" in field or "_." in field:
                        query &= RegexQ(__raw__={field: data})
                    else:
                        dict_query[field.replace(".", "__")] = data

                for field in opts.datetime_fields or []:
                    data = parameters.pop(field, None)
                    if data is not None:
                        dates_q = cls._get_dates_query(field, data)
                        if isinstance(dates_q, Q):
                            query &= dates_q
                        elif isinstance(dates_q, dict):
                            dict_query.update(dates_q)

                for field, value in parameters.items():
                    for keys, func in cls._multi_field_param_prefix.items():
                        if field not in keys:
                            continue
                        try:
                            data = cls.MultiFieldParameters(**value)
                        except Exception:
                            raise MakeGetAllQueryError("incorrect field format", field)
                        if not data.fields:
                            break
                        if data.pattern is not None:
                            if any("._" in f for f in data.fields):
                                q = reduce(
                                    lambda a, x: func(
                                        a,
                                        RegexQ(
                                            __raw__={
                                                x: {"$regex": data.pattern, "$options": "i"}
                                            }
                                        ),
                                    ),
                                    data.fields,
                                    RegexQ(),
                                )
                            else:
                                regex = RegexWrapper(data.pattern, flags=re.IGNORECASE)
                                sep_fields = [f.replace(".", "__") for f in data.fields]
                                q = reduce(
                                    lambda a, x: func(a, RegexQ(**{x: regex})),
                                    sep_fields,
                                    RegexQ(),
                                )
                        else:
                            date_fields = [field for field in data.fields if field in opts.datetime_fields]
                            if not date_fields:
                                break

                            q = Q()
                            for date_f in date_fields:
                                dates_q = cls._get_dates_query(date_f, data.datetime)
                                if isinstance(dates_q, dict):
                                    dates_q = RegexQ(**dates_q)
                                q = func(q, dates_q)

                        query = query & q
        except APIError:
            raise
        except Exception as ex:
            raise errors.bad_request.FieldsValueError(
                "failed parsing query field",
                error=str(ex),
                **({"field": field} if field else {}),
            )

        return query & RegexQ(**dict_query)

    @classmethod
    def get_range_field_query(cls, field: str, data: Sequence[Optional[str]]) -> RegexQ:
        """
        Return a range query for the provided field. The data should contain min and max values
        Both intervals are included. For open range queries either min or max can be None
        In case the min value is None the records with missing or None value from db are included
        """
        if not isinstance(data, (list, tuple)) or len(data) != 2:
            raise errors.bad_request.ValidationError(
                f"Min and max values should be specified for range field {field}"
            )

        min_val, max_val = data
        if min_val is None and max_val is None:
            raise errors.bad_request.ValidationError(
                f"At least one of min or max values should be provided for field {field}"
            )

        mongoengine_field = field.replace(".", "__")
        query = {}
        if min_val is not None:
            query[f"{mongoengine_field}__gte"] = min_val
        if max_val is not None:
            query[f"{mongoengine_field}__lte"] = max_val

        q = RegexQ(**query)
        if min_val is None:
            q |= RegexQ(**{mongoengine_field: None})

        return q

    @attr.s(auto_attribs=True)
    class ListQueryFilter:
        """
        Deserialize filters data and build db_query object that represents it with the corresponding
        mongo engine operations
        Each part has include and exclude lists that map to mongoengine operations as following:
        "any"
          - include -> 'in'
          - exclude -> 'not_all'
          - combined by 'or' operation
        "all"
          - include -> 'all'
          - exclude -> 'nin'
          - combined by 'and' operation
        "op" optional parameter for combining "and" and "all" parts. Can be "and" or "or". The default is "and"
        """

        _and_op = "and"
        _or_op = "or"
        _allowed_op = [_and_op, _or_op]
        _db_modifiers: Mapping = {
            (Q.OR, True): "in",
            (Q.OR, False): "not__all",
            (Q.AND, True): "all",
            (Q.AND, False): "nin",
        }

        @attr.s(auto_attribs=True)
        class ListFilter:
            include: Sequence[str] = []
            exclude: Sequence[str] = []

            @classmethod
            def from_dict(cls, d: Mapping):
                if d is None:
                    return None
                return cls(**d)

        any: ListFilter = attr.ib(converter=ListFilter.from_dict, default=None)
        all: ListFilter = attr.ib(converter=ListFilter.from_dict, default=None)
        op: str = attr.ib(default="and")
        db_query: dict = attr.ib(init=False)

        # noinspection PyUnresolvedReferences
        @op.validator
        def op_validator(self, _, value):
            if value not in self._allowed_op:
                raise ValueError(
                    f"Invalid list query filter operator: {value}. "
                    f"Should be one of {str(self._allowed_op)}"
                )

        @property
        def and_op(self) -> bool:
            return self.op == self._and_op

        def __attrs_post_init__(self):
            self.db_query = {}
            for op, conditions in ((Q.OR, self.any), (Q.AND, self.all)):
                if not conditions:
                    continue

                operations = {}
                for vals, include in (
                    (conditions.include, True),
                    (conditions.exclude, False),
                ):
                    if not vals:
                        continue

                    unique = set(vals)
                    if None in unique:
                        # noinspection PyTypeChecker
                        unique.remove(None)
                        if include:
                            operations["size"] = 0
                        else:
                            operations["not__size"] = 0

                    if not unique:
                        continue

                    operations[self._db_modifiers[(op, include)]] = list(unique)

                self.db_query[op] = operations

        @classmethod
        def from_data(cls, field, data: Mapping):
            if not isinstance(data, dict):
                raise errors.bad_request.ValidationError(
                    "invalid filter for field, dictionary expected",
                    field=field,
                    value=str(data),
                )

            try:
                return cls(**data)
            except Exception as ex:
                raise errors.bad_request.ValidationError(
                    field=field,
                    value=str(ex),
                )

    @classmethod
    def get_list_filter_query(
        cls, field: str, data: Mapping
    ) -> Union[RegexQ, RegexQCombination]:
        if not data:
            return RegexQ()

        filter_ = cls.ListQueryFilter.from_data(field, data)

        mongoengine_field = field.replace(".", "__")
        queries = []
        for op, actions in filter_.db_query.items():
            if not actions:
                continue

            ops = []
            for action, vals in actions.items():
                # cannot just check vals here since 0 is acceptable value
                if vals is None or vals == []:
                    continue

                ops.append(RegexQ(**{f"{mongoengine_field}__{action}": vals}))

            if not ops:
                continue

            if len(ops) == 1:
                queries.extend(ops)
                continue

            queries.append(RegexQCombination(operation=op, children=ops))

        if not queries:
            return RegexQ()
        if len(queries) == 1:
            return queries[0]

        operation = Q.AND if filter_.and_op else Q.OR
        return RegexQCombination(operation=operation, children=queries)

    @classmethod
    def get_list_field_query(cls, field: str, data: Sequence[Optional[str]]) -> RegexQ:
        """
        Get a proper mongoengine Q object that represents an "or" query for the provided values
        with respect to the given list field, with support for "none of empty" in case a None value
        is included.

        - Exclusion can be specified by a leading "-" for each value (API versions <2.8)
            or by a preceding "__$not" value (operator)
        - AND can be achieved using a preceding "__$all" or "__$and" value (operator)
        """
        if not isinstance(data, (list, tuple)):
            data = [data]

        helper = cls.NewListFieldBucketHelper(field, data=data, legacy=True)
        global_op = helper.global_operator
        actions = helper.actions

        mongoengine_field = field.replace(".", "__")

        queries = [
            RegexQ(**{f"{mongoengine_field}__{action}": list(set(values))})
            for action, values in actions.items()
        ]

        if not queries:
            q = RegexQ()
        else:
            q = RegexQCombination(operation=global_op, children=queries)

        if not helper.allow_empty:
            return q

        return (
            q
            | RegexQ(**{f"{mongoengine_field}__exists": False})
            | RegexQ(**{mongoengine_field: []})
            | RegexQ(**{mongoengine_field: None})
        )

    @classmethod
    def _prepare_perm_query(cls, company, allow_public=False):
        if allow_public:
            return get_company_or_none_constraint(company)
        return Q(company=company)

    @classmethod
    def validate_order_by(cls, parameters, search_text) -> Sequence:
        """
        Validate and extract order_by params as a list
        If ordering is specified then make sure that id field is part of it
        This guarantees the unique order when paging
        """
        order_by = parameters.get(cls._ordering_key)
        if not order_by:
            return []

        order_by = order_by if isinstance(order_by, list) else [order_by]
        order_by = [cls._text_score if x == "@text_score" else x for x in order_by]
        if not search_text and cls._text_score in order_by:
            raise errors.bad_request.FieldsValueError(
                "text score cannot be used in order_by when search text is not used"
            )

        if not any(id_field in order_by for id_field in ("id", "-id")):
            order_by.append("id")

        return order_by

    @classmethod
    def validate_paging(cls, parameters=None, default_page=0, default_page_size=None):
        """
        Validate and extract paging info from from the provided dictionary. Supports default values.
        If page is specified then it should be non-negative, if page size is specified then it should be positive
        If page size is specified and page is not then 0 page is assumed
        If page is specified then page size should be specified too
        """
        parameters = parameters or {}

        start = parameters.get(cls._start_key)
        if start is not None:
            return start, cls.validate_scroll_size(parameters)

        max_page_size = mongo_conf.get("max_page_size", 500)
        page = parameters.get("page", default_page)
        if page is not None and page < 0:
            raise errors.bad_request.ValidationError("page must be >=0", field="page")

        page_size = parameters.get("page_size", default_page_size or max_page_size)
        if page_size is not None and page_size < 1:
            raise errors.bad_request.ValidationError(
                "page_size must be >0", field="page_size"
            )

        if page_size is not None:
            page = page or 0
            page_size = min(page_size, max_page_size)
            return page * page_size, page_size

        if page is not None:
            raise errors.bad_request.MissingRequiredFields(
                "page_size is required when page is requested", field="page_size"
            )

        return None, None

    @classmethod
    def get_projection(cls, parameters, override_projection=None, **__):
        """Extract a projection list from the provided dictionary. Supports an override projection."""
        if override_projection is not None:
            return override_projection
        if not parameters:
            return []
        return parameters.get(cls._projection_key) or parameters.get("only_fields", [])

    @classmethod
    def split_projection(
        cls, projection: Sequence[str]
    ) -> Tuple[Collection[str], Collection[str]]:
        """Return include and exclude lists based on passed projection and class definition"""
        if projection:
            include, exclude = partition(
                projection,
                key=lambda x: x[0] != ProjectionHelper.exclusion_prefix,
            )
        else:
            include, exclude = [], []
        exclude = {x.lstrip(ProjectionHelper.exclusion_prefix) for x in exclude}
        return include, set(cls.get_exclude_fields()).union(exclude).difference(include)

    @classmethod
    def set_projection(cls, parameters: dict, value: Sequence[str]) -> Sequence[str]:
        parameters.pop("only_fields", None)
        parameters[cls._projection_key] = value
        return value

    @classmethod
    def get_ordering(cls, parameters: dict) -> Optional[Sequence[str]]:
        return parameters.get(cls._ordering_key)

    @classmethod
    def set_ordering(cls, parameters: dict, value: Sequence[str]) -> Sequence[str]:
        parameters[cls._ordering_key] = value
        return value

    @classmethod
    def set_default_ordering(cls, parameters: dict, value: Sequence[str]) -> None:
        cls.set_ordering(parameters, cls.get_ordering(parameters) or value)

    @classmethod
    def validate_scroll_size(cls, query_dict: dict) -> int:
        size = query_dict.get(cls._size_key)
        if not size or not isinstance(size, int) or size < 1:
            raise errors.bad_request.ValidationError(
                "Integer size parameter greater than 1 should be provided when working with scroll"
            )
        return size

    @classmethod
    def get_data_with_scroll_support(
        cls,
        query_dict: dict,
        data_getter: Callable[[], Sequence[dict]],
        ret_params: dict,
    ) -> Sequence[dict]:
        """
        Retrieves the data by calling the provided data_getter api
        If scroll parameters are specified then put the query_dict 'start' parameter to the last
        scroll position and continue retrievals from that position
        If refresh_scroll is requested then bring once more the data from the beginning
        till the current scroll position
        In the end the scroll position is updated and accumulated frames are returned
        """
        query_dict = query_dict or {}
        state: Optional[cls.GetManyScrollState] = None
        if "scroll_id" in query_dict:
            size = cls.validate_scroll_size(query_dict)
            state = cls.get_cache_manager().get_or_create_state_core(
                query_dict.get("scroll_id")
            )
            if query_dict.get("refresh_scroll"):
                query_dict[cls._size_key] = max(state.position, size)
                state.position = 0
            query_dict[cls._start_key] = state.position

        data = data_getter()
        if cls._start_key in query_dict:
            query_dict[cls._start_key] = query_dict[cls._start_key] + len(data)

        if state:
            state.position = query_dict[cls._start_key]
            cls.get_cache_manager().set_state(state)
            if ret_params is not None:
                ret_params["scroll_id"] = state.id

        return data

    @classmethod
    def get_many_with_join(
        cls,
        company,
        query_dict=None,
        query_options=None,
        query=None,
        allow_public=False,
        override_projection=None,
        expand_reference_ids=True,
        projection_fields: dict = None,
        ret_params: dict = None,
    ):
        """
        Fetch all documents matching a provided query with support for joining referenced documents according to the
        requested projection. See get_many() for more info.
        :param expand_reference_ids: If True, reference fields that contain just an ID string are expanded into
            a sub-document in the format {_id: <ID>}. Otherwise, field values are left as a string.
        """
        if issubclass(cls, AuthDocument):
            # Refuse projection (join) for auth documents (auth.User etc.) to avoid inadvertently disclosing
            # auth-related secrets and prevent security leaks
            log.error(
                f"Attempted projection of {cls.__name__} auth document (ignored)",
                stack_info=True,
            )
            return []

        override_projection = cls.get_projection(
            parameters=query_dict, override_projection=override_projection
        )

        helper = ProjectionHelper(
            doc_cls=cls,
            projection=override_projection,
            expand_reference_ids=expand_reference_ids,
        )

        # Make the main query
        results = cls.get_many(
            override_projection=helper.doc_projection,
            company=company,
            parameters=query_dict,
            query_dict=query_dict,
            query=query,
            query_options=query_options,
            allow_public=allow_public,
            projection_fields=projection_fields,
            ret_params=ret_params,
        )

        def projection_func(doc_type, projection, ids):
            return doc_type.get_many_with_join(
                company=company,
                override_projection=projection,
                query=Q(id__in=ids),
                expand_reference_ids=expand_reference_ids,
                allow_public=allow_public,
            )

        return helper.project(results, projection_func)

    @classmethod
    def _get_collation_override(cls, field: str) -> Optional[dict]:
        return first(
            v
            for k, v in cls._field_collation_overrides.items()
            if field.startswith(k) or field.startswith(f"-{k}")
        )

    @classmethod
    def get_count(
        cls: Union["GetMixin", Document],
        company,
        query_dict: dict = None,
        query_options: QueryParameterOptions = None,
        query: Q = None,
        allow_public=False,
    ) -> int:
        _query = cls._get_combined_query(
            company=company,
            query_dict=query_dict,
            query_options=query_options,
            query=query,
            allow_public=allow_public,
        )
        return cls.objects(_query).count()

    @classmethod
    def _get_combined_query(
        cls,
        company,
        query_dict: dict = None,
        query_options: QueryParameterOptions = None,
        query: Q = None,
        allow_public=False,
    ) -> Q:
        if query_dict is not None:
            q = cls.prepare_query(
                parameters=query_dict,
                company=company,
                parameters_options=query_options,
                allow_public=allow_public,
            )
        else:
            q = cls._prepare_perm_query(company, allow_public=allow_public)

        return (q & query) if query else q

    @classmethod
    def get_many(
        cls,
        company,
        parameters: dict = None,
        query_dict: dict = None,
        query_options: QueryParameterOptions = None,
        query: Q = None,
        allow_public=False,
        override_projection: Collection[str] = None,
        return_dicts=True,
        projection_fields: dict = None,
        ret_params: dict = None,
    ):
        """
        Fetch all documents matching a provided query. Supported several built-in options
         (aside from those provided by the parameters):
            - Ordering: using query field `order_by` which can contain a string or a list of strings corresponding to
                    field names. Using field names not defined in the document will cause an error.
            - Paging: using query fields page and page_size. page must be larger than or equal to 0, page_size must be
                    larger than 0 and is required when specifying a page.
            - Text search: using query field `search_text`. If used, text score can be used in the ordering, using the
                    `@text_score` keyword. A text index must be defined on the document type, otherwise an error will
                    be raised.
        :param return_dicts: Return a list of dictionaries. If True, a list of dicts is returned (if projection was
            requested, each contains only the requested projection). If False, a QuerySet object is returned
            (lazy evaluated). If return_dicts is requested then the entities with the None value in order_by field
            are returned last in the ordering.
        :param company: Company ID (required)
        :param parameters: Parameters dict from which paging ordering and searching parameters are extracted.
        :param query_dict: If provided, passed to prepare_query() along with all of the relevant arguments to produce
            a query. The resulting query is AND'ed with the `query` parameter (if provided).
        :param query_options: query parameters options (see ParametersOptions)
        :param query: Optional query object (mongoengine.Q)
        :param override_projection: A list of projection fields overriding any projection specified in the `param_dict`
            argument
        :param allow_public: If True, objects marked as public (no associated company) are also queried.
        :return: A list of objects matching the query.
        """
        override_collation = None
        if query_dict:
            for field in query_dict:
                override_collation = cls._get_collation_override(field)
                if override_collation:
                    break

        _query = cls._get_combined_query(
            company=company,
            query_dict=query_dict,
            query_options=query_options,
            query=query,
            allow_public=allow_public,
        )

        if return_dicts:
            data_getter = partial(
                cls._get_many_override_none_ordering,
                query=_query,
                parameters=parameters,
                override_projection=override_projection,
                override_collation=override_collation,
                projection_fields=projection_fields,
            )
            return cls.get_data_with_scroll_support(
                query_dict=query_dict,
                data_getter=data_getter,
                ret_params=ret_params,
            )

        return cls._get_many_no_company(
            query=_query,
            parameters=parameters,
            override_projection=override_projection,
            override_collation=override_collation,
            projection_fields=projection_fields,
        )

    @classmethod
    def get_many_public(
        cls,
        query: Q = None,
        projection: Collection[str] = None,
    ):
        """
        Fetch all public documents matching a provided query.
        :param query: Optional query object (mongoengine.Q).
        :param projection: A list of projection fields.
        :return: A list of documents matching the query.
        """
        q = get_company_or_none_constraint()
        _query = (q & query) if query else q

        return cls._get_many_no_company(query=_query, override_projection=projection)

    @staticmethod
    def _get_qs_with_ordering(qs: QuerySet, order_by: Sequence):
        disk_use_setting = mongo_conf.get("allow_disk_use.sort", None)
        if disk_use_setting is not None:
            qs = qs.allow_disk_use(disk_use_setting)
        return qs.order_by(*order_by)

    @classmethod
    def _get_many_no_company(
        cls: Union["GetMixin", Document],
        query: Q,
        parameters=None,
        override_projection=None,
        override_collation=None,
        projection_fields: dict = None,
    ):
        """
        Fetch all documents matching a provided query.
        This is a company-less version for internal uses. We assume the caller has either added any necessary
        constraints to the query or that no constraints are required.

        NOTE: BE VERY CAREFUL WITH THIS CALL, as it allows returning data across companies.

        :param query: Query object (mongoengine.Q)
        :param parameters: Parameters dict from which paging ordering and searching parameters are extracted.
        :param override_projection: A list of projection fields overriding any projection specified in the `param_dict`
            argument
        """
        if not query:
            raise ValueError("query or call_data must be provided")

        parameters = parameters or {}
        search_text = parameters.get(cls._search_text_key)
        order_by = cls.validate_order_by(parameters=parameters, search_text=search_text)
        if order_by and not override_collation:
            override_collation = cls._get_collation_override(order_by[0])
        start, size = cls.validate_paging(parameters=parameters)
        include, exclude = cls.split_projection(
            cls.get_projection(parameters, override_projection)
        )

        qs = cls.objects(query)
        if override_collation:
            qs = qs.collation(collation=override_collation)
        if search_text:
            qs = qs.search_text(search_text)
        if order_by:
            # add ordering
            qs = cls._get_qs_with_ordering(qs, order_by)

        if include:
            # add projection
            qs = qs.only(*include)

        if exclude:
            qs = qs.exclude(*exclude)

        if projection_fields:
            qs = qs.fields(**projection_fields)

        if start is not None and size:
            # add paging
            qs = qs.skip(start).limit(size)

        return qs

    @classmethod
    def _get_queries_for_order_field(
        cls, query: Q, order_field: str
    ) -> Union[None, Tuple[Q, Q]]:
        """
        In case the order_field is one of the cls fields and the sorting is ascending
        then return the tuple of 2 queries:
        1. original query with not empty constraint on the order_by field
        2. original query with empty constraint on the order_by field
        """
        if not order_field or order_field.startswith("-") or "[" in order_field:
            return

        mongo_field_name = order_field.replace(".", "__")
        mongo_field = first(
            v for k, v in cls.get_all_fields_with_instance() if k == mongo_field_name
        )

        if isinstance(mongo_field, ListField):
            params = {"is_list": True}
        elif isinstance(mongo_field, StringField):
            params = {"empty_value": ""}
        else:
            params = {}
        non_empty = query & field_exists(mongo_field_name, **params)
        empty = query & field_does_not_exist(mongo_field_name, **params)
        return non_empty, empty

    @classmethod
    def _get_many_override_none_ordering(
        cls: Union[Document, "GetMixin"],
        query: Q = None,
        parameters: dict = None,
        override_projection: Collection[str] = None,
        override_collation: dict = None,
        projection_fields: dict = None,
    ) -> Sequence[dict]:
        """
        Fetch all documents matching a provided query. For the first order by field
        the None values are sorted in the end regardless of the sorting order.
        If the first order field is a user defined parameter (either from execution.parameters,
        or from last_metrics) then the collation is set that sorts strings in numeric order where possible.
        This is a company-less version for internal uses. We assume the caller has either added any necessary
        constraints to the query or that no constraints are required.

        NOTE: BE VERY CAREFUL WITH THIS CALL, as it allows returning data across companies.

        :param query: Query object (mongoengine.Q)
        :param parameters: Parameters dict from which paging ordering and searching parameters are extracted.
        :param override_projection: A list of projection fields overriding any projection specified in the `param_dict`
            argument
        """
        if not query:
            raise ValueError("query or call_data must be provided")

        parameters = parameters or {}
        search_text = parameters.get(cls._search_text_key)
        order_by = cls.validate_order_by(parameters=parameters, search_text=search_text)
        start, size = cls.validate_paging(parameters=parameters)
        if size is not None and size <= 0:
            return []

        include, exclude = cls.split_projection(
            cls.get_projection(parameters, override_projection)
        )

        query_sets = [cls.objects(query)]
        if order_by:
            order_field = first(
                field for field in order_by if not field.startswith("$")
            )
            res = cls._get_queries_for_order_field(query, order_field)
            if res:
                query_sets = [cls.objects(q) for q in res]
            query_sets = [cls._get_qs_with_ordering(qs, order_by) for qs in query_sets]
            if order_field and not override_collation:
                override_collation = cls._get_collation_override(order_field)

        if override_collation:
            query_sets = [
                qs.collation(collation=override_collation) for qs in query_sets
            ]

        if search_text:
            query_sets = [qs.search_text(search_text) for qs in query_sets]

        if include:
            # add projection
            query_sets = [qs.only(*include) for qs in query_sets]

        if exclude:
            query_sets = [qs.exclude(*exclude) for qs in query_sets]

        if projection_fields:
            query_sets = [qs.fields(**projection_fields) for qs in query_sets]

        if start is None or not size:
            return [
                obj.to_proper_dict(only=include, exclude=exclude)
                for qs in query_sets
                for obj in qs
            ]

        # add paging
        ret = []
        last_set = len(query_sets) - 1
        for i, qs in enumerate(query_sets):
            last_size = len(ret)
            ret.extend(
                obj.to_proper_dict(only=include, exclude=exclude)
                for obj in (qs.skip(start) if start else qs).limit(size)
            )
            added = len(ret) - last_size

            if added > 0:
                start = 0
                size = max(0, size - added)
            elif i != last_set:
                start -= min(start, qs.count())

            if size <= 0:
                break

        return ret

    @classmethod
    def get_for_writing(
        cls, *args, _only: Collection[str] = None, **kwargs
    ) -> "GetMixin":
        if _only and "company" not in _only:
            _only = list(set(_only) | {"company"})
        result = cls.get(*args, _only=_only, include_public=True, **kwargs)
        if result and not result.company:
            object_name = cls.__name__.lower()
            raise errors.forbidden.NoWritePermission(
                f"cannot modify public {object_name}(s), ids={(result.id,)}"
            )
        return result


class UpdateMixin(object):
    __user_set_allowed_fields = None
    __locked_when_published_fields = None

    @classmethod
    def user_set_allowed(cls):
        if cls.__user_set_allowed_fields is None:
            cls.__user_set_allowed_fields = dict(
                get_fields_choices(cls, "user_set_allowed")
            )
        return cls.__user_set_allowed_fields

    @classmethod
    def locked_when_published(cls):
        if cls.__locked_when_published_fields is None:
            cls.__locked_when_published_fields = dict(
                get_fields_choices(cls, "locked_when_published")
            )
        return cls.__locked_when_published_fields

    @classmethod
    def get_safe_update_dict(cls, fields):
        if not fields:
            return {}
        valid_fields = cls.user_set_allowed()
        fields = [(k, v, fields[k]) for k, v in valid_fields.items() if k in fields]
        update_dict = {
            field: value
            for field, allowed, value in fields
            if allowed is None
            or (
                (value in allowed)
                if not isinstance(value, list)
                else all(v in allowed for v in value)
            )
        }
        return update_dict

    @classmethod
    def safe_update(
        cls: Union["UpdateMixin", Document],
        company_id,
        id,
        partial_update_dict,
        injected_update=None,
    ):
        update_dict = cls.get_safe_update_dict(partial_update_dict)
        if not update_dict:
            return 0, {}
        if injected_update:
            update_dict.update(injected_update)
        update_count = cls.objects(id=id, company=company_id).update(
            upsert=False, **update_dict
        )
        return update_count, update_dict


class DbModelMixin(GetMixin, ProperDictMixin, UpdateMixin):
    """Provide convenience methods for a subclass of mongoengine.Document"""

    @classmethod
    def aggregate(
        cls: Union["DbModelMixin", Document],
        pipeline: Sequence[dict],
        allow_disk_use=None,
        **kwargs,
    ) -> CommandCursor:
        """
        Aggregate objects of this document class according to the provided pipeline.
        :param pipeline: a list of dictionaries describing the pipeline stages
        :param allow_disk_use: if True, allow the server to use disk space if aggregation query cannot fit in memory.
        If None, default behavior will be used (see apiserver.conf/mongo/aggregate/allow_disk_use)
        :param kwargs: additional keyword arguments passed to mongoengine
        :return:
        """
        kwargs.update(
            allowDiskUse=allow_disk_use
            if allow_disk_use is not None
            else mongo_conf.get("allow_disk_use.aggregate", True)
        )
        return cls.objects.aggregate(pipeline, **kwargs)

    @classmethod
    def set_public(
        cls: Type[Document],
        company_id: str,
        user_id: str,
        ids: Sequence[str],
        invalid_cls: Type[BaseError],
        enabled: bool = True,
    ):
        if enabled:
            items = list(cls.objects(id__in=ids, company=company_id).only("id"))
            update: dict = dict(set__company_origin=company_id, set__company="")
        else:
            items = list(
                cls.objects(
                    id__in=ids, company="", company_origin=company_id
                ).only("id")
            )
            update: dict = dict(set__company=company_id, unset__company_origin=1)

        if len(items) < len(ids):
            missing = tuple(set(ids).difference(i.id for i in items))
            raise invalid_cls(ids=missing)

        if hasattr(cls, "last_change"):
            update["set__last_change"] = datetime.utcnow()
        if hasattr(cls, "last_changed_by"):
            update["set__last_changed_by"] = user_id

        return {"updated": cls.objects(id__in=ids).update(**update)}


def validate_id(cls, company, **kwargs):
    """
    Validate existence of objects with certain IDs. within company.
    :param cls: Model class to search in
    :param company: Company to search in
    :param kwargs: Mapping of field name to object ID. If any ID does not have a corresponding object,
                    it will be reported along with the name it was assigned to.
    :return:
    """
    ids = set(kwargs.values())
    objs = list(cls.objects(company=company, id__in=ids).only("id"))
    missing = ids - set(x.id for x in objs)
    if not missing:
        return
    id_to_name = {}
    for name, obj_id in kwargs.items():
        id_to_name.setdefault(obj_id, []).append(name)
    raise errors.bad_request.ValidationError(
        "Invalid {} ids".format(cls.__name__.lower()),
        **{name: obj_id for obj_id in missing for name in id_to_name[obj_id]},
    )
