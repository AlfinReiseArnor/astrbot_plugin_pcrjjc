from os.path import dirname, join
from msgpack import packb, unpackb
import asyncio
from random import randint
from json import loads
from hashlib import md5, sha256
from Crypto.Cipher import AES
from base64 import b64encode, b64decode
from .bsgamesdk import bsdkclient
import re
from dateutil.parser import parse
import httpx
import random

# 替换 nonebot 的 logger，适配 AstrBot
from astrbot.api import logger

import time
import json
import secrets
import string
import os
import traceback


curpath = dirname(__file__)

class ApiException(Exception):

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def get_api_root(qudao):
    if qudao == 0:
        return random.choice(
            [
                "https://le1-prod-all-gs-gzlj.bilibiligame.net",
                "https://l2-prod-all-gs-gzlj.bilibiligame.net",
                "https://l3-prod-all-gs-gzlj.bilibiligame.net",
            ]
        )
    elif qudao == 1:
        return random.choice(
            [
                "https://l1-prod-uo-gs-gzlj.bilibiligame.net",
                "https://l2-prod-uo-gs-gzlj.bilibiligame.net/",
                "https://l3-prod-uo-gs-gzlj.bilibiligame.net/",
            ]
        )


# 插件自带的那份 APP-VER 默认值（只读，仅用于配置目录还没缓存时的兜底）
_BUNDLED_VERSION_PATH = join(curpath, "version.txt")


def _get_version_file_path() -> str:
    """APP-VER 版本号缓存路径（P3-7）。

    旧实现直接把版本号写回插件目录下的 client/version.txt：官方 Docker /
    只读挂载 data/plugins 时这一步会抛 PermissionError，而它又没被 try 包住，
    于是 b_login 连续 3 次失败、整个插件的查询全部瘫痪。改写到配置目录。
    """
    try:
        from astrbot.api.star import StarTools
        data_dir = str(StarTools.get_data_dir("astrbot_plugin_pcrjjc"))
    except Exception:
        data_dir = join("data", "plugin_data", "astrbot_plugin_pcrjjc")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        pass
    return join(data_dir, "version.txt")


def _read_app_version() -> str:
    """读取缓存的 APP-VER；配置目录还没有时回退到插件自带那份。"""
    for path in (_get_version_file_path(), _BUNDLED_VERSION_PATH):
        try:
            with open(path, encoding="utf-8") as fp:
                version = fp.read().strip()
        except OSError:
            continue
        if version:
            return version
    return ""


def _write_app_version(version: str) -> None:
    """写回 APP-VER。只读目录等情况下失败仅记日志，绝不让登录中断。"""
    try:
        with open(_get_version_file_path(), "w", encoding="utf-8") as fp:
            print(version, file=fp)
    except OSError as e:
        logger.debug(f"[pcrjjc] APP-VER 缓存写入失败（不影响登录）：{e}")


def _get_session_cache_path() -> str:
    """登录会话缓存路径（与 device.json / frame.json 同目录）"""
    try:
        from astrbot.api.star import StarTools
        data_dir = str(StarTools.get_data_dir("astrbot_plugin_pcrjjc"))
    except Exception:
        data_dir = join("data", "plugin_data", "astrbot_plugin_pcrjjc")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        pass
    return join(data_dir, "session.json")

def _get_device_json_path() -> str:
    try:
        from astrbot.api.star import StarTools
        data_dir = str(StarTools.get_data_dir("astrbot_plugin_pcrjjc"))
    except Exception:
        data_dir = join("data", "plugin_data", "astrbot_plugin_pcrjjc")
    import os
    os.makedirs(data_dir, exist_ok=True)
    return join(data_dir, "device.json")


def init_device_id(clear_id=False):
    dev_path = _get_device_json_path()
    js = {"DEVICE-ID": ""}
    try:
        with open(dev_path, "r", encoding="UTF-8") as f:
            js = json.load(f)
    except Exception:
        pass

    device_id = js.get("DEVICE-ID", "")
    if not device_id or clear_id:
        random_string = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        sha256_str = sha256(random_string.encode("utf-8")).digest()
        timestamp_str = str(time.time()).encode("utf-8")
        device_id = md5(timestamp_str + sha256_str).hexdigest()
        logger.info(f"设备id已生成并保存：{device_id}")
        js["DEVICE-ID"] = device_id
        try:
            with open(dev_path, "w", encoding="UTF-8") as f:
                json.dump(js, f, indent=4, ensure_ascii=False)
        except OSError as e:
            # 只读目录：本次仍可继续用刚生成的 id，只是下次启动会重新生成
            logger.warning(f"[pcrjjc] 设备id保存失败（不影响本次查询）：{e}")
    return device_id


defaultHeaders = {
    "Accept-Encoding": "gzip",
    "User-Agent": "Dalvik/2.1.0 (Linux, U, Android 5.1.1, PCRT00 Build/LMY48Z)",
    "X-Unity-Version": "2018.4.30f1",
    "APP-VER": "99.9.9",
    "BATTLE-LOGIC-VERSION": "4",
    "BUNDLE-VER": "",
    "DEVICE": "2",
    "DEVICE-ID": init_device_id(),
    "DEVICE-NAME": "OPPO PCRT00",
    "EXCEL-VER": "1.0.0",
    "GRAPHICS-DEVICE-NAME": "Adreno (TM) 640",
    "IP-ADDRESS": "10.0.2.15",
    "KEYCHAIN": "",
    "LOCALE": "CN",
    "PLATFORM-OS-VERSION": "Android OS 5.1.1 / API-22 (LMY48Z/rel.se.infra.20200612.100533)",
    "REGION-CODE": "",
    "RES-KEY": "ab00a0a6dd915a052a2ef7fd649083e5",
    "RES-VER": "10002200",
    "SHORT-UDID": "0",
    "CHANNEL-ID": "1",
    "PLATFORM": "2",
    "Connection": "Keep-Alive",
}


class pcrclient:

    def __init__(self, bsclient: bsdkclient):
        self.viewer_id = 0
        self.uid = None
        self.access_key = None
        self.bsdk = bsclient
        self.headers = defaultHeaders.copy()
        self.headers["PLATFORM-ID"] = self.bsdk.platform
        self.client = httpx.AsyncClient()
        # 固定一家入口域名（每个客户端一次，重连时才换）：
        # get_api_root() 原本是「每次请求随机挑 3 个域名之一」，请求被打散到 3 个
        # host 上，keep-alive 连接池无法被充分复用，前几次请求要反复 TLS 握手。
        # 固定后同一条连接可以一直用，只有连接出错重连时才换一个入口。
        self._api_root = (get_api_root(self.bsdk.qudao) or get_api_root(0)).rstrip("/")
        self.call_lock = asyncio.Lock()
        if self.bsdk.qudao == 1:
            self.headers["RES-KEY"] = "d145b29050641dac2f8b19df0afe0e59"
        # 会话缓存：命中则可直接查（省掉一次 B 站登录 + 过码）
        self._session_restored = False
        self._restore_session()

    @staticmethod
    def createkey() -> bytes:
        return bytes(ord("0123456789abcdef"[randint(0, 15)]) for _ in range(32))

    @staticmethod
    def add_to_16(b: bytes) -> bytes:
        n = len(b) % 16
        n = n // 16 * 16 - n + 16
        return b + (n * bytes([n]))

    @staticmethod
    def pack(data: object, key: bytes) -> bytes:
        aes = AES.new(key, AES.MODE_CBC, b"7Fk9Lm3Np8Qr4Sv2")
        return aes.encrypt(pcrclient.add_to_16(packb(data, use_bin_type=False))) + key

    @staticmethod
    def encrypt(data: str, key: bytes) -> bytes:
        aes = AES.new(key, AES.MODE_CBC, b"7Fk9Lm3Np8Qr4Sv2")
        return aes.encrypt(pcrclient.add_to_16(data.encode("utf8"))) + key

    @staticmethod
    def decrypt(data: bytes):
        data = b64decode(data.decode("utf8"))
        aes = AES.new(data[-32:], AES.MODE_CBC, b"7Fk9Lm3Np8Qr4Sv2")
        return aes.decrypt(data[:-32]), data[-32:]

    @staticmethod
    def unpack(data: bytes):
        data = b64decode(data.decode("utf8"))
        aes = AES.new(data[-32:], AES.MODE_CBC, b"7Fk9Lm3Np8Qr4Sv2")
        dec = aes.decrypt(data[:-32])
        return unpackb(dec[: -dec[-1]], strict_map_key=False), data[-32:]

    async def callapi(
        self,
        apiurl: str,
        request: dict,
        crypted: bool = True,
        noerr: bool = True,
        header=False,
    ):
        # 按apiurl创建json文件 保存apiurl request data_headers data
        async with self.call_lock:
            key = pcrclient.createkey()

            try:
                if self.viewer_id is not None:
                    request["viewer_id"] = (
                        b64encode(pcrclient.encrypt(str(self.viewer_id), key))
                        if crypted
                        else str(self.viewer_id)
                    )
                resp = await self.client.post(
                    self._api_root + apiurl,
                    data=(
                        pcrclient.pack(request, key)
                        if crypted
                        else str(request).encode("utf8")
                    ),
                    headers=self.headers,
                    timeout=20,
                )
                # 网关/CDN 错误页、限流页、维护页都是非 200；旧实现直接拿 body
                # 去解码，最终只在日志里留下 “未知错误Incorrect padding”，掩盖真因。
                if resp.status_code != 200:
                    snippet = repr(resp.content[:64])[:120]
                    logger.warning(
                        f"pcrclient: {apiurl} 非 200 响应 HTTP {resp.status_code}，"
                        f"长度 {len(resp.content)}，首段 {snippet}"
                    )
                    raise ApiException(f"接口异常：HTTP {resp.status_code}", 502)
                response = resp.content

                # 只包住“把响应体解成协议数据”这一步：HTML 错误页、空包、被截断的包
                # 会在这里抛 binascii/msgpack 异常，分类成可重试的“响应格式异常”
                try:
                    response = pcrclient.unpack(response)[0] if crypted else loads(response)
                except Exception as e:
                    snippet = repr(response[:64])[:120]
                    raise ApiException(
                        f"响应格式异常：{type(e).__name__}: {e}"
                        f"（长度 {len(response)}，首段 {snippet}）",
                        502,
                    ) from e

                data_headers = response["data_headers"]

                if "sid" in data_headers and data_headers["sid"] != "":
                    t = md5()
                    t.update((data_headers["sid"] + "c!SID!n").encode("utf8"))
                    self.headers["SID"] = t.hexdigest()

                if "request_id" in data_headers:
                    self.headers["REQUEST-ID"] = data_headers["request_id"]
                data = response["data"]
                if not noerr and "server_error" in data:
                    data = data["server_error"]
                    logger.info(f"pcrclient: {apiurl} api failed {data}")
                    raise ApiException(data["message"], data["status"])

                # logger.info(f'pcrclient: {apiurl} api called')
                return (data, data_headers) if header else data
            except ApiException:
                # 已经分类过的错误（服务器明确返回的 / 上面两类）原样向上抛，
                # 否则会被重新包成“未知错误”，status 与真实原因全部丢失
                raise
            except Exception as e:
                logger.debug(f"pcrclient: {apiurl} 请求异常：\n{traceback.format_exc()}")
                raise ApiException(f"未知错误{str(e)}", 501)

    async def aclose(self):
        """关闭底层 httpx 连接池（插件终止/重载时调用，避免连接泄漏）"""
        try:
            await self.client.aclose()
        except Exception:
            pass

    async def reconnect(self):
        """丢弃当前连接池并重建。

        线上出现的 “Server disconnected without sending a response” 是典型的
        keep-alive 连接已被服务端关掉、客户端却还在复用。重建后下一次请求走新连接。
        """
        try:
            await self.client.aclose()
        except Exception:
            pass
        self.client = httpx.AsyncClient()
        # 换一个入口域名：当前这个大概率是连不上的那个
        self._api_root = (get_api_root(self.bsdk.qudao) or get_api_root(0)).rstrip("/")
        self.headers.pop("SID", None)
        self.headers.pop("REQUEST-ID", None)

    # ---------- 登录会话缓存 ----------
    def _session_key(self) -> str:
        return str(getattr(self.bsdk, "account", ""))

    def _restore_session(self):
        """启动时尝试复用上次的 viewer_id，省掉一次完整登录与过码。"""
        if getattr(self.bsdk, "qudao", 0) != 0:
            return  # 渠道服直接走 account/password，无需缓存
        try:
            with open(_get_session_cache_path(), "r", encoding="UTF-8") as f:
                data = json.load(f)
        except Exception:
            return
        entry = data.get(self._session_key())
        if not entry:
            return
        try:
            viewer_id = int(entry.get("viewer_id") or 0)
        except (TypeError, ValueError):
            return
        if viewer_id <= 0:
            return
        self.uid = entry.get("uid")
        self.access_key = entry.get("access_key")
        self.viewer_id = viewer_id
        self._session_restored = True
        logger.info(f"[pcrjjc] 复用上次登录会话（viewer_id={viewer_id}），本次启动跳过过码")

    def _save_session(self):
        if getattr(self.bsdk, "qudao", 0) != 0 or not self.viewer_id:
            return
        path = _get_session_cache_path()
        try:
            try:
                with open(path, "r", encoding="UTF-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data[self._session_key()] = {
                "uid": self.uid,
                "access_key": self.access_key,
                "viewer_id": self.viewer_id,
                "saved_at": int(time.time()),
            }
            with open(path, "w", encoding="UTF-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[pcrjjc] 会话缓存写入失败（不影响查询）：{e}")

    def reset_session(self):
        """缓存会话失效时调用：清掉内存与磁盘缓存，让下次走完整登录。"""
        self._session_restored = False
        self.viewer_id = 0
        self.uid = None
        self.access_key = None
        try:
            path = _get_session_cache_path()
            with open(path, "r", encoding="UTF-8") as f:
                data = json.load(f)
            if data.pop(self._session_key(), None) is not None:
                with open(path, "w", encoding="UTF-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception:
            pass

    async def check_gamestart(self):
        gamestart, data_headers = await self.callapi(
            "/check/game_start",
            {"apptype": 0, "campaign_data": "", "campaign_user": randint(0, 99999)},
            header=True,
        )
        if "store_url" in data_headers:
            # 版本号优先取服务端下发值，取不到就用本地缓存；全程不让文件 IO
            # 打断登录流程（只读插件目录下旧实现会直接 PermissionError）
            matched = re.compile(r"\d\d\.\d\.\d").findall(data_headers["store_url"])
            if matched:
                version = matched[0]
                _write_app_version(version)
            else:
                version = _read_app_version()
            if version:
                defaultHeaders["APP-VER"] = version
                self.headers["APP-VER"] = version
                gamestart, data_headers = await self.callapi(
                    "/check/game_start",
                    {"apptype": 0, "campaign_data": "", "campaign_user": randint(0, 99999)},
                    header=True,
                )

        if "now_tutorial" in gamestart and not gamestart["now_tutorial"]:
            raise ApiException("该账号没过完教程!", 403)

    async def check_dangerous(self):
        lres, data_headers = await self.callapi(
            "/tool/sdk_login",
            {
                "uid": str(self.uid),
                "access_key": self.access_key,
                "channel": "1",
                "platform": self.bsdk.platform,
            },
            header=True,
        )
        if "is_risk" in lres and lres["is_risk"] == 1:
            raise ApiException("账号存在风险", 403)
        if "maintenance_message" in lres:
            match = re.search(
                r"\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d", lres["maintenance_message"]
            ).group()
            raise ApiException("服务器在维护", parse(match))
        self.viewer_id = data_headers["viewer_id"]

    async def login(self):
        self.uid, self.access_key = await self.bsdk.b_login()

        if "REQUEST-ID" in self.headers:
            self.headers.pop("REQUEST-ID")

        manifest = await self.callapi(
            "/source_ini/get_maintenance_status?format=json", {}, False, noerr=True
        )
        # 这段旧实现已被注释掉（以字符串字面量形式保留）；加 r 前缀是为了
        # 里面的 \d 不再被当成转义序列，否则加载时仍会报 SyntaxWarning（P3-13）
        r'''if "maintenance_message" in manifest:
            match = re.search(
                r"\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d", manifest["maintenance_message"]
            ).group()
            raise ApiException("服务器在维护", parse(match))'''

        ver = manifest["required_manifest_ver"]
        logger.info(f"using manifest ver = {ver}")
        self.headers["MANIFEST-VER"] = str(ver)

        await self.check_dangerous()
        await self.check_gamestart()
        self._session_restored = False
        self._save_session()
