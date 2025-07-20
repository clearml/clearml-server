import re
from importlib import import_module
from itertools import chain
from pathlib import Path
from typing import cast, Iterable, List, MutableMapping, Optional, Tuple, Callable

import jsonmodels.models

from apiserver.apierrors import APIError, errors
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.utilities.partial_version import PartialVersion
from .apicall import APICall
from .auth import Identity
from .endpoint import Endpoint
from .errors import MalformedPathError, InvalidVersionError, CallFailedError
from .util import parse_return_stack_on_code
from .validators import (
    validate_data,
    validate_auth,
    validate_role,
    validate_impersonation,
)

log = config.logger(__file__)


class ServiceRepo(object):
    _endpoints: MutableMapping[str, List[Endpoint]] = {}
    """ 
    Registered endpoints, in the format of {endpoint_name: Endpoint)}
    the list of endpoints is sorted by min_version
    """

    _version_required = config.get("apiserver.version.required")
    """ If version is required, parsing will fail for endpoint paths that do not contain a valid version """

    _check_max_version = config.get("apiserver.version.check_max_version")
    """If the check is set, parsing will fail for endpoint request with the version that is grater than the current 
    maximum """

    _max_version = PartialVersion("2.33")
    """ Maximum version number (the highest min_version value across all endpoints) """

    _endpoint_exp = (
        re.compile(
            r"^/?v(?P<endpoint_version>\d+\.?\d+)/(?P<endpoint_name>[a-zA-Z_]\w+\.[a-zA-Z_]\w+)/?$"
        )
        if config.get("apiserver.version.required")
        else re.compile(
            r"^/?(v(?P<endpoint_version>\d+\.?\d+)/)?(?P<endpoint_name>[a-zA-Z_]\w+\.[a-zA-Z_]\w+)/?$"
        )
    )
    """ 
        Endpoint structure expressions. We have two expressions, one with optional version part.
        Constraints for the first (strict) expression:
        1. May start with a leading '/'
        2. Followed by a version number (int or float) preceded by a leading 'v'
        3. Followed by a '/'
        4. Followed by a service name, which must start with an english letter (lower or upper case) or underscore,
            and followed by any number of alphanumeric or underscore characters
        5. Followed by a '.'  
        6. Followed by an action name, which must start with an english letter (lower or upper case) or underscore,
            and followed by any number of alphanumeric or underscore characters  
        7. May end with a leading '/' 
        
        The second (optional version) expression does not require steps 2 and 3. 
    """

    _return_stack = config.get("apiserver.return_stack")
    """ return stack trace on error """

    _return_stack_on_code = parse_return_stack_on_code(
        config.get("apiserver.return_stack_on_code", {})
    )
    """ if 'return_stack' is true and error contains a return code, return stack trace only for these error codes """

    _credentials = config["secure.credentials.apiserver"]
    """ Api Server credentials used for intra-service communication """

    _token = None
    """ Token for internal calls """

    @classmethod
    def _load_from_path(
        cls,
        root_module: Path,
        module_prefix: Optional[str] = None,
        predicate: Optional[Callable[[Path], bool]] = None,
    ):
        log.info(f"Loading services from {str(root_module.absolute())}")
        sub_module = None
        for sub_module in root_module.glob("*"):
            if predicate and not predicate(sub_module):
                continue

            if (
                sub_module.is_file()
                and sub_module.suffix == ".py"
                and not sub_module.stem == "__init__"
            ):
                import_module(
                    ".".join(
                        filter(None, (module_prefix, root_module.stem, sub_module.stem))
                    )
                )

            if sub_module.is_dir() and not sub_module.stem == "__pycache__":
                import_module(
                    ".".join(
                        filter(None, (module_prefix, root_module.stem, sub_module.stem))
                    )
                )

        # leave no trace of the 'sub_module' local
        del sub_module

        cls._max_version = max(
            cls._max_version,
            max(
                ep.min_version
                for ep in cast(Iterable[Endpoint], chain(*cls._endpoints.values()))
            ),
        )

    @classmethod
    def load(cls, root_module="services"):
        cls._load_from_path(
            root_module=Path(__file__).parents[1] / root_module,
            module_prefix="apiserver",
        )

    @classmethod
    def register(cls, endpoint: Endpoint):
        if cls._endpoints.get(endpoint.name):
            if any(
                ep.min_version == endpoint.min_version
                for ep in cls._endpoints[endpoint.name]
            ):
                raise Exception(
                    f"Trying to register an existing endpoint. name={endpoint.name}, version={endpoint.min_version}"
                )
            else:
                cls._endpoints[endpoint.name].append(endpoint)
        else:
            cls._endpoints[endpoint.name] = [endpoint]

        cls._endpoints[endpoint.name].sort(key=lambda ep: ep.min_version, reverse=True)

    @classmethod
    def endpoint_names(cls):
        return sorted(cls._endpoints.keys())

    @classmethod
    def endpoints_summary(cls):
        return {
            "endpoints": {
                name: list(map(Endpoint.to_dict, eps))
                for name, eps in cls._endpoints.items()
            },
            "models": {},
        }

    @classmethod
    def max_endpoint_version(cls) -> PartialVersion:
        return cls._max_version

    @classmethod
    def _get_endpoint(cls, name, version) -> Optional[Endpoint]:
        versions = cls._endpoints.get(name)
        if not versions:
            return None
        try:
            return next(ep for ep in versions if ep.min_version <= version)
        except StopIteration:
            # no appropriate version found
            return None

    @classmethod
    def _resolve_endpoint_from_call(cls, call: APICall) -> Optional[Endpoint]:
        endpoint = cls._get_endpoint(
            call.endpoint_name, call.requested_endpoint_version
        )
        if endpoint is None:
            call.log_api = False
            call.set_error_result(
                msg=(
                    f"Unable to find endpoint for name {call.endpoint_name} "
                    f"and version {call.requested_endpoint_version}"
                ),
                code=404,
                subcode=0,
            )
            return

        call.actual_endpoint_version = endpoint.min_version
        call.requires_authorization = endpoint.authorize
        return endpoint

    @classmethod
    def parse_endpoint_path(cls, path: str) -> Tuple[PartialVersion, str]:
        """ Parse endpoint version, service and action from request path. """
        m = cls._endpoint_exp.match(path)
        if not m:
            raise MalformedPathError("Invalid request path %s" % path)
        endpoint_name = m.group("endpoint_name")
        version = m.group("endpoint_version")
        if version is None:
            # If endpoint is available, use the max version
            version = cls._max_version
        else:
            try:
                version = PartialVersion(version)
            except ValueError as e:
                raise errors.bad_request.RequestPathHasInvalidVersion(
                    version=version, reason=e
                )
            if cls._check_max_version and version > cls._max_version:
                raise InvalidVersionError(
                    f"Invalid API version (max. supported version is {cls._max_version})"
                )
        return version, endpoint_name

    @classmethod
    def _should_return_stack(cls, code: int, subcode: int) -> bool:
        if not cls._return_stack or code not in cls._return_stack_on_code:
            return False
        if subcode is None:
            # Code in dict, but no subcode. We'll allow it.
            return True
        subcode_list = cls._return_stack_on_code.get(code)
        if subcode_list is None:
            # if the code is there but we don't have any subcode list, always return stack
            return True
        return subcode in subcode_list

    @classmethod
    def _get_identity(
        cls, call: APICall, endpoint: Endpoint = None, ignore_error: bool = False
    ) -> Optional[Identity]:
        authorize = endpoint and endpoint.authorize
        if ignore_error or not authorize:
            try:
                return call.identity
            except Exception:
                return None
        return call.identity

    @classmethod
    def _get_company(
        cls, call: APICall, endpoint: Endpoint = None, ignore_error: bool = False
    ) -> Optional[str]:
        identity = cls._get_identity(call, endpoint=endpoint, ignore_error=ignore_error)
        return None if identity is None else identity.company

    @classmethod
    def handle_call(cls, call: APICall, load_data_callback: Callable = None):
        company = None
        try:
            if call.failed:
                raise CallFailedError()

            endpoint = cls._resolve_endpoint_from_call(call)

            if call.failed:
                raise CallFailedError()

            validate_auth(endpoint, call)
            validate_role(endpoint, call)
            if validate_impersonation(endpoint, call):
                # if impersonating, validate role again
                validate_role(endpoint, call)

            if load_data_callback:
                load_data_callback(call)
                if call.failed:
                    raise CallFailedError()

            validate_data(call, endpoint)

            if call.failed:
                raise CallFailedError()

            # In case call does not require authorization, parsing the identity.company might raise an exception
            company = cls._get_company(call, endpoint)

            with translate_errors_context():
                ret = endpoint.func(call, company, call.data_model)

            # allow endpoints to return dict or model (instead of setting them explicitly on the call)
            if ret is not None:
                if isinstance(ret, jsonmodels.models.Base):
                    call.result.data_model = ret
                elif isinstance(ret, dict):
                    call.result.data = ret

        except APIError as ex:
            # report stack trace only for gene
            include_stack = cls._should_return_stack(
                ex.code, ex.subcode
            )
            call.set_error_result(
                code=ex.code,
                subcode=ex.subcode,
                msg=str(ex),
                include_stack=include_stack,
            )
        except CallFailedError:
            # Do nothing, let 'finally' wrap up
            pass
        except Exception as ex:
            log.exception(ex)
            include_stack = cls._should_return_stack(
                500, 0
            )
            call.set_error_result(
                code=500, subcode=0, msg=str(ex), include_stack=include_stack
            )
        finally:
            content, content_type = call.get_response()
            call.mark_end()

            console_msg = f"Returned {call.result.code} for {call.endpoint_name} in {call.duration}ms"
            if call.result.code < 300:
                log.info(console_msg)
            else:
                console_msg = f"{console_msg}, msg={call.result.msg}"
                if call.result.code < 500:
                    log.warn(console_msg)
                else:
                    log.error(console_msg)

        return content, content_type, company
