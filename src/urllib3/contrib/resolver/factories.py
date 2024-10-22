from __future__ import annotations

import importlib
import inspect
import typing
from abc import ABCMeta
from base64 import b64encode
from typing import Any
from urllib.parse import parse_qs

from ...util import parse_url
from .protocols import BaseResolver, ProtocolResolver


class ResolverFactory(metaclass=ABCMeta):
    @staticmethod
    def has(
        protocol: ProtocolResolver,
        specifier: str | None = None,
        implementation: str | None = None,
    ) -> bool:
        package_name: str = __name__.split(".")[0]
        module_expr = f".{protocol.value.replace('-', '_')}"

        if implementation:
            module_expr += f"._{implementation.replace('-', '_').lower()}"

        try:
            resolver_module = importlib.import_module(
                module_expr, f"{package_name}.contrib.resolver"
            )
        except ImportError:
            return False

        implementations: list[tuple[str, type[BaseResolver]]] = inspect.getmembers(
            resolver_module,
            lambda e: isinstance(e, type)
            and issubclass(e, BaseResolver)
            and (
                (specifier is None and e.specifier is None) or specifier == e.specifier
            ),
        )

        if not implementations:
            return False

        return True

    @staticmethod
    def new(
        protocol: ProtocolResolver,
        specifier: str | None = None,
        implementation: str | None = None,
        **kwargs: Any,
    ) -> BaseResolver:
        package_name: str = __name__.split(".")[0]

        module_expr = f".{protocol.value.replace('-', '_')}"

        if implementation:
            module_expr += f"._{implementation.replace('-', '_').lower()}"

        spe_msg = " " if specifier is None else f' (w/ specifier "{specifier}") '

        try:
            resolver_module = importlib.import_module(
                module_expr, f"{package_name}.contrib.resolver"
            )
        except ImportError as e:
            raise NotImplementedError(
                f"{protocol}{spe_msg}cannot be loaded. Tried to import '{module_expr}'. Did you specify a non-existent implementation?"
            ) from e

        implementations: list[tuple[str, type[BaseResolver]]] = inspect.getmembers(
            resolver_module,
            lambda e: isinstance(e, type)
            and issubclass(e, BaseResolver)
            and (
                (specifier is None and e.specifier is None) or specifier == e.specifier
            ),
        )

        if not implementations:
            raise NotImplementedError(
                f"{protocol}{spe_msg}cannot be loaded. "
                "No compatible implementation available. "
                "Make sure your implementation inherit from BaseResolver."
            )

        implementation_target: type[BaseResolver] = implementations.pop()[1]

        return implementation_target(**kwargs)


class ResolverDescription:
    """Describe how a BaseResolver must be instantiated."""

    def __init__(
        self,
        protocol: ProtocolResolver,
        specifier: str | None = None,
        implementation: str | None = None,
        server: str | None = None,
        port: int | None = None,
        *host_patterns: str,
        **kwargs: typing.Any,
    ) -> None:
        self.protocol = protocol
        self.specifier = specifier
        self.implementation = implementation
        self.server = server
        self.port = port
        self.host_patterns = host_patterns
        self.kwargs = kwargs

    def __setitem__(self, key: str, value: typing.Any) -> None:
        self.kwargs[key] = value

    def __contains__(self, item: str) -> bool:
        return item in self.kwargs

    def new(self) -> BaseResolver:
        kwargs = {**self.kwargs}

        if self.server:
            kwargs["server"] = self.server
        if self.port:
            kwargs["port"] = self.port
        if self.host_patterns:
            kwargs["patterns"] = self.host_patterns

        return ResolverFactory.new(
            self.protocol,
            self.specifier,
            self.implementation,
            **kwargs,
        )

    @staticmethod
    def from_url(url: str) -> ResolverDescription:
        parsed_url = parse_url(url)

        schema = parsed_url.scheme

        if schema is None:
            raise ValueError("Given DNS url is missing a protocol")

        specifier = None
        implementation = None

        if "+" in schema:
            schema, specifier = tuple(schema.lower().split("+", 1))

        protocol = ProtocolResolver(schema)
        kwargs: dict[str, typing.Any] = {}

        if parsed_url.path:
            kwargs["path"] = parsed_url.path

        if parsed_url.auth:
            kwargs["headers"] = dict()
            if ":" in parsed_url.auth:
                username, password = parsed_url.auth.split(":")

                username = username.strip("'\"")
                password = password.strip("'\"")

                kwargs["headers"][
                    "Authorization"
                ] = f"Basic {b64encode(f'{username}:{password}'.encode()).decode()}"
            else:
                kwargs["headers"]["Authorization"] = f"Bearer {parsed_url.auth}"

        if parsed_url.query:
            parameters = parse_qs(parsed_url.query)

            for parameter in parameters:
                if not parameters[parameter]:
                    continue

                parameter_insensible = parameter.lower()

                if (
                    isinstance(parameters[parameter], list)
                    and len(parameters[parameter]) > 1
                ):
                    if parameter == "implementation":
                        raise ValueError("Only one implementation can be passed to URL")

                    values = []

                    for e in parameters[parameter]:
                        if "," in e:
                            values.extend(e.split(","))
                        else:
                            values.append(e)

                    if parameter_insensible in kwargs:
                        if isinstance(kwargs[parameter_insensible], list):
                            kwargs[parameter_insensible].extend(values)
                        else:
                            values.append(kwargs[parameter_insensible])
                            kwargs[parameter_insensible] = values
                        continue

                    kwargs[parameter_insensible] = values
                    continue

                value: str = parameters[parameter][0].lower().strip(" ")

                if parameter == "implementation":
                    implementation = value
                    continue

                if "," in value:
                    list_of_values = value.split(",")

                    if parameter_insensible in kwargs:
                        if isinstance(kwargs[parameter_insensible], list):
                            kwargs[parameter_insensible].extend(list_of_values)
                        else:
                            list_of_values.append(kwargs[parameter_insensible])
                        continue

                    kwargs[parameter_insensible] = list_of_values
                    continue

                value_converted: bool | int | float | None = None

                if value in ["false", "true"]:
                    value_converted = True if value == "true" else False
                elif value.isdigit():
                    value_converted = int(value)
                elif (
                    value.count(".") == 1
                    and value.index(".") > 0
                    and value.replace(".", "").isdigit()
                ):
                    value_converted = float(value)

                kwargs[parameter_insensible] = (
                    value if value_converted is None else value_converted
                )

        host_patterns: list[str] = []

        if "hosts" in kwargs:
            host_patterns = (
                kwargs["hosts"].split(",")
                if isinstance(kwargs["hosts"], str)
                else kwargs["hosts"]
            )
            del kwargs["hosts"]

        return ResolverDescription(
            protocol,
            specifier,
            implementation,
            parsed_url.host,
            parsed_url.port,
            *host_patterns,
            **kwargs,
        )
