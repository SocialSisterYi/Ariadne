"""Ariadne 各种 model 存放的位置"""
import functools
import json
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Type, Union

from loguru import logger
from pydantic import BaseConfig, BaseModel, Extra, Field, validator
from pydantic.networks import AnyHttpUrl
from typing_extensions import Literal
from yarl import URL

from .util import gen_subclass, internal_cls

if TYPE_CHECKING:
    from .app import Ariadne
    from .event import MiraiEvent
    from .message.chain import MessageChain
    from .typing import AbstractSetIntStr, DictStrAny, MappingIntStrAny


def datetime_encoder(v: datetime) -> float:
    """编码 datetime 对象

    Args:
        v (datetime): datetime 对象

    Returns:
        float: 编码后的 datetime (时间戳)
    """
    return v.timestamp()


class DatetimeEncoder(json.JSONEncoder):
    """可以编码 datetime 的 JSONEncoder"""

    def default(self, o):
        return int(o.timestamp()) if isinstance(o, datetime) else super().default(o)


class AriadneBaseModel(BaseModel):
    """
    Ariadne 一切数据模型的基类.
    """

    def dict(
        self,
        *,
        include: Union[None, "AbstractSetIntStr", "MappingIntStrAny"] = None,
        exclude: Union[None, "AbstractSetIntStr", "MappingIntStrAny"] = None,
        by_alias: bool = False,
        skip_defaults: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
    ) -> "DictStrAny":
        _, *_ = by_alias, exclude_none, skip_defaults
        return super().dict(
            include=include,  # type: ignore
            exclude=exclude,  # type: ignore
            by_alias=True,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=True,
        )

    class Config(BaseConfig):
        """Ariadne BaseModel 设置"""

        extra = Extra.allow
        json_encoders = {
            datetime: datetime_encoder,
        }
        arbitrary_types_allowed = True


class LogConfig(Dict[Type["MiraiEvent"], str]):
    def __init__(self, log_level: str = "INFO"):
        from .event.message import (
            ActiveMessage,
            FriendMessage,
            GroupMessage,
            OtherClientMessage,
            StrangerMessage,
            TempMessage,
        )

        self.log_level: str = log_level

        account_seg = "{ariadne.account}"
        msg_chain_seg = "{event.messageChain.safe_display}"
        sender_seg = "{event.sender.name}({event.sender.id})"
        user_seg = "{event.sender.nickname}({event.sender.id})"
        group_seg = "{event.sender.group.name}({event.sender.group.id})"
        client_seg = "{event.sender.platform}({event.sender.id})"
        self[GroupMessage] = f"{account_seg}: [{group_seg}] {sender_seg} -> {msg_chain_seg}"
        self[TempMessage] = f"{account_seg}: [{group_seg}.{sender_seg}] -> {msg_chain_seg}"
        self[FriendMessage] = f"{account_seg}: [{user_seg}] -> {msg_chain_seg}"
        self[StrangerMessage] = f"{account_seg}: [{user_seg}] -> {msg_chain_seg}"
        self[OtherClientMessage] = f"{account_seg}: [{client_seg}] -> {msg_chain_seg}"
        for active_msg_cls in gen_subclass(ActiveMessage):
            sync_label: str = "[SYNC] " if active_msg_cls.__fields__["sync"].default else ""
            self[active_msg_cls] = f"{account_seg}: {sync_label}[{{event.subject}}] <- {msg_chain_seg}"

    def event_hook(self, app: "Ariadne") -> Callable[["MiraiEvent"], Awaitable[None]]:
        return functools.partial(self.log, app)

    async def log(self, app: "Ariadne", event: "MiraiEvent") -> None:
        fmt = self.get(type(event))
        if fmt:
            logger.log(self.log_level, fmt.format(event=event, ariadne=app))


class MiraiSession(AriadneBaseModel):
    """
    用于描述与上游接口会话, 并存储会话状态的实体类.

    Attributes:
        host (AnyHttpUrl): `mirai-api-http` 服务所在的根接口地址
        account (int): 应用所使用账号的整数 ID, 虽然启用 `singleMode` 时不需要, 但仍然建议填写.
        verify_key (str): 在 `mirai-api-http` 配置流程中定义, 需为相同的值以通过安全验证, 需在 mirai-api-http 配置里启用 `enableVerify`.
        session_key (str, optional): 会话标识, 即会话中用于进行操作的唯一认证凭证.
    """

    host: Optional[AnyHttpUrl]
    """链接地址, 以 http 开头, 作为服务器连接时应为 None"""

    single_mode: bool = False
    """mirai-console 是否开启 single_mode (单例模式)"""

    account: Optional[int] = None
    """账号"""

    verify_key: Optional[str] = None
    """mirai-api-http 配置的 VerifyKey 字段"""

    session_key: Optional[str] = None
    """会话标识"""

    version: Optional[str] = None
    """mirai-api-http 的版本"""

    def __init__(
        self,
        host: Optional[Union[AnyHttpUrl, str]] = None,
        account: Optional[Union[int, str]] = None,
        verify_key: Optional[str] = None,
        *,
        single_mode: bool = False,
    ) -> None:
        super().__init__(
            host=host,  # type: ignore
            account=account,  # type: ignore
            verify_key=verify_key,  # type: ignore
            single_mode=single_mode,  # type: ignore
        )

    def url_gen(self, route: str) -> str:
        """生成 route 对应的 API URI

        Args:
            route (str): route 地址

        Returns:
            str: 对应的 API URI
        """
        if self.host is None:
            raise ValueError("Remote host is unset")
        return str(URL(self.host) / route)


@functools.total_ordering
class MemberPerm(Enum):
    """描述群成员在群组中所具备的权限"""

    Member = "MEMBER"  # 普通成员
    Administrator = "ADMINISTRATOR"  # 管理员
    Owner = "OWNER"  # 群主

    def __str__(self) -> str:
        return self.value

    def __lt__(self, other: "MemberPerm"):
        lv_map = {MemberPerm.Member: 1, MemberPerm.Administrator: 2, MemberPerm.Owner: 3}
        return lv_map[self] < lv_map[other]

    def __repr__(self) -> str:
        perm_map: Dict[str, str] = {
            "MEMBER": "<普通成员>",
            "ADMINISTRATOR": "<管理员>",
            "OWNER": "<群主>",
        }
        return perm_map[self.value]


@internal_cls()
class Group(AriadneBaseModel):
    """描述 Tencent QQ 中的群组."""

    id: int
    """群号"""

    name: str
    """群名"""

    accountPerm: MemberPerm = Field(..., alias="permission")
    """你在群中的权限"""

    def __int__(self):
        return self.id

    def __str__(self) -> str:
        return f"{self.name}({self.id})"

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Group) and self.id == other.id

    async def getConfig(self) -> "GroupConfig":
        """获取该群组的 Config

        Returns:
            Config: 该群组的设置对象.
        """
        from .app import Ariadne

        return await Ariadne.current().getGroupConfig(self)

    async def modifyConfig(self, config: "GroupConfig") -> None:
        """修改该群组的 Config

        Args:
            config (GroupConfig): 经过修改后的群设置对象.
        """
        from .app import Ariadne

        return await Ariadne.current().modifyGroupConfig(self, config)

    async def getAvatar(self, cover: Optional[int] = None) -> bytes:
        """获取该群组的头像
        Args:
            cover (Optional[int]): 群封面标号 (若为 None 则获取该群头像, 否则获取该群封面)

        Returns:
            bytes: 群头像的二进制内容.
        """
        from .app import Ariadne

        cover = (cover or 0) + 1
        rider = await Ariadne.service.http_interface.request(
            "GET", f"http://p.qlogo.cn/gh/{self.id}/{self.id}_{cover}/"
        )
        return await rider.io().read()


@internal_cls()
class Member(AriadneBaseModel):
    """描述用户在群组中所具备的有关状态, 包括所在群组, 群中昵称, 所具备的权限, 唯一ID."""

    id: int
    """QQ 号"""

    name: str = Field(..., alias="memberName")
    """显示名称"""

    permission: MemberPerm
    """群权限"""

    specialTitle: Optional[str] = None
    """特殊头衔"""

    joinTimestamp: Optional[int] = None
    """加入的时间"""

    lastSpeakTimestamp: Optional[int] = None
    """最后发言时间"""

    mutetimeRemaining: Optional[int] = None
    """禁言剩余时间"""

    group: Group
    """所在群组"""

    def __str__(self) -> str:
        return f"{self.name}({self.id} @ {self.group})"

    def __int__(self):
        return self.id

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, (Friend, Member, Stranger)) and self.id == other.id

    async def getProfile(self) -> "Profile":
        """获取该群成员的 Profile

        Returns:
            Profile: 该群成员的 Profile 对象
        """
        from .app import Ariadne

        return await Ariadne.current().getMemberProfile(self)

    async def getInfo(self) -> "MemberInfo":
        """获取该成员的可修改状态

        Returns:
            MemberInfo: 群组成员的可修改状态
        """
        return MemberInfo(name=self.name, specialTitle=self.specialTitle)

    async def modifyInfo(self, info: "MemberInfo") -> None:
        """
        修改群组成员的可修改状态; 需要具有相应权限(管理员/群主).

        Args:
            info (MemberInfo): 已修改的指定群组成员的可修改状态

        Returns:
            None: 没有返回.
        """
        from .app import Ariadne

        return await Ariadne.current().modifyMemberInfo(self, info)

    async def modifyAdmin(self, assign: bool) -> None:
        """
        修改一位群组成员管理员权限; 需要有相应权限(群主)

        Args:
            assign (bool): 是否设置群成员为管理员.

        Returns:
            None: 没有返回.
        """
        from .app import Ariadne

        return await Ariadne.current().modifyMemberAdmin(assign, self)

    async def getAvatar(self, size: Literal[640, 140] = 640) -> bytes:
        """获取该群成员的头像

        Args:
            size (Literal[640, 140]): 头像尺寸

        Returns:
            bytes: 群成员头像的二进制内容.
        """
        from .app import Ariadne

        rider = await Ariadne.service.http_interface.request(
            "GET", f"https://q2.qlogo.cn/headimg_dl?dst_uin={self.id}&spec={size}"
        )

        return await rider.io().read()


@internal_cls()
class Friend(AriadneBaseModel):
    """描述 Tencent QQ 中的好友."""

    id: int
    """QQ 号"""

    nickname: str
    """昵称"""

    remark: str
    """自行设置的代称"""

    def __int__(self):
        return self.id

    def __str__(self) -> str:
        return f"{self.remark}({self.id})"

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, (Friend, Member, Stranger)) and self.id == other.id

    async def getProfile(self) -> "Profile":
        """获取该好友的 Profile

        Returns:
            Profile: 该好友的 Profile 对象
        """
        from .app import Ariadne

        return await Ariadne.current().getFriendProfile(self)

    async def getAvatar(self, size: Literal[640, 140] = 640) -> bytes:
        """获取该好友的头像

        Args:
            size (Literal[640, 140]): 头像尺寸

        Returns:
            bytes: 好友头像的二进制内容.
        """
        from .app import Ariadne

        rider = await Ariadne.service.http_interface.request(
            "GET", f"https://q2.qlogo.cn/headimg_dl?dst_uin={self.id}&spec={size}"
        )

        return await rider.io().read()


@internal_cls()
class Stranger(AriadneBaseModel):
    """描述 Tencent QQ 中的陌生人."""

    id: int
    """QQ 号"""

    nickname: str
    """昵称"""

    remark: str
    """自行设置的代称"""

    def __int__(self):
        return self.id

    def __str__(self) -> str:
        return f"Stranger({self.id}, {self.nickname})"

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, (Friend, Member, Stranger)) and self.id == other.id

    async def getAvatar(self, size: Literal[640, 140] = 640) -> bytes:
        """获取该陌生人的头像

        Args:
            size (Literal[640, 140]): 头像尺寸

        Returns:
            bytes: 陌生人头像的二进制内容.
        """
        from .app import Ariadne

        rider = await Ariadne.service.http_interface.request(
            "GET", f"https://q2.qlogo.cn/headimg_dl?dst_uin={self.id}&spec={size}"
        )

        return await rider.io().read()


class GroupConfig(AriadneBaseModel):
    """描述群组各项功能的设置."""

    name: str = ""
    """群名"""

    announcement: str = ""
    """群公告"""

    confessTalk: bool = False
    """开启坦白说"""

    allowMemberInvite: bool = False
    """允许群成员直接邀请入群"""

    autoApprove: bool = False
    """自动通过加群申请"""

    anonymousChat: bool = False
    """允许匿名聊天"""


class MemberInfo(AriadneBaseModel):
    """描述群组成员的可修改状态, 修改需要管理员/群主权限."""

    name: str = ""
    """昵称, 与 nickname不同"""

    specialTitle: Optional[str] = ""
    """特殊头衔"""


@internal_cls()
class DownloadInfo(AriadneBaseModel):
    """描述一个文件的下载信息."""

    sha: str = ""
    """文件 SHA256"""

    md5: str = ""
    """文件 MD5"""

    download_times: int = Field(..., alias="downloadTimes")
    """下载次数"""

    uploader_id: int = Field(..., alias="uploaderId")
    """上传者 QQ 号"""

    upload_time: datetime = Field(..., alias="uploadTime")
    """上传时间"""

    last_modify_time: datetime = Field(..., alias="lastModifyTime")
    """最后修改时间"""

    url: Optional[str] = None
    """下载 url"""


@internal_cls()
class Announcement(AriadneBaseModel):
    """群公告"""

    group: Group
    """公告所在的群"""

    senderId: int
    """发送者QQ号"""

    fid: str
    """公告唯一标识ID"""

    allConfirmed: bool
    """群成员是否已全部确认"""

    confirmedMembersCount: int
    """已确认群成员人数"""

    publicationTime: datetime
    """公告发布时间"""


@internal_cls()
class FileInfo(AriadneBaseModel):
    """群组文件详细信息"""

    name: str = ""
    """文件名"""

    path: str = ""
    """文件路径的字符串表示"""

    id: Optional[str] = ""
    """文件 ID"""

    parent: Optional["FileInfo"] = None
    """父文件夹的 FileInfo 对象, 没有则表示存在于根目录"""

    contact: Optional[Union[Group, Friend]] = None
    """文件所在位置 (群组)"""

    is_file: bool = Field(..., alias="isFile")
    """是否为文件"""

    is_directory: bool = Field(..., alias="isDirectory")
    """是否为目录"""

    download_info: Optional[DownloadInfo] = Field(None, alias="downloadInfo")
    """下载信息"""

    @validator("contact", pre=True, allow_reuse=True)
    def _(cls, val: Optional[dict]):
        if not val:
            return None
        if "remark" in val:  # Friend
            return Friend.parse_obj(val)
        return Group.parse_obj(val)  # Group


FileInfo.update_forward_refs(FileInfo=FileInfo)


@internal_cls()
class Client(AriadneBaseModel):
    """
    指示其他客户端
    """

    id: int
    """客户端 ID"""

    platform: str
    """平台字符串表示"""


@internal_cls()
class Profile(AriadneBaseModel):
    """
    指示某个用户的个人资料
    """

    nickname: str
    """昵称"""

    email: Optional[str]
    """电子邮件地址"""

    age: Optional[int]
    """年龄"""

    level: int
    """QQ 等级"""

    sign: str
    """个性签名"""

    sex: Literal["UNKNOWN", "MALE", "FEMALE"]
    """性别"""


class BotMessage(AriadneBaseModel):
    """
    指示 Bot 发出的消息.
    """

    messageId: int
    """消息 ID"""

    origin: Optional["MessageChain"]
    """原始消息链 (发送的消息链)"""


class AriadneStatus(Enum):
    """指示 Ariadne 状态的枚举类"""

    STOP = "stop"
    """已停止"""

    LAUNCH = "launch"
    """正在启动"""

    RUNNING = "running"
    """正常运行"""

    SHUTDOWN = "shutdown"
    """刚开始关闭"""

    CLEANUP = "cleanup"
    """清理残留任务"""
