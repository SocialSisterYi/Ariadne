"""Commander: 便捷的指令触发体系"""
import abc
import asyncio
import inspect
from typing import (
    Any,
    Callable,
    Coroutine,
    Deque,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Literal,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from graia.broadcast import Broadcast, Listener
from graia.broadcast.entities.decorator import Decorator
from graia.broadcast.entities.exectarget import ExecTarget
from graia.broadcast.typing import T_Dispatcher
from pydantic import BaseModel, create_model, validator
from pydantic.fields import ModelField
from typing_extensions import LiteralString

from ...context import event_ctx
from ...dispatcher import ContextDispatcher
from ...event.message import MessageEvent
from ...model import AriadneBaseModel
from ...typing import MaybeFlag, Sentinel, Wrapper
from ...util import (
    constant,
    gen_subclass,
    get_stack_namespace,
    resolve_dispatchers_mixin,
)
from ..chain import MessageChain
from ..element import Element
from .util import (
    AnnotatedParam,
    ConstantDispatcher,
    MatchEntry,
    MatchNode,
    Param,
    Text,
    convert_empty,
    raw,
    split,
    tokenize,
)

T_Callable = TypeVar("T_Callable", bound=Callable)


def chain_validator(value: MessageChain, field: ModelField) -> Union[MessageChain, Element, str]:
    """
    MessageChain 处理函数.
    应用作 pydantic 的 Model validator.
    取决于字段类型标注, 若与消息链, 消息元素无关则会直接把消息链用 asDisplay 转换为字符串.

    Args:
        value (MessageChain): 消息链
        field (ModelField): 当前的 model 字段

    Returns:
        Union[MessageChain, Element, str]: 取决于字段类型标注
    """
    if field.outer_type_ is MessageChain:
        return value
    if issubclass(field.type_, Element):
        assert len(value) == 1
        assert isinstance(value[0], field.type_)
        return value[0]
    if isinstance(value, MessageChain):
        return str(value)
    if value is None:
        return field.default
    return value


class ParamDesc(abc.ABC):
    model: Optional[Type[BaseModel]]
    dest: Optional[str]
    default_factory: MaybeFlag[Callable[[], Any]]

    @abc.abstractmethod
    def gen_model(self, validators: Iterable[Callable]) -> None:
        """生成用于 pydantic 解析的 model 属性

        Args:
            validators (Iterable[Callable]): 用作 validator 的 Callable 可迭代对象
        """
        ...


def make_model(name: str, validators: Iterable[Callable] = (), **fields) -> Type[BaseModel]:
    return create_model(
        name,
        __validators__={
            f"#validator_{i}#": validator("*", pre=True, allow_reuse=True)(v)
            for i, v in enumerate(validators)
        },
        **{k: (v, ...) for k, v in fields.items()},
    )


class Slot(ParamDesc):
    """Slot"""

    def __init__(
        self,
        target: Union[str, int],
        type: MaybeFlag[Type] = Sentinel,
        default: MaybeFlag[Any] = Sentinel,
        default_factory: MaybeFlag[Callable[[], Any]] = Sentinel,
    ) -> None:
        self.target = str(target)
        self.type: Union[Literal[Sentinel, raw], Type] = type
        if self.type == "raw":
            self.type = raw
        if self.type is Sentinel:
            self.type = MessageChain
        self.default_factory = constant(default) if default is not Sentinel else default_factory
        self.dest: Optional[str] = None
        self.model: Optional[Type[BaseModel]] = None

    def gen_model(self, validators: Iterable[Callable]) -> None:
        if self.model or self.type is raw:
            return
        self.model = make_model("SlotModel", validators, val=self.type)

    def merge(self, other: "Slot") -> None:
        if self.type is Sentinel and other.type is not Sentinel:
            self.type = other.type
        if self.default_factory is Sentinel and other.default_factory is not Sentinel:
            self.default_factory = other.default_factory


class Arg(ParamDesc):
    """Argument"""

    headers: FrozenSet[str]

    def __init__(
        self,
        pattern: str,
        type: MaybeFlag[Type] = Sentinel,
        default: MaybeFlag[Any] = Sentinel,
        default_factory: MaybeFlag[Callable[[], Any]] = Sentinel,
    ) -> None:

        self.tags: List[str] = []
        self.dest: Optional[str] = None
        self.type: MaybeFlag[Type] = Sentinel
        self.model: Optional[Type[BaseModel]] = None
        iter_tokens = iter(tokenize(pattern))
        headers = next(iter_tokens)
        assert isinstance(headers, Text), "Required argument pattern!"
        self.headers = headers.choice
        for token in iter_tokens:
            assert isinstance(token, Param), "Argument pattern can only be presented at header!"
            assert len(token.names) == 1, "Arg param cannot have alias!"
            self.tags.append(next(iter(token.names)))
        nargs = len(self.tags)
        if nargs == 0:
            self.type = bool
            self.default_factory = constant(False)
        elif nargs == 1:
            self.type = MessageChain
        if default is not Sentinel:
            self.default_factory = constant(default)
        elif default_factory is not Sentinel:
            self.default_factory = default_factory
        if type is not Sentinel:
            self.type = type

    def gen_model(self, validators: Iterable[Callable]) -> None:
        if self.model:
            return
        if (
            isinstance(self.type, type)
            and issubclass(self.type, BaseModel)
            and not issubclass(self.type, AriadneBaseModel)
        ):
            self.model = self.type
            return

        nargs = len(self.tags)
        if nargs == 0:  # Set default
            self.model = make_model("ArgModel", validators, val=self.type)
        elif nargs == 1:
            self.model = make_model("ArgModel", validators, **{self.tags[0]: self.type})
        if self.model is None:
            raise ValueError(f"You didn't supply a suitable model for {self.dest}!")

    def update(self, annotation: MaybeFlag[Any], default: MaybeFlag[Any]) -> None:
        if self.type is Sentinel and annotation is not Sentinel:
            self.type = annotation
        if self.default_factory is Sentinel and default is not Sentinel:
            self.default_factory = constant(default)

    def __repr__(self) -> str:
        return f"Arg([{'|'.join(self.headers)}]{''.join(f' {{{tag}}}' for tag in self.tags)}"


class CommandEntry(MatchEntry, ExecTarget):
    """命令信息的存储数据结构, 同时可作为 ExecTarget"""

    def __init__(self, priority: int) -> None:
        self.priority = priority
        self.slot_map: Dict[str, Slot] = {}
        self.arg_map: Dict[str, Arg] = {}
        self.targets: Set[str] = set()
        self.header_map: Dict[str, Arg] = {}
        self.extra: Optional[AnnotatedParam] = None
        self._arg_name_map: Optional[Dict[Arg, str]] = None

    @property
    def arg_name_map(self) -> Dict[Arg, str]:
        if not self._arg_name_map:
            self._arg_name_map = {v: k for k, v in self.arg_map.items()}
        return self._arg_name_map.copy()

    def compile_param(
        self,
        slot_data: Dict[str, MessageChain],
        arg_data: Dict[str, List[MessageChain]],
        extra_list: List[MessageChain],
    ) -> Dict[str, Any]:
        ...  # TODO


class MismatchError(ValueError):
    """指令失配"""


class ParseData(NamedTuple):
    index: int
    node: MatchNode[CommandEntry]
    params: Tuple[MessageChain, ...]


class Commander:
    """便利的指令触发体系"""

    def __init__(self, broadcast: Broadcast, listen: bool = True):
        """
        Args:
            broadcast (Broadcast): 事件系统
            listen (bool): 是否监听消息事件
        """
        self.broadcast = broadcast
        self.validators: List[Callable] = [chain_validator]
        self.match_root: MatchNode[CommandEntry] = MatchNode()

        if listen:
            self.broadcast.listeners.append(
                Listener(
                    self.execute,
                    self.broadcast.getDefaultNamespace(),
                    list(gen_subclass(MessageEvent)),
                )
            )

    def __del__(self):
        self.broadcast.listeners = [i for i in self.broadcast.listeners if i.callable != self.execute]

    def add_type_cast(self, *caster: Callable):
        """添加类型验证器 (type caster / validator)"""
        self.validators = [*reversed(caster), *self.validators]

    @staticmethod
    def parse_command(command: LiteralString, entry: CommandEntry) -> None:
        """从传入的命令补充 entry 的信息

        Args:
            command (LiteralString): 命令
            entry (CommandEntry): 命令的 entry
        """
        tokenize_result: List[Union[Text, Param, AnnotatedParam]] = tokenize(command)
        for token in tokenize_result:
            if isinstance(token, Text):
                assert all(
                    pattern not in entry.header_map for pattern in token.choice
                ), f"{token} conflicts with an Arg object!"

            elif isinstance(token, AnnotatedParam):
                if token.wildcard or token.default:
                    assert token is tokenize_result[-1], "Not setting wildcard / optional on the last slot!"
                    entry.extra = token
                assert token.name not in entry.targets, "Duplicated parameter slot!"
                entry.targets.add(token.name)
                parsed_slot = Slot(
                    token.name,
                    eval(
                        token.annotation or "_sentinel",
                        *get_stack_namespace(2, {"raw": raw, "_sentinel": Sentinel}),
                    ),
                    eval(token.default or "_sentinel", *get_stack_namespace(2, {"_sentinel": Sentinel})),
                )
                parsed_slot.dest = token.name  # assuming that param_name is consistent
                entry.slot_map.setdefault(token.name, parsed_slot).merge(
                    parsed_slot
                )  # parsed slot < provided slot
            elif isinstance(token, Param):
                for name in token.names:
                    assert name not in entry.targets, "Duplicated parameter slot!"
                    entry.targets.add(name)
        MatchEntry.__init__(entry, tokenize_result)

    @staticmethod
    def update_from_func(entry: CommandEntry) -> None:
        """从 entry 的 callable 更新 entry 的信息

        Args:
            entry (CommandEntry): 命令的 entry
        """
        for name, parameter in inspect.signature(entry.callable).parameters.items():
            annotation = convert_empty(parameter.annotation)
            default = convert_empty(parameter.default)
            if default is not Sentinel:
                last_token = entry.tokens[-1]
                assert isinstance(last_token, Param), "Expected Param, not Text!"
                assert (
                    entry.slot_map[name].target in last_token.names
                ), "Not setting wildcard / optional on the last slot!"
            if name in entry.targets:
                parsed_slot = Slot(name, annotation, default)
                parsed_slot.dest = name  # assuming that name is consistent
                entry.slot_map.setdefault(name, parsed_slot).merge(parsed_slot)  # parsed slot < provided slot
            if name in entry.arg_map:
                entry.arg_map[name].update(annotation, default)

    def command(
        self,
        command: LiteralString,
        setting: Optional[Dict[str, Union[Slot, Arg]]] = None,
        dispatchers: Sequence[T_Dispatcher] = (),
        decorators: Sequence[Decorator] = (),
        priority: int = 16,
    ) -> Wrapper:
        """装饰一个命令处理函数

        Args:
            command (str): 要处理的命令
            setting (Dict[str, Union[Slot, Arg]], optional): 参数设置.
            dispatchers (Sequence[T_Dispatcher], optional): 可选的额外 Dispatcher 序列.
            decorators (Sequence[Decorator], optional): 可选的额外 Decorator 序列.

        Raises:
            ValueError: 命令格式错误

        Returns:
            Callable[[T_Callable], T_Callable]: 装饰器
        """

        entry = CommandEntry(priority)

        for name, val in (setting or {}).items():
            if isinstance(val, Slot):
                entry.slot_map[val.target] = val
            elif isinstance(val, Arg):
                for header in val.headers:
                    entry.header_map[header] = val
                entry.arg_map[name] = val
            else:
                raise TypeError(f"Unknown setting value: {name} - {val!r}")
            val.dest = name

        Commander.parse_command(command, entry)

        def wrapper(func: T_Callable) -> T_Callable:

            ExecTarget.__init__(
                entry,
                func,
                [
                    ContextDispatcher(),
                    *resolve_dispatchers_mixin(dispatchers),
                ],
                list(decorators),
            )
            Commander.update_from_func(entry)
            for slot in entry.slot_map.values():
                slot.gen_model(self.validators)
                assert slot.dest is not None, "Slot dest is None!"
            for arg in entry.arg_map.values():
                arg.gen_model(self.validators)
                assert arg.dest is not None, "Argument dest is not set!"
                assert arg.default_factory is not Sentinel, f"{arg}'s default factory is not set!"
            if entry.extra:
                entry.nodes.pop()  # the last optional / wildcard token should not be on the MatchGraph

            self.match_root.push(entry)
            return func

        return wrapper

    def parse_rest(
        self,
        frags: List[MessageChain],
        str_frags: List[str],
        params: Tuple[MessageChain, ...],
        entry: CommandEntry,
    ) -> Optional[Tuple[int, Coroutine]]:
        # walks down optional, wildcard and Arg
        # extract slot data based on entry
        slot_data: Dict[str, MessageChain] = {
            name: chain for param, chain in zip(entry.params, params) for name in param.names
        }
        # slam all the rest data inside extra_list
        extra_list: List[MessageChain] = []
        # store MessageChains assigned to Arg in arg_data
        arg_data: Dict[str, List[MessageChain]] = {}
        # index frags
        index: int = 0
        while index < len(frags):
            str_frag: str = str_frags[index]
            if str_frag in entry.header_map:
                arg = entry.header_map[str_frag]
                if arg.dest:
                    if arg.dest in arg_data:  # if the arg is already assigned
                        return
                    arg_data[arg.dest] = frags[index : index + len(arg.tags)]
                index += len(arg.tags)
                if index > len(frags):  # failed
                    return None
            else:
                extra_list.append(frags[index])
            index += 1
        dispatchers: List[T_Dispatcher] = [
            ConstantDispatcher(entry.compile_param(slot_data, arg_data, extra_list))
        ]
        if event := event_ctx.get(None):
            dispatchers.extend(resolve_dispatchers_mixin([event.Dispatcher]))
        return index, self.broadcast.Executor(entry, dispatchers)

    async def execute(self, chain: MessageChain):
        """触发 Commander.

        Args:
            chain (MessageChain): 触发的消息链
        """

        mapping_str, elem_m = chain._to_mapping_str()
        str_frags: List[str] = split(mapping_str)
        chain_frags: List[MessageChain] = [
            MessageChain._from_mapping_string(frag, elem_m) for frag in str_frags
        ]
        pending_exec: Dict[int, List[Coroutine]] = {}
        pending_next: Deque[ParseData] = Deque([ParseData(0, self.match_root, ())])

        def push_pending(index: int, nxt: MatchNode[CommandEntry], params: Tuple[MessageChain, ...]):
            for entry in nxt.entries:
                if res := self.parse_rest(chain_frags[index:], str_frags[index:], params, entry):
                    pending_exec.setdefault(res[0], []).append(res[1])
                pending_next.append(ParseData(index, nxt, params))

        while pending_next:
            index, node, params = pending_next.popleft()
            if index >= len(str_frags):
                continue
            chain_frag: MessageChain = chain_frags[index]
            index += 1
            frag: str = str_frags[index]
            if frag in node.next:
                nxt = node.next[frag]
                push_pending(index, nxt, params)
            if Sentinel in node.next:
                nxt = node.next[Sentinel]
                params += (chain_frag,)
                push_pending(index, nxt, params)

        for _, coros in sorted(pending_exec.items()):
            await asyncio.wait(self.broadcast.loop.create_task(coro) for coro in coros)
