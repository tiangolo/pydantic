import inspect
from decimal import Decimal
from enum import IntEnum
from typing import Any, Callable, List, Mapping, NamedTuple, Pattern, Set, Tuple, Type, Union

from . import errors as errors_
from .error_wrappers import ErrorWrapper
from .types import (
    DSN,
    ConstrainedDecimal,
    ConstrainedFloat,
    ConstrainedInt,
    ConstrainedStr,
    EmailStr,
    Json,
    JsonWrapper,
    UrlStr,
    condecimal,
    confloat,
    conint,
    constr,
)
from .utils import display_as_type, list_like
from .validators import NoneType, dict_validator, find_validators, not_none_validator

Required: Any = Ellipsis


class ValidatorSignature(IntEnum):
    JUST_VALUE = 1
    VALUE_KWARGS = 2
    CLS_JUST_VALUE = 3
    CLS_VALUE_KWARGS = 4


class Shape(IntEnum):
    SINGLETON = 1
    LIST = 2
    SET = 3
    MAPPING = 4
    TUPLE = 5


class Validator(NamedTuple):
    func: Callable
    pre: bool
    whole: bool
    always: bool
    check_fields: bool


class Schema:
    """
    Used to provide extra information about a field in a model schema. The parameters will be
    converted to validations and will add annotations to the generated JSON Schema. Some arguments
    apply only to number fields (``int``, ``float``, ``Decimal``) and some apply only to ``str``

    :param default: since the Schema is replacing the field’s default, its first argument is used
      to set the default, use ellipsis (``...``) to indicate the field is required
    :param alias: the public name of the field
    :param title: can be any string, used in the schema
    :param description: can be any string, used in the schema
    :param gt: only applies to numbers, requires the field to be "greater than". The schema
      will have an ``exclusiveMinimum`` validation keyword
    :param ge: only applies to numbers, requires the field to be "greater than or equal to". The
    schema will have a ``minimum`` validation keyword
    :param lt: only applies to numbers, requires the field to be "less than". The schema
    will have an ``exclusiveMaximum`` validation keyword
    :param le: only applies to numbers, requires the field to be "less than or equal to". The
    schema will have a ``maximum`` validation keyword
    :param min_length: only applies to strings, requires the field to have a minimum length. The
    schema will have a ``maximum`` validation keyword
    :param max_length: only applies to strings, requires the field to have a maximum length. The
    schema will have a ``maxLength`` validation keyword
    :param regex: only applies to strings, requires the field match agains a regular expression
    pattern string. The schema will have a ``pattern`` validation keyword
    :param **extra: any additional keyword arguments will be added as is to the schema
    """

    __slots__ = (
        'default',
        'alias',
        'title',
        'description',
        'gt',
        'ge',
        'lt',
        'le',
        'min_length',
        'max_length',
        'regex',
        'extra',
    )

    def __init__(
        self,
        default,
        *,
        alias: str = None,
        title: str = None,
        description: str = None,
        gt: float = None,
        ge: float = None,
        lt: float = None,
        le: float = None,
        min_length: int = None,
        max_length: int = None,
        regex: str = None,
        **extra,
    ):
        self.default = default
        self.alias = alias
        self.title = title
        self.description = description
        self.extra = extra
        self.gt = gt
        self.ge = ge
        self.lt = lt
        self.le = le
        self.min_length = min_length
        self.max_length = max_length
        self.regex = regex


_numeric_types = (int, float, Decimal)
_blacklist = (EmailStr, DSN, UrlStr, ConstrainedStr, ConstrainedInt, ConstrainedFloat, ConstrainedDecimal)
_str_attrs = ('max_length', 'min_length', 'regex')
_numeric_attrs = ('gt', 'lt', 'ge', 'le')
_map_types_const = {str: constr, int: conint, float: confloat, Decimal: condecimal}


def get_annotation_from_schema(annotation, schema):
    """Get an annotation with validation implemented for numbers and strings based on the schema.

    :param annotation: an annotation from a field specification, as ``str``, ``ConstrainedStr`` or the
      return value of ``constr(max_length=10)``
    :param schema: an instance of Schema, possibly with declarations for validations and JSON Schema
    :return: the same ``annotation`` if unmodified or a new annotation with validation in place
    """
    kwargs = {}
    params_to_set = False
    if (
        isinstance(annotation, type)
        and issubclass(annotation, (str,) + _numeric_types)
        and not issubclass(annotation, bool)
        and not any([issubclass(annotation, c) for c in _blacklist])
    ):
        if issubclass(annotation, str):
            attrs = _str_attrs
            kwargs = {'min_length': None, 'max_length': None, 'regex': None}
            con = _map_types_const[str]
        else:
            # Is numeric type
            attrs = _numeric_attrs
            kwargs = {'gt': None, 'lt': None, 'ge': None, 'le': None}
            n_types = [t for t in _numeric_types if issubclass(annotation, t)]
            con = _map_types_const[n_types[0]]
        for attr in attrs:
            if hasattr(schema, attr) and getattr(schema, attr) is not None:
                params_to_set = True
                kwargs[attr] = getattr(schema, attr)
        if params_to_set:
            annotation = con(**kwargs)
    return annotation


class Field:
    __slots__ = (
        'type_',
        'sub_fields',
        'key_field',
        'validators',
        'whole_pre_validators',
        'whole_post_validators',
        'default',
        'required',
        'model_config',
        'name',
        'alias',
        'has_alias',
        '_schema',
        'validate_always',
        'allow_none',
        'shape',
        'class_validators',
        'parse_json',
    )

    def __init__(
        self,
        *,
        name: str,
        type_: Type,
        class_validators: List[Validator],
        default: Any,
        required: bool,
        model_config: Any,
        alias: str = None,
        allow_none: bool = False,
        schema: Schema = None,
    ):

        self.name: str = name
        self.has_alias: bool = bool(alias)
        self.alias: str = alias or name
        self.type_: type = type_
        self.class_validators = class_validators or []
        self.validate_always: bool = False
        self.sub_fields: List[Field] = None
        self.key_field: Field = None
        self.validators = []
        self.whole_pre_validators = None
        self.whole_post_validators = None
        self.default: Any = default
        self.required: bool = required
        self.model_config = model_config
        self.allow_none: bool = allow_none
        self.parse_json: bool = False
        self.shape: Shape = Shape.SINGLETON
        self._schema: Schema = schema
        self.prepare()

    @classmethod
    def infer(cls, *, name, value, annotation, class_validators, config):
        schema_from_config = config.get_field_schema(name)
        if isinstance(value, Schema):
            schema = value
            value = schema.default
        else:
            schema = Schema(value, **schema_from_config)
        schema.alias = schema.alias or schema_from_config.get('alias')
        required = value == Required
        annotation = get_annotation_from_schema(annotation, schema)
        return cls(
            name=name,
            type_=annotation,
            alias=schema.alias,
            class_validators=class_validators,
            default=None if required else value,
            required=required,
            model_config=config,
            schema=schema,
        )

    def set_config(self, config):
        self.model_config = config
        schema_from_config = config.get_field_schema(self.name)
        if schema_from_config:
            self._schema.alias = self._schema.alias or schema_from_config.get('alias')
            self.alias = self._schema.alias

    @property
    def alt_alias(self):
        return self.name != self.alias

    def prepare(self):
        if self.default is not None and self.type_ is None:
            self.type_ = type(self.default)

        if self.type_ is None:
            raise errors_.ConfigError(f'unable to infer type for attribute "{self.name}"')

        self.validate_always: bool = (
            getattr(self.type_, 'validate_always', False) or any(v.always for v in self.class_validators)
        )

        if not self.required and not self.validate_always and self.default is None:
            self.allow_none = True

        self._populate_sub_fields()
        self._populate_validators()

    def _populate_sub_fields(self):  # noqa: C901 (ignore complexity)
        # typing interface is horrible, we have to do some ugly checks
        if isinstance(self.type_, type) and issubclass(self.type_, JsonWrapper):
            self.type_ = self.type_.inner_type
            self.parse_json = True

        if self.type_ is Pattern:
            # python 3.7 only, Pattern is a typing object but without sub fields
            return
        origin = getattr(self.type_, '__origin__', None)
        if origin is None:
            # field is not "typing" object eg. Union, Dict, List etc.
            return
        if origin is Union:
            types_ = []
            for type_ in self.type_.__args__:
                if type_ is NoneType:
                    self.allow_none = True
                    self.required = False
                else:
                    types_.append(type_)
            self.sub_fields = [self._create_sub_type(t, f'{self.name}_{display_as_type(t)}') for t in types_]
            return

        if issubclass(origin, Tuple):
            self.shape = Shape.TUPLE
            self.sub_fields = [self._create_sub_type(t, f'{self.name}_{i}') for i, t in enumerate(self.type_.__args__)]
            return

        if issubclass(origin, List):
            self.type_ = self.type_.__args__[0]
            self.shape = Shape.LIST
        elif issubclass(origin, Set):
            self.type_ = self.type_.__args__[0]
            self.shape = Shape.SET
        else:
            assert issubclass(origin, Mapping)
            self.key_field = self._create_sub_type(self.type_.__args__[0], 'key_' + self.name)
            self.type_ = self.type_.__args__[1]
            self.shape = Shape.MAPPING

        if getattr(self.type_, '__origin__', None):
            # type_ has been refined eg. as the type of a List and sub_fields needs to be populated
            self.sub_fields = [self._create_sub_type(self.type_, '_' + self.name)]

    def _create_sub_type(self, type_, name):
        return self.__class__(
            type_=type_,
            name=name,
            class_validators=self.class_validators,
            default=self.default,
            required=self.required,
            allow_none=self.allow_none,
            model_config=self.model_config,
        )

    def _populate_validators(self):
        if not self.sub_fields:
            get_validators = getattr(self.type_, 'get_validators', None)
            v_funcs = (
                *tuple(v.func for v in self.class_validators if not v.whole and v.pre),
                *(
                    get_validators()
                    if get_validators
                    else find_validators(self.type_, self.model_config.arbitrary_types_allowed)
                ),
                *tuple(v.func for v in self.class_validators if not v.whole and not v.pre),
            )
            self.validators = self._prep_vals(v_funcs)

        if self.class_validators:
            self.whole_pre_validators = self._prep_vals(v.func for v in self.class_validators if v.whole and v.pre)
            self.whole_post_validators = self._prep_vals(v.func for v in self.class_validators if v.whole and not v.pre)

    def _prep_vals(self, v_funcs):
        v = []
        for f in v_funcs:
            if not f or (self.allow_none and f is not_none_validator):
                continue
            v.append((_get_validator_signature(f), f))
        return tuple(v)

    def validate(self, v, values, *, loc, cls=None):  # noqa: C901 (ignore complexity)
        if self.allow_none and v is None:
            return None, None

        loc = loc if isinstance(loc, tuple) else (loc,)

        if self.parse_json:
            v, error = self._validate_json(v, loc)
            if error:
                return v, error

        if self.whole_pre_validators:
            v, errors = self._apply_validators(v, values, loc, cls, self.whole_pre_validators)
            if errors:
                return v, errors

        if self.shape is Shape.SINGLETON:
            v, errors = self._validate_singleton(v, values, loc, cls)
        elif self.shape is Shape.MAPPING:
            v, errors = self._validate_mapping(v, values, loc, cls)
        elif self.shape is Shape.TUPLE:
            v, errors = self._validate_tuple(v, values, loc, cls)
        else:
            # list or set
            v, errors = self._validate_list_set(v, values, loc, cls)
            if not errors and self.shape is Shape.SET:
                v = set(v)

        if not errors and self.whole_post_validators:
            v, errors = self._apply_validators(v, values, loc, cls, self.whole_post_validators)
        return v, errors

    def _validate_json(self, v, loc):
        try:
            return Json.validate(v), None
        except (ValueError, TypeError) as exc:
            return v, ErrorWrapper(exc, loc=loc, config=self.model_config)

    def _validate_list_set(self, v, values, loc, cls):
        if not list_like(v):
            e = errors_.ListError() if self.shape is Shape.LIST else errors_.SetError()
            return v, ErrorWrapper(e, loc=loc, config=self.model_config)

        result, errors = [], []
        for i, v_ in enumerate(v):
            v_loc = *loc, i
            r, e = self._validate_singleton(v_, values, v_loc, cls)
            if e:
                errors.append(e)
            else:
                result.append(r)

        if errors:
            return v, errors
        else:
            return result, None

    def _validate_tuple(self, v, values, loc, cls):
        e = None
        if not list_like(v):
            e = errors_.TupleError()
        else:
            actual_length, expected_length = len(v), len(self.sub_fields)
            if actual_length != expected_length:
                e = errors_.TupleLengthError(actual_length=actual_length, expected_length=expected_length)

        if e:
            return v, ErrorWrapper(e, loc=loc, config=self.model_config)

        result, errors = [], []
        for i, (v_, field) in enumerate(zip(v, self.sub_fields)):
            v_loc = *loc, i
            r, e = field.validate(v_, values, loc=v_loc, cls=cls)
            if e:
                errors.append(e)
            else:
                result.append(r)

        if errors:
            return v, errors
        else:
            return tuple(result), None

    def _validate_mapping(self, v, values, loc, cls):
        try:
            v_iter = dict_validator(v)
        except TypeError as exc:
            return v, ErrorWrapper(exc, loc=loc, config=self.model_config)

        result, errors = {}, []
        for k, v_ in v_iter.items():
            v_loc = *loc, '__key__'
            key_result, key_errors = self.key_field.validate(k, values, loc=v_loc, cls=cls)
            if key_errors:
                errors.append(key_errors)
                continue

            v_loc = *loc, k
            value_result, value_errors = self._validate_singleton(v_, values, v_loc, cls)
            if value_errors:
                errors.append(value_errors)
                continue

            result[key_result] = value_result
        if errors:
            return v, errors
        else:
            return result, None

    def _validate_singleton(self, v, values, loc, cls):
        if self.sub_fields:
            errors = []
            for field in self.sub_fields:
                value, error = field.validate(v, values, loc=loc, cls=cls)
                if error:
                    errors.append(error)
                else:
                    return value, None
            return v, errors
        else:
            return self._apply_validators(v, values, loc, cls, self.validators)

    def _apply_validators(self, v, values, loc, cls, validators):
        for signature, validator in validators:
            try:
                if signature is ValidatorSignature.JUST_VALUE:
                    v = validator(v)
                elif signature is ValidatorSignature.VALUE_KWARGS:
                    v = validator(v, values=values, config=self.model_config, field=self)
                elif signature is ValidatorSignature.CLS_JUST_VALUE:
                    v = validator(cls, v)
                else:
                    # ValidatorSignature.CLS_VALUE_KWARGS
                    v = validator(cls, v, values=values, config=self.model_config, field=self)
            except (ValueError, TypeError) as exc:
                return v, ErrorWrapper(exc, loc=loc, config=self.model_config)
        return v, None

    def __repr__(self):
        return f'<Field({self})>'

    def __str__(self):
        parts = [self.name, 'type=' + display_as_type(self.type_)]

        if self.required:
            parts.append('required')
        else:
            parts.append(f'default={self.default!r}')

        if self.alt_alias:
            parts.append('alias=' + self.alias)
        return ' '.join(parts)


def _get_validator_signature(validator):
    signature = inspect.signature(validator)

    # bind here will raise a TypeError so:
    # 1. we can deal with it before validation begins
    # 2. (more importantly) it doesn't get confused with a TypeError when executing the validator
    try:
        if 'cls' in signature._parameters:
            if len(signature.parameters) == 2:
                signature.bind(object(), 1)
                return ValidatorSignature.CLS_JUST_VALUE
            else:
                signature.bind(object(), 1, values=2, config=3, field=4)
                return ValidatorSignature.CLS_VALUE_KWARGS
        else:
            if len(signature.parameters) == 1:
                signature.bind(1)
                return ValidatorSignature.JUST_VALUE
            else:
                signature.bind(1, values=2, config=3, field=4)
                return ValidatorSignature.VALUE_KWARGS
    except TypeError as e:
        raise errors_.ConfigError(
            f'Invalid signature for validator {validator}: {signature}, should be: '
            f'(value) or (value, *, values, config, field) or for class validators '
            f'(cls, value) or (cls, value, *, values, config, field)'
        ) from e
