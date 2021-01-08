# -*- coding: utf-8 -*-

import functools
import inspect
import json
import os
import sys
from io import open
from types import MethodType
from typing import Any, Dict, Iterable, List, Type, Union

import requests
import six
import wrapt

try:
    from lark import ParseError
except ImportError:
    from lark.common import ParseError

from brewtils.choices import parse
from brewtils.errors import PluginParamError, _deprecate
from brewtils.models import Command, Parameter, Choices

if sys.version_info.major == 2:
    from funcsigs import signature, Parameter as InspectParameter
else:
    from inspect import signature, Parameter as InspectParameter

__all__ = [
    "command",
    "parameter",
    "parameters",
    "system",
]

# The wrapt module has a cool feature where you can disable wrapping a decorated function,
# instead just using the original function. This is pretty much exactly what we want - we
# aren't using decorators for their 'real' purpose of wrapping a function, we just want to add
# some metadata to the function object. So we'll disable the wrapping normally, but we need to
# test that enabling the wrapping would work.
_wrap_functions = False


def parse_client(client):
    """Get a list of Beergarden Commands from a client object

    This will iterate over everything returned from dir, looking for metadata added
    by the decorators.

    """
    bg_commands = []

    for attr in dir(client):
        method = getattr(client, attr)

        if inspect.ismethod(method) and (
            hasattr(method, "_command") or hasattr(method, "parameters")
        ):
            method_command = _initialize_command(method)

            for p in method_command.parameters:
                _initialize_parameter(param=p, func=method)

            bg_commands.append(method_command)

    return bg_commands


def system(cls=None, bg_name=None, bg_version=None):
    """Class decorator that marks a class as a beer-garden System

    This doesn't really do anything anymore, and is now pretty much deprecated. This
    should really be named "client," but after consideration it doesn't really need to
    exist at all - the Client is whatever you tell the Plugin it is, no need for
    a special decorator.

    For historical purposes - the functionality of the ``parse_client`` function was
    previously in this decorator.

    This does creates some attributes on the class for back-compatability reasons (and
    to stop linters from complaining). But these are just placeholders until the actual
    values are determined when the Plugin client is set:

      * ``_bg_name``: an optional system name
      * ``_bg_version``: an optional system version
      * ``_bg_commands``: holds all registered commands
      * ``_current_request``: Reference to the currently executing request

    Args:
        cls: The class to decorated
        bg_name: Optional plugin name
        bg_version: Optional plugin version

    Returns:
        The decorated class

    """
    if cls is None:
        return functools.partial(system, bg_name=bg_name, bg_version=bg_version)

    # Assign these here so linters don't complain
    cls._bg_name = bg_name
    cls._bg_version = bg_version
    cls._bg_commands = []
    cls._current_request = None

    return cls


def command(
    _wrapped=None,  # type: MethodType
    description=None,  # type: str
    parameters=None,  # type: List[Parameter]
    command_type="ACTION",  # type: str
    output_type="STRING",  # type: str
    schema=None,
    form=None,
    template=None,
    icon_name=None,  # type: str
    hidden=False,  # type: bool
):
    """Decorator that marks a function as a beer-garden command

    For example:

    .. code-block:: python

        @command(output_type='JSON')
        def echo_json(self, message):
            return message

    Args:
        _wrapped: The function to decorate. This is handled as a positional argument and
            shouldn't be explicitly set.
        description: The command description. If not given the first line of the method
            docstring will be used.
        parameters: A list of Command parameters. It's recommended to use @parameter
            decorators to declare Parameters instead of declaring them here, but it is
            allowed. Any Parameters given here will be merged with Parameters sourced
            from decorators and inferred from the method signature.
        command_type: The command type. Valid options are Command.COMMAND_TYPES.
        output_type: The output type. Valid options are Command.OUTPUT_TYPES.
        schema: A custom schema definition.
        form: A custom form definition.
        template: A custom template definition.
        icon_name: The icon name. Should be either a FontAwesome or a Glyphicon name.
        hidden: Flag controlling whether the command is visible on the user interface.

    Returns:
        The decorated function
    """
    if _wrapped is None:
        return functools.partial(
            command,
            description=description,
            parameters=parameters,
            command_type=command_type,
            output_type=output_type,
            schema=schema,
            form=form,
            template=template,
            icon_name=icon_name,
            hidden=hidden,
        )

    _wrapped._command = Command(
        description=description,
        parameters=parameters,
        command_type=command_type,
        output_type=output_type,
        schema=schema,
        form=form,
        template=template,
        icon_name=icon_name,
        hidden=hidden,
    )

    return _wrapped


def parameter(
    _wrapped=None,  # type: Union[MethodType, Type]
    key=None,  # type: str
    type=None,  # type: str
    multi=None,  # type: bool
    display_name=None,  # type: str
    optional=None,  # type: bool
    default=None,  # type: Any
    description=None,  # type: str
    choices=None,  # type: Union[Dict, Iterable, str]
    parameters=None,  # type: List[Parameter]
    nullable=None,  # type: bool
    maximum=None,  # type: int
    minimum=None,  # type: int
    regex=None,  # type: str
    form_input_type=None,  # type: str
    type_info=None,  # type: dict
    is_kwarg=None,  # type: bool
    model=None,  # type: Type
):
    """Decorator that enables Parameter specifications for a beer-garden Command

    This is intended to be used when more specification is desired for a Parameter.

    For example::

        @parameter(
            key="message",
            description="Message to echo",
            optional=True,
            type="String",
            default="Hello, World!",
        )
        def echo(self, message):
            return message

    Args:
        _wrapped: The function to decorate. This is handled as a positional argument and
            shouldn't be explicitly set.
        key: String specifying the parameter identifier. If the decorated object is a
            method the key must match an argument name.
        type: String indicating the type to use for this parameter.
        multi: Boolean indicating if this parameter is a multi. See documentation for
            discussion of what this means.
        display_name: String that will be displayed as a label in the user interface.
        optional: Boolean indicating if this parameter must be specified.
        default: The value this parameter will be assigned if not overridden when
            creating a request.
        description: An additional string that will be displayed in the user interface.
        choices: List or dictionary specifying allowed values. See documentation for
            more information.
        parameters: Any nested parameters. See also: the 'model' argument.
        nullable: Boolean indicating if this parameter is allowed to be null.
        maximum: Integer indicating the maximum value of the parameter.
        minimum: Integer indicating the minimum value of the parameter.
        regex: String describing a regular expression constraint on the parameter.
        form_input_type: Specify the form input field type (e.g. textarea). Only used
            for string fields.
        type_info: Type-specific information. Mostly reserved for future use.
        is_kwarg: Boolean indicating if this parameter is meant to be part of the
            decorated function's kwargs. Only applies when the decorated object is a
            method.
        model: Class to be used as a model for this parameter. Must be a Python type
            object, not an instance.

    Returns:
        The decorated function

    """
    if _wrapped is None:
        return functools.partial(
            parameter,
            key=key,
            type=type,
            multi=multi,
            display_name=display_name,
            optional=optional,
            default=default,
            description=description,
            choices=choices,
            parameters=parameters,
            nullable=nullable,
            maximum=maximum,
            minimum=minimum,
            regex=regex,
            form_input_type=form_input_type,
            type_info=type_info,
            is_kwarg=is_kwarg,
            model=model,
        )

    _wrapped.parameters = getattr(_wrapped, "parameters", [])

    _wrapped.parameters.append(
        Parameter(
            key=key,
            type=type,
            multi=multi,
            display_name=display_name,
            optional=optional,
            default=default,
            description=description,
            choices=choices,
            parameters=parameters,
            nullable=nullable,
            maximum=maximum,
            minimum=minimum,
            regex=regex,
            form_input_type=form_input_type,
            type_info=type_info,
            is_kwarg=is_kwarg,
            model=model,
        )
    )

    return _wrapped


def parameters(*args):
    """Specify multiple Parameter definitions at once

    This can be useful for commands which have a large number of complicated
    parameters but aren't good candidates for a Model.

    .. code-block:: python

        @parameter(**params[cmd1][param1])
        @parameter(**params[cmd1][param2])
        @parameter(**params[cmd1][param3])
        def cmd1(self, **kwargs):
            pass

    Can become:

    .. code-block:: python

        @parameters(params[cmd1])
        def cmd1(self, **kwargs):
            pass

    Args:
        *args (iterable): Positional arguments
            The first (and only) positional argument must be a list containing
            dictionaries that describe parameters.

    Returns:
        func: The decorated function
    """
    if len(args) == 1:
        return functools.partial(parameters, args[0])
    elif len(args) != 2:
        raise PluginParamError("@parameters takes a single argument")

    try:
        for param in args[0]:
            parameter(args[1], **param)
    except TypeError:
        raise PluginParamError("@parameters arg must be an iterable of dictionaries")

    @wrapt.decorator(enabled=_wrap_functions)
    def wrapper(_double_wrapped, _, _args, _kwargs):
        return _double_wrapped(*_args, **_kwargs)

    return wrapper(args[1])


def _initialize_command(method):
    # type: (MethodType) -> Command
    """Update a Command definition with info from the method

    Args:
        method: The method with the Command to initialize

    Returns:
        The initialized Command

    """
    cmd = getattr(method, "_command", Command())

    cmd.name = _function_name(method)
    cmd.description = cmd.description or _function_docstring(method)

    resolved_mod = _resolve_display_modifiers(
        method, cmd.name, schema=cmd.schema, form=cmd.form, template=cmd.template
    )
    cmd.schema = resolved_mod["schema"]
    cmd.form = resolved_mod["form"]
    cmd.template = resolved_mod["template"]

    cmd.parameters += getattr(method, "parameters", [])
    for arg in signature(method).parameters.values():
        if arg.name not in cmd.parameter_keys():
            cmd.parameters.append(Parameter(key=arg.name, optional=False))
        else:
            # I'm not super happy about this. It makes sense - positional arguments are
            # "required", so mark them as non-optional, but it's really unexpected.
            # A @parameter that doesn't specify "optional=" will have a different value
            # based on the function signature. Regardless, we went with this originally
            # so we need to keep it for back-compatibility
            param = cmd.get_parameter_by_key(arg.name)
            if param.optional is None:
                param.optional = False

    return cmd


def _function_name(func):
    # Required for Python 2/3 compatibility
    if hasattr(func, "func_name"):
        command_name = func.func_name
    else:
        command_name = func.__name__

    return command_name


def _function_docstring(func):
    # Required for Python 2/3 compatibility
    if hasattr(func, "func_doc"):
        docstring = func.func_doc
    else:
        docstring = func.__doc__

    return docstring.split("\n")[0] if docstring else None


def _resolve_display_modifiers(
    wrapped, command_name, schema=None, form=None, template=None
):
    def _load_from_url(url):
        response = requests.get(url)
        if response.headers.get("content-type", "").lower() == "application/json":
            return json.loads(response.text)
        return response.text

    def _load_from_path(path):
        current_dir = os.path.dirname(inspect.getfile(wrapped))
        file_path = os.path.abspath(os.path.join(current_dir, path))

        with open(file_path, "r") as definition_file:
            return definition_file.read()

    resolved = {}

    for key, value in {"schema": schema, "form": form, "template": template}.items():

        if isinstance(value, six.string_types):
            try:
                if value.startswith("http"):
                    resolved[key] = _load_from_url(value)

                elif value.startswith("/") or value.startswith("."):
                    loaded_value = _load_from_path(value)
                    resolved[key] = (
                        loaded_value if key == "template" else json.loads(loaded_value)
                    )

                elif key == "template":
                    resolved[key] = value

                else:
                    raise PluginParamError(
                        "%s specified for command '%s' was not a "
                        "definition, file path, or URL" % (key, command_name)
                    )
            except Exception as ex:
                raise PluginParamError(
                    "Error reading %s definition from '%s' for command "
                    "'%s': %s" % (key, value, command_name, ex)
                )

        elif value is None or (key in ["schema", "form"] and isinstance(value, dict)):
            resolved[key] = value

        elif key == "form" and isinstance(value, list):
            resolved[key] = {"type": "fieldset", "items": value}

        else:
            raise PluginParamError(
                "%s specified for command '%s' was not a definition, "
                "file path, or URL" % (key, command_name)
            )

    return resolved


def _initialize_parameter(
    param=None,
    func=None,
    key=None,
    type=None,
    multi=None,
    display_name=None,
    optional=None,
    default=None,
    description=None,
    choices=None,
    parameters=None,
    nullable=None,
    maximum=None,
    minimum=None,
    regex=None,
    form_input_type=None,
    type_info=None,
    is_kwarg=None,
    model=None,
):
    """Helper method to 'fix' Parameters

    This exists to move logic out of the @parameter decorator. Previously there was a
    fair amount of logic in the decorator, which meant that it wasn't feasible to create
    a Parameter without using it. This made things like nested models difficult to do
    correctly.

    There are also some checks and translation that need to happen for every Parameter,
    most notably the "choices" attribute.

    This method also ensures that these checks and translations occur for child
    Parameters.

    Args:
        param: An already-created Parameter. If this is given all the other
        Parameter-creation kwargs will be ignored
        func: The function this Parameter will be used on. This will only exist for
        top-level Parameters (Parameters that have a Command as their parent, not
        another Parameter). If given, additional checks will be performed to ensure the
        Parameter matches the function signature.


    Keyword Args:
        Will be used to construct a new Parameter
    """
    param = param or Parameter(
        key=key,
        type=type,
        multi=multi,
        display_name=display_name,
        optional=optional,
        default=default,
        description=description,
        choices=choices,
        parameters=parameters,
        nullable=nullable,
        maximum=maximum,
        minimum=minimum,
        regex=regex,
        form_input_type=form_input_type,
        type_info=type_info,
        is_kwarg=is_kwarg,
        model=model,
    )

    # Every parameter needs a key, so stop that right here
    if param.key is None:
        raise PluginParamError("Attempted to create a parameter without a key")

    param.type = _format_type(param.type)
    param.choices = _format_choices(param.choices)

    if func:
        func_default = _validate_kwargness(func, param)
        if func_default and param.default is None:
            param.default = func_default

    # Type info is where type specific information goes. For now, this is specific
    # to file types. See #289 for more details.
    if param.type == "Bytes":
        param.type_info = {"storage": "gridfs"}

    # Nullifying default file parameters for safety
    if param.type == "Base64":
        param.default = None

    # Now deal with nested parameters
    if param.parameters:
        param.type = "Dictionary"
        param.parameters = _generate_nested_params(param.parameters)

    elif param.model is not None:
        param.type = "Dictionary"
        param.parameters = _generate_nested_params(param.model.parameters)

        # If the model is not nullable and does not have a default we will try
        # to generate a one using the defaults defined on the model parameters
        if not param.nullable and not param.default:
            param.default = {}
            for nested_param in param.parameters:
                if nested_param.default:
                    param.default[nested_param.key] = nested_param.default

    return param


def _generate_nested_params(parameter_list):
    """Generate nested parameters from a list of Parameters or a Model object

    This exists for backwards compatibility with the "old

    This function will take a list of Parameters and will return a new list of "real"
    Parameters.

    The main thing this does is ensure the choices specification is correct for all
    Parameters in the tree.
    """
    initialized_params = []

    for param in parameter_list:

        # This is already a Parameter. Only really need to interpret the choices
        # definition and recurse down into nested Parameters
        if isinstance(param, Parameter):
            initialized_params.append(_initialize_parameter(param=param))

        # This is a model class object. Needed for backwards compatibility
        # See https://github.com/beer-garden/beer-garden/issues/354
        elif hasattr(param, "parameters"):
            _deprecate(
                "Constructing a nested Parameters list using model class objects "
                "is deprecated. Please pass the model's parameter list directly."
            )
            initialized_params += _generate_nested_params(param.parameters)

        # No clue!
        else:
            raise PluginParamError("Unable to generate parameter from '%s'" % param)

    return initialized_params


def _format_type(param_type):
    if param_type == str:
        return "String"
    elif param_type == int:
        return "Integer"
    elif param_type == float:
        return "Float"
    elif param_type == bool:
        return "Boolean"
    elif param_type == dict:
        return "Dictionary"
    elif str(param_type).lower() == "file":
        return "Bytes"
    elif str(param_type).lower() == "datetime":
        return "DateTime"
    elif not param_type:
        return "Any"
    else:
        return str(param_type).title()


def _format_choices(choices):
    def determine_display(display_value):
        if isinstance(display_value, six.string_types):
            return "typeahead"

        return "select" if len(display_value) <= 50 else "typeahead"

    def determine_type(type_value):
        if isinstance(type_value, six.string_types):
            return "url" if type_value.startswith("http") else "command"

        return "static"

    if not choices:
        return None

    if isinstance(choices, dict):
        if not choices.get("value"):
            raise PluginParamError(
                "No 'value' provided for choices. You must at least "
                "provide valid values."
            )

        value = choices.get("value")
        display = choices.get("display", determine_display(value))
        choice_type = choices.get("type")
        strict = choices.get("strict", True)

        if choice_type is None:
            choice_type = determine_type(value)
        elif choice_type not in Choices.TYPES:
            raise PluginParamError(
                "Invalid choices type '%s' - Valid type options are %s"
                % (choice_type, Choices.TYPES)
            )
        else:
            if (
                (
                    choice_type == "command"
                    and not isinstance(value, (six.string_types, dict))
                )
                or (choice_type == "url" and not isinstance(value, six.string_types))
                or (choice_type == "static" and not isinstance(value, (list, dict)))
            ):
                allowed_types = {
                    "command": "('string', 'dictionary')",
                    "url": "('string')",
                    "static": "('list', 'dictionary)",
                }
                raise PluginParamError(
                    "Invalid choices value type '%s' - Valid value types for "
                    "choice type '%s' are %s"
                    % (type(value), choice_type, allowed_types[choice_type])
                )

        if display not in Choices.DISPLAYS:
            raise PluginParamError(
                "Invalid choices display '%s' - Valid display options are %s"
                % (display, Choices.DISPLAYS)
            )

    elif isinstance(choices, str):
        value = choices
        display = determine_display(value)
        choice_type = determine_type(value)
        strict = True

    else:
        try:
            # Assume some sort of iterable
            value = list(choices)
        except TypeError:
            raise PluginParamError(
                "Invalid 'choices': must be a string, dictionary, or iterable."
            )

        display = determine_display(value)
        choice_type = determine_type(value)
        strict = True

    # Now parse out type-specific aspects
    unparsed_value = ""
    try:
        if choice_type == "command":
            if isinstance(value, six.string_types):
                unparsed_value = value
            else:
                unparsed_value = value["command"]

            details = parse(unparsed_value, parse_as="func")
        elif choice_type == "url":
            unparsed_value = value
            details = parse(unparsed_value, parse_as="url")
        else:
            if isinstance(value, dict):
                unparsed_value = choices.get("key_reference")
                if unparsed_value is None:
                    raise PluginParamError(
                        "Specifying a static choices dictionary requires a "
                        '"key_reference" field with a reference to another '
                        'parameter ("key_reference": "${param_key}")'
                    )

                details = {"key_reference": parse(unparsed_value, parse_as="reference")}
            else:
                details = {}
    except ParseError:
        raise PluginParamError(
            "Invalid choices definition - Unable to parse '%s'" % unparsed_value
        )

    return Choices(
        type=choice_type, display=display, value=value, strict=strict, details=details
    )


def _validate_kwargness(_wrapped, param):
    """Try to ensure that a Parameter lines up with the method signature

    It's expected that this will only be called for Parameters where this makes sense
    (aka top-level Parameters). It doesn't make sense to call this for model Parameters,
    so you shouldn't do that.

    Args:
        _wrapped:
        param:

    Returns:

    """
    sig_param = None  # The actual inspect.Parameter from the signature
    has_kwargs = False  # Does the func have **kwargs?

    for p in signature(_wrapped).parameters.values():
        if p.name == param.key:
            sig_param = p
        if p.kind == InspectParameter.VAR_KEYWORD:
            has_kwargs = True

    # Couldn't find the parameter. That's OK if this parameter is meant to be part of
    # the **kwargs AND the function has a **kwargs parameter.
    if sig_param is None:
        if param.is_kwarg is False:
            raise PluginParamError(
                "Parameter was not not marked as part of kwargs and wasn't found in "
                "the method signature (should is_kwarg be True?)"
            )
        elif not has_kwargs:
            raise PluginParamError(
                "Parameter was declared as a kwarg (is_kwarg=True) but the method "
                "signature does not declare a **kwargs parameter"
            )

    # Cool, found the parameter. Just verify that it's not pure positional and that it's
    # not marked as part of kwargs.
    else:
        if param.is_kwarg:
            raise PluginParamError(
                "Parameter was marked as part of kwargs but was found in the method "
                "signature (should is_kwarg be False?)"
            )

        # I don't think this is even possible in Python < 3.8
        if sig_param.kind == InspectParameter.POSITIONAL_ONLY:
            raise PluginParamError(
                "Sorry, positional-only type parameters are not supported"
            )

        if sig_param.default != InspectParameter.empty:
            return sig_param.default


# Alias the old names for compatibility
def command_registrar(*args, **kwargs):
    _deprecate(
        "Looks like you're using the '@command_registrar' decorator. Heads up - this "
        "name will be removed in version 4.0, please use '@system' instead. Thanks!"
    )
    return system(*args, **kwargs)


def register(*args, **kwargs):
    _deprecate(
        "Looks like you're using the '@register' decorator. Heads up - this name will "
        "be removed in version 4.0, please use '@command' instead. Thanks!"
    )
    return command(*args, **kwargs)


def plugin_param(*args, **kwargs):
    _deprecate(
        "Looks like you're using the '@plugin_param' decorator. Heads up - this name "
        "will be removed in version 4.0, please use '@parameter' instead. Thanks!"
    )
    return parameter(*args, **kwargs)
