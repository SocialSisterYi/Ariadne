# Twilight - 混合式消息链处理器

> 本模块名字取自 [`My Little Pony`](https://mlp.fandom.com/wiki/My_Little_Pony_Friendship_is_Magic_Wiki) 中的 [`Twilight Sparkle`](https://mlp.fandom.com/wiki/Twilight_Sparkle).
>
> Friendship is magic!

## 缘起

想必 [`v4`](../../appendix/terms/#v4) 用户都或多或少的知道 `Kanata` 吧.

其介绍的 正则表达式 参数提取/关键字匹配 非常的有趣, 而 `Twilight` 在其基础上增加了对 `argparse` 中部分功能的支持.

## 快速开始

```py
from graia.ariadne.message.parser.twilight import Twilight, FullMatch, ParamMatch, RegexResult

twilight = Twilight([FullMatch("指令"), ParamMatch() @ "param"])

@broadcast.receiver(GroupMessage, dispatchers=[twilight])
async def twilight_handler(event: GroupMessage, app: Ariadne, param: RegexResult):
    await app.sendMessage(event, "收到指令: " + param.result)
```

接下来, 让我们解析一下这段代码:

## 创建 Twilight

```py
twilight = Twilight([FullMatch("指令"), ParamMatch() @ "param"])
```

这里说明我们需要匹配内容为 "指令 xxx" 的消息, 并且把 "xxx" 作为参数传递给 `param` 变量.

`Twilight` 接受一个由 `Match` 组成的列表, 之后对于每条消息利用 [`re`][] 的正则表达式与 [`argparse`][argparse] 进行解析

!!! info "手动生成"

    在 `Twilight` 对象上调用 `generate(message_chain)` 即可手动生成
    [`Sparkle`][graia.ariadne.message.parser.twilight.Sparkle] 而无需配合 `Broadcast`.

    这对于本地调试很有用.
### 分配参数

```python
ParamMatch() @ "param"
```

这一段的 `ParamMatch() @ "param"` 说明这个参数传递给函数内的 `param` 形参.

也就是 `param: RegexResult` 这里.

与此同时, `#!py ParamMatch().param("param")` 与这个用法等效.

## Match

### RegexMatch

[`RegexMatch`][graia.ariadne.message.parser.twilight.RegexMatch] 是 `Twilight` 的基础, 它可以匹配指定的正则表达式.

[`FullMatch`][graia.ariadne.message.parser.twilight.FullMatch]
[`UnionMatch`][graia.ariadne.message.parser.twilight.UnionMatch]
[`ParamMatch`][graia.ariadne.message.parser.twilight.ParamMatch]
[`WildcardMatch`][graia.ariadne.message.parser.twilight.WildcardMatch]
都是基于 [`RegexMatch`][graia.ariadne.message.parser.twilight.RegexMatch] 的包装类.

- `FullMatch`: 完整匹配内容
- `UnionMatch`: 匹配多个内容
- `ParamMatch`: 匹配指定参数
- `WildcardMatch`: 匹配任意内容

#### flags 方法

可以通过 [`flags`][graia.ariadne.message.parser.twilight.RegexMatch.flags] 方法设置正则表达式的匹配标记.

```pycon
>>> RegexMatch(r"\d+ # digits").flags(re.V) # 设置 re.VERBOSE 标记
```

#### space 方法

[`SpacePolicy`][graia.ariadne.message.parser.twilight.SpacePolicy] 是一个 [`enum.Enum`][enum.Enum] 类, 有如下常量:

- `NOSPACE`: 不附带尾随空格.
- `PRESERVE`: 预留尾随空格. (默认)
- `FORCE`: 强制需要尾随空格.

它们应被作为 **不透明对象** 使用.

[`SpacePolicy`][graia.ariadne.message.parser.twilight.SpacePolicy]
应该传递给
[`RegexMatch.space`][graia.ariadne.message.parser.twilight.RegexMatch.space]
方法, 用于确定 `RegexMatch` 尾随空格策略.

### ArgumentMatch

`ArgumentMatch` 思路与 `RegexMatch` 不同, 它基于 [argparse][] 进行参数解析.

[`ArgumentMatch`][graia.ariadne.message.parser.twilight.ArgumentMatch]
的初始化方法与 [add_argument][argparse.ArgumentParser.add_argument] 非常相似.

受限于篇幅, 这里没法详细展开. 只能给出几个用例:

```pycon
>>> ArgumentMatch("-s", "--switch", action="store_true") # 开关
>>> ArgumentMatch("-o", "--opt", type=str, choices=["head", "body"]) # 只允许 "head" 或 "body"
>>> ArgumentMatch("-m", choices=MessageChain(["choice_a", "choice_b"])) # 注意默认是 MessageChain, 所以要这样写
```

## 配合 Broadcast 使用

`Twilight` 应作为 `dispatcher` 传入 `broadcast.receiver` / `ListenerSchema` 中.

在 `receiver` 函数的类型标注中, 通过 标注参数为 `Sparkle` 获取当前 `Sparkle`, 通过 `name: Match` 的形式获取 `name` 对应的匹配对象.

像这样:

```py
@broadcast.receiver(MessageEvent, dispatchers=[
        Twilight(
            [
                FullMatch(".command"),
                "arg" @ RegexMatch(r"\d+", optional=True)
            ]
        )
    ]
)
async def reply(..., arg: RegexResult):
    ...
```

!!! note "使用 `Sparkle`, `Match`, `MatchResult` 的子类进行标注都是可以的."

一旦匹配失败 (`generate` 抛出异常), `Broadcast` 的本次执行就会被取消.

### MatchResult

`RegexResult` 与 `ArgResult` 都是 [`MatchResult`][graia.ariadne.message.parser.twilight.MatchResult] 的子类.

这二者方便地标注了匹配结果信息.

`MatchResult` 的属性:

- `MatchResult.matched`: 对应的 `Match` 对象是否匹配.
- `MatchResult.origin`: 原始 `Match` 对象.
- `MatchResult.result`: 匹配结果.