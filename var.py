from enum import Enum
from asyncio import Lock
from pydantic import BaseModel


class Platform(Enum):
    b_id = 0
    qu_id = 1


class NoticeType(Enum):
    jjc = 0
    pjjc = 1
    online = 2


class Priority(Enum):
    query_all = 10
    detial_query = 2
    query_user = 3
    bind = 4
    query_talent = 5


class BaseSet(Enum):
    b_max_pri = 20
    qu_max_pri = 20


class LoadBase(BaseModel):
    b_group_user: int
    qu_group_user: int
    b_group_pcrid: int
    qu_group_pcrid: int
    b_private_user: int
    qu_private_user: int
    b_private_pcrid: int
    qu_private_pcrid: int
    b_today_send: int
    qu_today_send: int
    b_yesterday_send: int
    qu_yesterday_send: int


queue_dict = {
    Platform.b_id.value: False,
    Platform.qu_id.value: False,
}

private_dict = {
    Platform.b_id.value: BaseSet.b_max_pri.value,
    Platform.qu_id.value: BaseSet.qu_max_pri.value,
}

platform_dict = {
    Platform.qu_id.value: "渠",
    "渠": Platform.qu_id.value,
}

cache = {}
lck = Lock()
query_cache = {}
talent_cache = {}
jjc_log = {Platform.b_id.value: [], Platform.qu_id.value: []}
