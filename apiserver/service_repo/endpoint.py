from enum import Enum
from typing import Callable, Sequence, Text, Any
from boltons.iterutils import remap
from jsonmodels import models
from jsonmodels.errors import FieldNotSupported

from apiserver.services_schema import schema
from apiserver.utilities.partial_version import PartialVersion
from .apicall import APICall
from .schema_validator import SchemaValidator

EndpointFunc = Callable[[APICall, Text, models.Base], Any]


class Endpoint(object):
    _endpoint_config_cache = {}
    """
    Endpoints configuration cache, in the format of {full endpoint name: dict}
    """

    def __init__(
        self,
        name: Text,
        func: EndpointFunc,
        min_version: Text = "1.0",
        required_fields: Sequence[Text] = None,
        request_data_model: models.Base = None,
        response_data_model: models.Base = None,
        validate_schema: bool = False,
    ):
        """
        Endpoint configuration
        :param name: full endpoint name
        :param func: endpoint implementation
        :param min_version: minimum supported version
        :param required_fields: required request fields, can not be used with validate_schema
        :param request_data_model: request jsonschema model, will be validated if validate_schema=False
        :param response_data_model: response jsonschema model, will be validated if validate_schema=False
        :param validate_schema: whether request and response schema should be validated
        """
        self.name = name
        self.min_version = PartialVersion(min_version)
        self.func = func
        self.required_fields = required_fields
        self.request_data_model = request_data_model
        self.response_data_model = response_data_model
        service, _, endpoint_name = self.name.partition(".")
        try:
            self.endpoint_group = schema.services[service].endpoint_groups[
                endpoint_name
            ]
        except KeyError:
            raise RuntimeError(
                f"schema for endpoint {service}.{endpoint_name} not found"
            )
        if validate_schema:
            if self.required_fields:
                raise ValueError(
                    f"endpoint {self.name}: can not use 'required_fields' with 'validate_schema'"
                )
            endpoint = self.endpoint_group.get_for_version(self.min_version)
            request_schema = endpoint.request_schema
            response_schema = endpoint.response_schema
        else:
            request_schema = None
            response_schema = None
        self.request_schema_validator = SchemaValidator(request_schema)
        self.response_schema_validator = SchemaValidator(response_schema)

    def __repr__(self):
        return f"{type(self).__name__}<{self.name}>"

    def to_dict(self):
        """
        Used by `server.endpoints` endpoint.
        Provided endpoints and their schemas on a best-effort basis.
        """
        d = {
            "min_version": str(self.min_version),
            "required_fields": self.required_fields,
            "request_data_model": None,
            "response_data_model": None,
        }

        def safe_to_json_schema(data_model: models.Base):
            """
            Provided data_model schema if available
            """
            try:
                res = data_model.to_json_schema()

                def visit(path, key, value):
                    if isinstance(value, Enum):
                        value = str(value)
                    return key, value

                return remap(res, visit=visit)
            except (FieldNotSupported, TypeError):
                return str(data_model.__name__)

        if self.request_data_model:
            d["request_data_model"] = safe_to_json_schema(self.request_data_model)
        if self.response_data_model:
            d["response_data_model"] = safe_to_json_schema(self.response_data_model)
        if self.request_schema_validator.enabled:
            d["request_schema"] = self.request_schema_validator.schema
        if self.response_schema_validator.enabled:
            d["response_schema"] = self.response_schema_validator.schema

        return d

    @property
    def authorize(self):
        return self.endpoint_group.authorize

    @property
    def allow_roles(self):
        return self.endpoint_group.allow_roles

    def allows(self, role):
        return self.endpoint_group.allows(role)

    @property
    def is_internal(self):
        return self.endpoint_group.internal
