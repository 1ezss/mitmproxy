"""
    This module manges and invokes typed commands.
"""
import inspect
import types
import io
import typing
import shlex
import textwrap
import functools
import sys

from mitmproxy.utils import typecheck
from mitmproxy import exceptions
import mitmproxy.types


def lexer(s):
    # mypy mis-identifies shlex.shlex as abstract
    lex = shlex.shlex(s)  # type: ignore
    lex.wordchars += "."
    lex.whitespace_split = True
    lex.commenters = ''
    return lex


def typename(t: type) -> str:
    """
        Translates a type to an explanatory string.
    """
    to = mitmproxy.types.CommandTypes.get(t, None)
    if not to:
        raise NotImplementedError(t)
    return to.display


class Command:
    def __init__(self, manager, path, func) -> None:
        self.path = path
        self.manager = manager
        self.func = func
        sig = inspect.signature(self.func)
        self.help = None
        if func.__doc__:
            txt = func.__doc__.strip()
            self.help = "\n".join(textwrap.wrap(txt))

        self.has_positional = False
        for i in sig.parameters.values():
            # This is the kind for *args paramters
            if i.kind == i.VAR_POSITIONAL:
                self.has_positional = True
        self.paramtypes = [v.annotation for v in sig.parameters.values()]
        self.returntype = sig.return_annotation

    def paramnames(self) -> typing.Sequence[str]:
        v = [typename(i) for i in self.paramtypes]
        if self.has_positional:
            v[-1] = "*" + v[-1]
        return v

    def retname(self) -> str:
        return typename(self.returntype) if self.returntype else ""

    def signature_help(self) -> str:
        params = " ".join(self.paramnames())
        ret = self.retname()
        if ret:
            ret = " -> " + ret
        return "%s %s%s" % (self.path, params, ret)

    def call(self, args: typing.Sequence[str]) -> typing.Any:
        """
            Call the command with a list of arguments. At this point, all
            arguments are strings.
        """
        if not self.has_positional and (len(self.paramtypes) != len(args)):
            raise exceptions.CommandError("Usage: %s" % self.signature_help())

        remainder = []  # type: typing.Sequence[str]
        if self.has_positional:
            remainder = args[len(self.paramtypes) - 1:]
            args = args[:len(self.paramtypes) - 1]

        pargs = []
        for arg, paramtype in zip(args, self.paramtypes):
            if typecheck.check_command_type(arg, paramtype):
                pargs.append(arg)
            else:
                pargs.append(parsearg(self.manager, arg, paramtype))

        if remainder:
            chk = typecheck.check_command_type(
                remainder,
                typing.Sequence[self.paramtypes[-1]]  # type: ignore
            )
            if chk:
                pargs.extend(remainder)
            else:
                raise exceptions.CommandError("Invalid value type: %s - expected %s" % (remainder, self.paramtypes[-1]))

        with self.manager.master.handlecontext():
            ret = self.func(*pargs)

        if not typecheck.check_command_type(ret, self.returntype):
            raise exceptions.CommandError("Command returned unexpected data")

        return ret


ParseResult = typing.NamedTuple(
    "ParseResult",
    [("value", str), ("type", typing.Type)],
)


class CommandManager:
    def __init__(self, master):
        self.master = master
        self.commands = {}

    def collect_commands(self, addon):
        for i in dir(addon):
            if not i.startswith("__"):
                o = getattr(addon, i)
                if hasattr(o, "command_path"):
                    self.add(o.command_path, o)

    def add(self, path: str, func: typing.Callable):
        self.commands[path] = Command(self, path, func)

    def parse_partial(self, cmdstr: str) -> typing.Sequence[ParseResult]:
        """
            Parse a possibly partial command. Return a sequence of (part, type) tuples.
        """
        buf = io.StringIO(cmdstr)
        parts = []  # type: typing.List[str]
        lex = lexer(buf)
        while 1:
            remainder = cmdstr[buf.tell():]
            try:
                t = lex.get_token()
            except ValueError:
                parts.append(remainder)
                break
            if not t:
                break
            parts.append(t)
        if not parts:
            parts = [""]
        elif cmdstr.endswith(" "):
            parts.append("")

        parse = []  # type: typing.List[ParseResult]
        params = []  # type: typing.List[type]
        typ = None  # type: typing.Type
        for i in range(len(parts)):
            if i == 0:
                typ = mitmproxy.types.Cmd
                if parts[i] in self.commands:
                    params.extend(self.commands[parts[i]].paramtypes)
            elif params:
                typ = params.pop(0)
                # FIXME: Do we need to check that Arg is positional?
                if typ == mitmproxy.types.Cmd and params and params[0] == mitmproxy.types.Arg:
                    if parts[i] in self.commands:
                        params[:] = self.commands[parts[i]].paramtypes
            else:
                typ = str
            parse.append(ParseResult(value=parts[i], type=typ))
        return parse

    def call_args(self, path: str, args: typing.Sequence[str]) -> typing.Any:
        """
            Call a command using a list of string arguments. May raise CommandError.
        """
        if path not in self.commands:
            raise exceptions.CommandError("Unknown command: %s" % path)
        return self.commands[path].call(args)

    def call(self, cmdstr: str):
        """
            Call a command using a string. May raise CommandError.
        """
        parts = list(lexer(cmdstr))
        if not len(parts) >= 1:
            raise exceptions.CommandError("Invalid command: %s" % cmdstr)
        return self.call_args(parts[0], parts[1:])

    def dump(self, out=sys.stdout) -> None:
        cmds = list(self.commands.values())
        cmds.sort(key=lambda x: x.signature_help())
        for c in cmds:
            for hl in (c.help or "").splitlines():
                print("# " + hl, file=out)
            print(c.signature_help(), file=out)
            print(file=out)


def parsearg(manager: CommandManager, spec: str, argtype: type) -> typing.Any:
    """
        Convert a string to a argument to the appropriate type.
    """
    t = mitmproxy.types.CommandTypes.get(argtype, None)
    if not t:
        raise exceptions.CommandError("Unsupported argument type: %s" % argtype)
    try:
        return t.parse(manager, argtype, spec)  # type: ignore
    except exceptions.TypeError as e:
        raise exceptions.CommandError from e


def verify_arg_signature(f: typing.Callable, args: list, kwargs: dict) -> None:
    sig = inspect.signature(f)
    try:
        sig.bind(*args, **kwargs)
    except TypeError as v:
        raise exceptions.CommandError("Argument mismatch: %s" % v.args[0])


def command(path):
    def decorator(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            verify_arg_signature(function, args, kwargs)
            return function(*args, **kwargs)
        wrapper.__dict__["command_path"] = path
        return wrapper
    return decorator


def argument(name, type):
    """
        Set the type of a command argument at runtime. This is useful for more
        specific types such as mitmproxy.types.Choice, which we cannot annotate
        directly as mypy does not like that.
    """
    def decorator(f: types.FunctionType) -> types.FunctionType:
        assert name in f.__annotations__
        f.__annotations__[name] = type
        return f
    return decorator
