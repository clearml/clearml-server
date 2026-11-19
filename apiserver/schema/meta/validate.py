#!/usr/bin/env python
import argparse
import json
import os
import reprlib
import sys
import time
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from typing import OrderedDict, cast

import colorful as cf
import pyhocon
import regex
import yaml
from boltons.iterutils import default_enter, remap
from jsonschema import (
    Draft202012Validator,
    ErrorTree,
)
from jsonschema import (
    ValidationError as JSONSchemaValidationError,
)
from jsonschema.validators import validator_for
from pyparsing import ParseBaseException
from treelib.tree import Tree, Node

LINTER_URL = "https://www.jsonschemavalidator.net/"


class LocalStorage(object):
    def __init__(self, driver):
        self.driver = driver

    def __len__(self):
        return self.driver.execute_script("return window.localStorage.length;")

    def items(self):
        return self.driver.execute_script(
            """
            var ls = window.localStorage, items = {};
            for (var i = 0, k; i < ls.length; ++i)
                items[k = ls.key(i)] = ls.getItem(k);
            return items;
            """
        )

    def keys(self):
        return self.driver.execute_script(
            """
            var ls = window.localStorage, keys = [];
            for (var i = 0; i < ls.length; ++i)
                keys[i] = ls.key(i);
            return keys;
            """
        )

    def get(self, key):
        return self.driver.execute_script(
            "return window.localStorage.getItem(arguments[0]);", key
        )

    def remove(self, key):
        self.driver.execute_script("window.localStorage.removeItem(arguments[0]);", key)

    def clear(self):
        self.driver.execute_script("window.localStorage.clear();")

    def __getitem__(self, key):
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self.driver.execute_script(
            "window.localStorage.setItem(arguments[0], arguments[1]);", key, value
        )

    def __contains__(self, key):
        return key in self.keys()

    def __iter__(self):
        return iter(self.keys())

    def __repr__(self):
        return repr(self.items())


class ValidationError(Exception):

    def __init__(self, *args):
        super(ValidationError, self).__init__(*args)
        self.message = self.args[0]

    def report(self, schema_file):
        message = cf.red(schema_file)
        if self.message:
            message += ": {}".format(self.message)
        print(message)


class InvalidFile(ValidationError):
    """
    InvalidFile
    Wraps other exceptions that occur in file validation

    :param message: message to display
    """

    def __init__(self, message):
        super(InvalidFile, self).__init__(message)
        exc_type, _, _ = self.exc_info = sys.exc_info()
        if exc_type:
            self.message = "{}: {}".format(exc_type.__name__, message)


def as_dict(d: pyhocon.ConfigTree) -> dict:
    """
    Convert ConfigTree to plain dict
    """
    def enter(path, key, value):
        if isinstance(value, OrderedDict):
            return dict(), value.items()
        return default_enter(path, key, value)

    return remap(d.as_plain_ordered_dict(), enter=enter)


def load_hocon(name):
    """
    load_hocon
    load configuration from file

    :param name: file path
    """
    return as_dict(cast(pyhocon.ConfigTree, pyhocon.ConfigFactory.parse_file(name)))


def validate_ascii_only(name):
    invalid_char = next(
        (
            (line_num, column, char)
            for line_num, line in enumerate(Path(name).read_text().splitlines())
            for column, char in enumerate(line)
            if ord(char) not in range(128)
        ),
        None,
    )
    if invalid_char:
        line, column, char = invalid_char
        raise ValidationError(
            "file contains non-ascii character {!r} in line {} pos {}".format(
                char, line, column
            )
        )


def validate_file(meta, name):
    """
    validate_file
    validate file according to meta-scheme

    :param meta: meta-scheme
    :param name: file path
    """
    validate_ascii_only(name)
    try:
        schema = load_hocon(name)
    except ParseBaseException as e:
        raise InvalidFile(repr(e))

    try:
        meta.validate(schema)
        return schema
    except JSONSchemaValidationError as e:
        visualize(ErrorTree(meta.iter_errors(schema)))
        path = "->".join(map(str, e.absolute_path))
        message = "{}: {}".format(path, reprlib.repr(e.args[0]))  # truncate
        raise InvalidFile(message)
    except Exception as e:
        raise InvalidFile(str(e))


def visualize(errors: ErrorTree):
    """
    Visualize all errors as a tree structure
    """
    tree = Tree()

    # detect dictionaries by nested curly braces
    p = r"""
    (?<brace>
        \{
        (?:
            [^{}]++      # anything but braces
        | (?&brace)    # recurse for nesting
        )*
        \}
    )"""

    pattern = regex.compile(p, regex.VERBOSE)

    def message(error: JSONSchemaValidationError) -> str:
        return pattern.sub("<redacted>", error.message)

    def format_error(error: JSONSchemaValidationError) -> str:
        return f"{cf.green('.'.join(error.path))} ${cf.magenta('.'.join(map(str, error.schema_path)))}: {message(error)}"

    def walk_error(parent: Node | None, error: JSONSchemaValidationError) -> None:
        for sub_error in error.context or []:
            parent_ = tree.create_node(
                data=sub_error, parent=parent, tag=format_error(sub_error)
            )
            walk_error(parent_, sub_error)

    def walk(parent: Node | None, error_node: ErrorTree):
        parent = tree.create_node(
            data=error_node.errors,
            parent=parent,
            tag="\n".join(map(format_error, error_node.errors.values())),
        )
        for v in error_node.errors.values():
            walk_error(parent, v)
        for v in error_node._contents.values():
            walk(parent, v)

    walk(None, errors)
    tree.show()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    parser.add_argument(
        "--stop", "-s", action="store_true", help="stop after first error"
    )
    parser.add_argument(
        "--linter", "-l", action="store_true", help="open jsonschema linter in browser"
    )
    parser.add_argument(
        "--raise",
        "-r",
        action="store_true",
        dest="raise_",
        help="raise first exception encountered and print traceback",
    )
    parser.add_argument(
        "--detect-collisions",
        action="store_true",
        help="detect objects with the same name in different modules",
    )
    return parser.parse_args()


def open_linter(driver, meta, schema):
    if not driver.operational():
        raise Exception("selenium not installed")
    driver.maximize_window()
    driver.get(LINTER_URL)
    storage = LocalStorage(driver)
    storage["jsonText"] = json.dumps(schema, indent=4)
    storage["schemaText"] = json.dumps(meta, indent=4)
    driver.refresh()


class LazyDriver(object):
    def __init__(self):
        self._driver = None
        try:
            from selenium import common, webdriver
        except ImportError:
            webdriver = None
            common = None
        self.webdriver = webdriver
        self.common = common

    def operational(self) -> bool:
        return self.driver is not None

    def __getattr__(self, item):
        return getattr(self.driver, item)

    @property
    def driver(self):
        if self._driver:
            return self._driver
        if not (self.webdriver and self.common):
            print("selenium not installed: linter unavailable")
            return None

        for driver_type in self.webdriver.Chrome, self.webdriver.Firefox:
            try:
                self._driver = driver_type()
                break
            except self.common.exceptions.WebDriverException:
                pass
        else:
            print("No webdriver is found for chrome or firefox")

        return self._driver

    def wait(self):
        assert self.common
        if not self._driver:
            return
        try:
            while True:
                self._driver.title
                time.sleep(0.5)
        except self.common.exceptions.WebDriverException:
            pass


def remove_description(dct):
    dct.pop("description", None)
    for value in dct.values():
        try:
            remove_description(value)
        except (TypeError, AttributeError):
            pass


def main(here: str):
    args = parse_args()
    meta = load_hocon(here + "/meta.conf")
    validator_for(meta).check_schema(meta)
    meta = Draft202012Validator(load_hocon(here + "/meta.conf"))

    driver = LazyDriver()

    collisions = {}

    for schema_file in args.files:

        if Path(schema_file).name.startswith("_"):
            continue

        try:
            print(cf.bold(f"checking: {schema_file}"))
            schema = validate_file(meta, schema_file)
        except InvalidFile as e:
            if args.linter:
                open_linter(driver, meta, load_hocon(schema_file))
            elif args.raise_:
                raise
            elif args.stop:
                break

            e.report(schema_file)
        except ValidationError as e:
            e.report(schema_file)
        else:
            for def_name, value in schema.get("_definitions", {}).items():
                service_name = str(Path(schema_file).stem)
                remove_description(value)
                collisions.setdefault(def_name, {})[service_name] = value

    warning = cf.red("warning")

    if args.detect_collisions:
        for name, values in collisions.items():
            if len(values) <= 1:
                continue
            groups = [
                [service for (service, _) in pairs]
                for _, pairs in groupby(values.items(), itemgetter(1))
            ]
            if not groups:
                raise RuntimeError("Unknown error")
            print(
                "{}: collision for {}:\n{}".format(warning, name, yaml.dump(groups)),
                end="",
            )

    driver.wait()


if __name__ == "__main__":
    main(here=os.path.dirname(__file__))
