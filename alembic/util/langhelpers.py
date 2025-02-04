import collections
from collections.abc import Iterable
import textwrap
import uuid
import warnings

from sqlalchemy.util import asbool  # noqa
from sqlalchemy.util import immutabledict  # noqa
from sqlalchemy.util import memoized_property  # noqa
from sqlalchemy.util import to_list  # noqa
from sqlalchemy.util import unique_list  # noqa

from .compat import inspect_getargspec
from .compat import string_types


class _ModuleClsMeta(type):
    def __setattr__(cls, key, value):
        super(_ModuleClsMeta, cls).__setattr__(key, value)
        cls._update_module_proxies(key)


class ModuleClsProxy(metaclass=_ModuleClsMeta):
    """Create module level proxy functions for the
    methods on a given class.

    The functions will have a compatible signature
    as the methods.

    """

    _setups = collections.defaultdict(lambda: (set(), []))

    @classmethod
    def _update_module_proxies(cls, name):
        attr_names, modules = cls._setups[cls]
        for globals_, locals_ in modules:
            cls._add_proxied_attribute(name, globals_, locals_, attr_names)

    def _install_proxy(self):
        attr_names, modules = self._setups[self.__class__]
        for globals_, locals_ in modules:
            globals_["_proxy"] = self
            for attr_name in attr_names:
                globals_[attr_name] = getattr(self, attr_name)

    def _remove_proxy(self):
        attr_names, modules = self._setups[self.__class__]
        for globals_, locals_ in modules:
            globals_["_proxy"] = None
            for attr_name in attr_names:
                del globals_[attr_name]

    @classmethod
    def create_module_class_proxy(cls, globals_, locals_):
        attr_names, modules = cls._setups[cls]
        modules.append((globals_, locals_))
        cls._setup_proxy(globals_, locals_, attr_names)

    @classmethod
    def _setup_proxy(cls, globals_, locals_, attr_names):
        for methname in dir(cls):
            cls._add_proxied_attribute(methname, globals_, locals_, attr_names)

    @classmethod
    def _add_proxied_attribute(cls, methname, globals_, locals_, attr_names):
        if not methname.startswith("_"):
            meth = getattr(cls, methname)
            if callable(meth):
                locals_[methname] = cls._create_method_proxy(
                    methname, globals_, locals_
                )
            else:
                attr_names.add(methname)

    @classmethod
    def _create_method_proxy(cls, name, globals_, locals_):
        fn = getattr(cls, name)

        def _name_error(name, from_):
            raise NameError(
                "Can't invoke function '%s', as the proxy object has "
                "not yet been "
                "established for the Alembic '%s' class.  "
                "Try placing this code inside a callable."
                % (name, cls.__name__)
            ) from from_

        globals_["_name_error"] = _name_error

        translations = getattr(fn, "_legacy_translations", [])
        if translations:
            spec = inspect_getargspec(fn)
            if spec[0] and spec[0][0] == "self":
                spec[0].pop(0)

            outer_args = inner_args = "*args, **kw"
            translate_str = "args, kw = _translate(%r, %r, %r, args, kw)" % (
                fn.__name__,
                tuple(spec),
                translations,
            )

            def translate(fn_name, spec, translations, args, kw):
                return_kw = {}
                return_args = []

                for oldname, newname in translations:
                    if oldname in kw:
                        warnings.warn(
                            "Argument %r is now named %r "
                            "for method %s()." % (oldname, newname, fn_name)
                        )
                        return_kw[newname] = kw.pop(oldname)
                return_kw.update(kw)

                args = list(args)
                if spec[3]:
                    pos_only = spec[0][: -len(spec[3])]
                else:
                    pos_only = spec[0]
                for arg in pos_only:
                    if arg not in return_kw:
                        try:
                            return_args.append(args.pop(0))
                        except IndexError:
                            raise TypeError(
                                "missing required positional argument: %s"
                                % arg
                            )
                return_args.extend(args)

                return return_args, return_kw

            globals_["_translate"] = translate
        else:
            outer_args = "*args, **kw"
            inner_args = "*args, **kw"
            translate_str = ""

        func_text = textwrap.dedent(
            """\
        def %(name)s(%(args)s):
            %(doc)r
            %(translate)s
            try:
                p = _proxy
            except NameError as ne:
                _name_error('%(name)s', ne)
            return _proxy.%(name)s(%(apply_kw)s)
            e
        """
            % {
                "name": name,
                "translate": translate_str,
                "args": outer_args,
                "apply_kw": inner_args,
                "doc": fn.__doc__,
            }
        )
        lcl = {}
        exec(func_text, globals_, lcl)
        return lcl[name]


def _with_legacy_names(translations):
    def decorate(fn):
        fn._legacy_translations = translations
        return fn

    return decorate


def rev_id():
    return uuid.uuid4().hex[-12:]


def to_tuple(x, default=None):
    if x is None:
        return default
    elif isinstance(x, string_types):
        return (x,)
    elif isinstance(x, Iterable):
        return tuple(x)
    else:
        return (x,)


def dedupe_tuple(tup):
    return tuple(unique_list(tup))


class Dispatcher:
    def __init__(self, uselist=False):
        self._registry = {}
        self.uselist = uselist

    def dispatch_for(self, target, qualifier="default"):
        def decorate(fn):
            if self.uselist:
                self._registry.setdefault((target, qualifier), []).append(fn)
            else:
                assert (target, qualifier) not in self._registry
                self._registry[(target, qualifier)] = fn
            return fn

        return decorate

    def dispatch(self, obj, qualifier="default"):

        if isinstance(obj, string_types):
            targets = [obj]
        elif isinstance(obj, type):
            targets = obj.__mro__
        else:
            targets = type(obj).__mro__

        for spcls in targets:
            if qualifier != "default" and (spcls, qualifier) in self._registry:
                return self._fn_or_list(self._registry[(spcls, qualifier)])
            elif (spcls, "default") in self._registry:
                return self._fn_or_list(self._registry[(spcls, "default")])
        else:
            raise ValueError("no dispatch function for object: %s" % obj)

    def _fn_or_list(self, fn_or_list):
        if self.uselist:

            def go(*arg, **kw):
                for fn in fn_or_list:
                    fn(*arg, **kw)

            return go
        else:
            return fn_or_list

    def branch(self):
        """Return a copy of this dispatcher that is independently
        writable."""

        d = Dispatcher()
        if self.uselist:
            d._registry.update(
                (k, [fn for fn in self._registry[k]]) for k in self._registry
            )
        else:
            d._registry.update(self._registry)
        return d
