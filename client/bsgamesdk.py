import asyncio
import json
import time
import hashlib
from . import rsacr
import urllib
from astrbot.api import logger
import httpx

# ============ AstrBot 适配层（替换 hoshino 的发送接口）============
# main.py 在初始化时调用 register_send_func 注入实现
_private_send_func = None
_group_send_func = None


def register_send_func(private_func=None, group_func=None):
    global _private_send_func, _group_send_func
    if private_func is not None:
        _private_send_func = private_func
    if group_func is not None:
        _group_send_func = group_func


async def private_send(target_id, msg):
    if _private_send_func is None:
        logger.warning(f"[pcrjjc] private_send 未注册，丢弃消息: {msg}")
        return
    await _private_send_func(target_id, msg)


async def group_send(target_id, msg):
    if _group_send_func is None:
        logger.warning(f"[pcrjjc] group_send 未注册，丢弃消息: {msg}")
        return
    await _group_send_func(target_id, msg)
# ================================================================


bililogin = "https://line1-sdk-center-login-sh.biligame.net/"
header = {"User-Agent": "Mozilla/5.0 BSGameSDK",
          "Content-Type": "application/x-www-form-urlencoded",
          "Host": "line1-sdk-center-login-sh.biligame.net"}
captcha_header = {"Content-Type": "application/json",
                  "User-Agent": "pcrjjc2/1.0.0"}

# 手动过码时限（秒）
DEFAULT_GT_WAIT = 90
# 手动过码网站地址（自建站点需提供 GET /?captcha_type=… 与 GET /api/block?userid=…）
# 留空 = 不启用“手动兜底”。旧版本这里写的是 https://yousite.com —— 那只是个占位域名，
# 并不是可用的过码站点，所以这里保持留空，不把占位值当成默认值（P2-7）。
DEFAULT_MANUAL_CAPTCH_SITE = ""
# 自动过码接口根地址。留空 = 使用下面这个内置默认值。
# 这台服务就是旧版（以及上游 pcrjjc2 系列）一直硬编码的公共自动过码接口：
# “查询小号登录触发验证码时，插件默认把 challenge/gt/userid 交给它去过码”用的就是它。
# 想换成自建/其它过码服务，在 WebUI 的 captcha_api 里填上你的根地址即可覆盖。
DEFAULT_AUTO_CAPTCHA_API = "https://pcrd.tencentbot.top"

# 运行时生效值：由 main.py 在插件初始化时通过 configure() 覆盖
manual_captch_site = DEFAULT_MANUAL_CAPTCH_SITE
auto_captcha_api = DEFAULT_AUTO_CAPTCHA_API
gt_wait = DEFAULT_GT_WAIT


def configure(captcha_site=None, captcha_api=None, captcha_wait=None):
    """应用 WebUI 里的过码配置，返回生效后的三元组。

    留空（或非正整数）时回退到内置默认值（不是保留当前值）——
    这样用户在 WebUI 里清空配置后能真正恢复默认，重载也不会沿用上一次的设置。
    地址末尾的 "/" 会被去掉，避免拼出 //api/block 这类双斜杠。
    """
    global manual_captch_site, auto_captcha_api, gt_wait

    manual_captch_site = DEFAULT_MANUAL_CAPTCH_SITE
    auto_captcha_api = DEFAULT_AUTO_CAPTCHA_API
    gt_wait = DEFAULT_GT_WAIT

    site = str(captcha_site or "").strip().rstrip("/")
    if site:
        manual_captch_site = site

    api = str(captcha_api or "").strip().rstrip("/")
    if api:
        auto_captcha_api = api

    try:
        wait = int(captcha_wait or 0)
    except (TypeError, ValueError):
        wait = 0
    if wait > 0:
        gt_wait = wait

    return manual_captch_site, auto_captcha_api, gt_wait


async def sendpost(url, data):
    async with httpx.AsyncClient() as client:
        return (await client.post(url=url, data=data, headers=header, timeout=20)).json()


async def captchaVerifier(gt, challenge, userid):
    if not auto_captcha_api:
        # 没配就快速失败，交给 login() 转手动兜底，别去请求一个空地址
        raise Exception("未配置自动过码接口（WebUI 的 captcha_api）")
    async with httpx.AsyncClient(timeout=30) as AsyncClient:
        try:
            res = await AsyncClient.get(
                url=f"{auto_captcha_api}/geetest_renew?captcha_type=1&challenge={challenge}&gt={gt}&userid={userid}&gs=1",
                headers=captcha_header)
            res = res.json()
            uuid = res["uuid"]
            ccnt = 0
            while (ccnt := ccnt + 1) < 10:
                res = await AsyncClient.get(url=f"{auto_captcha_api}/check/{uuid}",
                                            headers=captcha_header)
                res = res.json()

                if "queue_num" in res:
                    tim = min(int(res['queue_num']), 3) * 10
                    logger.info(f"过码排队，当前有{res['queue_num']}个在前面，等待{tim}s")
                    await asyncio.sleep(tim)
                    continue

                info = res["info"]
                if 'validate' in info:
                    return info["challenge"], info["gt_user_id"], info["validate"]

                if res["info"] in ["fail", "url invalid"]:
                    raise Exception("自动过码失败")

                if res["info"] == "in running":
                    logger.info("正在过码。等待5s")
                    await asyncio.sleep(5)

            raise Exception("自动过码多次失败")
        except Exception as e:
            raise Exception(f"自动过码异常，{e}")


def setsign(data):
    data["timestamp"] = int(time.time())
    data["client_timestamp"] = int(time.time())
    sign = ""
    data2 = ""
    for key in data:
        if key == "pwd":
            pwd = urllib.parse.quote(data["pwd"])
            data2 += f"{key}={pwd}&"
        data2 += f"{key}={data[key]}&"
    for key in sorted(data):
        sign += f"{data[key]}"
    data = sign
    sign = sign + "fe8aac4e02f845b8ad67c427d48bfaf1"
    sign = hashlib.md5(sign.encode()).hexdigest()
    data2 += "sign=" + sign
    return data2


modolrsa = '{"operators":"5","merchant_id":"1","isRoot":"0","domain_switch_count":"0","sdk_type":"1","sdk_log_type":"1","timestamp":"1613035485639","support_abis":"x86,armeabi-v7a,armeabi","access_key":"","sdk_ver":"3.4.2","oaid":"","dp":"1280*720","original_domain":"","imei":"227656364311444","version":"1","udid":"KREhESMUIhUjFnJKNko2TDQFYlZkB3cdeQ==","apk_sign":"e89b158e4bcf988ebd09eb83f5378e87","platform_type":"3","old_buvid":"XZA2FA4AC240F665E2F27F603ABF98C615C29","android_id":"84567e2dda72d1d4","fingerprint":"","mac":"08:00:27:53:DD:12","server_id":"1592","domain":"line1-sdk-center-login-sh.biligame.net","app_id":"1370","version_code":"90","net":"4","pf_ver":"6.0.1","cur_buvid":"XZA2FA4AC240F665E2F27F603ABF98C615C29","c":"1","brand":"Android","client_timestamp":"1613035486888","channel_id":"1","uid":"","game_id":"1370","ver":"2.4.10","model":"MuMu"}'
modollogin = '{"operators":"5","merchant_id":"1","isRoot":"0","domain_switch_count":"0","sdk_type":"1","sdk_log_type":"1","timestamp":"1613035508188","support_abis":"x86,armeabi-v7a,armeabi","access_key":"","sdk_ver":"3.4.2","oaid":"","dp":"1280*720","original_domain":"","imei":"227656364311444","gt_user_id":"fac83ce4326d47e1ac277a4d552bd2af","seccode":"","version":"1","udid":"KREhESMUIhUjFnJKNko2TDQFYlZkB3cdeQ==","apk_sign":"e89b158e4bcf988ebd09eb83f5378e87","platform_type":"3","old_buvid":"XZA2FA4AC240F665E2F27F603ABF98C615C29","android_id":"84567e2dda72d1d4","fingerprint":"","validate":"84ec07cff0d9c30acb9fe46b8745e8df","mac":"08:00:27:53:DD:12","server_id":"1592","domain":"line1-sdk-center-login-sh.biligame.net","app_id":"1370","pwd":"rxwA8J+GcVdqa3qlvXFppusRg4Ss83tH6HqxcciVsTdwxSpsoz2WuAFFGgQKWM1+GtFovrLkpeMieEwOmQdzvDiLTtHeQNBOiqHDfJEKtLj7h1nvKZ1Op6vOgs6hxM6fPqFGQC2ncbAR5NNkESpSWeYTO4IT58ZIJcC0DdWQqh4=","version_code":"90","net":"4","pf_ver":"6.0.1","cur_buvid":"XZA2FA4AC240F665E2F27F603ABF98C615C29","c":"1","brand":"Android","client_timestamp":"1613035509437","channel_id":"1","uid":"","captcha_type":"1","game_id":"1370","challenge":"efc825eaaef2405c954a91ad9faf29a2","user_id":"doo349","ver":"2.4.10","model":"MuMu"}'
modolcaptch = '{"operators":"5","merchant_id":"1","isRoot":"0","domain_switch_count":"0","sdk_type":"1","sdk_log_type":"1","timestamp":"1613035486182","support_abis":"x86,armeabi-v7a,armeabi","access_key":"","sdk_ver":"3.4.2","oaid":"","dp":"1280*720","original_domain":"","imei":"227656364311444","version":"1","udid":"KREhESMUIhUjFnJKNko2TDQFYlZkB3cdeQ==","apk_sign":"e89b158e4bcf988ebd09eb83f5378e87","platform_type":"3","old_buvid":"XZA2FA4AC240F665E2F27F603ABF98C615C29","android_id":"84567e2dda72d1d4","fingerprint":"","mac":"08:00:27:53:DD:12","server_id":"1592","domain":"line1-sdk-center-login-sh.biligame.net","app_id":"1370","version_code":"90","net":"4","pf_ver":"6.0.1","cur_buvid":"XZA2FA4AC240F665E2F27F603ABF98C615C29","c":"1","brand":"Android","client_timestamp":"1613035487431","channel_id":"1","uid":"","game_id":"1370","ver":"2.4.10","model":"MuMu"}'


async def _login(account, password, challenge="", gt_user="", validate=""):
    rsa = await sendpost(bililogin + "api/client/rsa", setsign(json.loads(modolrsa)))
    data = json.loads(modollogin)
    public_key = rsa['rsa_key']
    data["access_key"] = ""
    data["gt_user_id"] = gt_user
    data["uid"] = ""
    data["challenge"] = challenge
    data["user_id"] = account
    data["validate"] = validate
    if validate:
        data["seccode"] = validate + "|jordan"
    data["pwd"] = rsacr.rsacreate(rsa['hash'] + password, public_key)
    return await sendpost(bililogin + "api/client/login", setsign(data))


async def login(bili_account, bili_pwd, admin_id=None):
    logger.info(f'logging in with acc={bili_account}')
    login_sta = await _login(bili_account, bili_pwd)
    if login_sta.get("message", "") == "用户名或密码错误":
        raise Exception("用户名或密码错误")

    if login_sta['code'] == 200000:  # secondary verify
        cap = await sendpost(bililogin + "api/client/start_captcha", setsign(json.loads(modolcaptch)))
        try:
            challenge, gt_user_id, captch_done = await captchaVerifier(
                cap['gt'], cap['challenge'], cap['gt_user_id'])
        except Exception as e:
            # 只有真的配了手动兜底站点才会去尝试手动过码；留空时下一行就直接抛错放弃，
            # 所以这里按配置分支把话说准，别再写“尝试手动过码”误导排查。
            if manual_captch_site:
                logger.error(
                    f"自动过码失败: {e}，转手动过码（站点 {manual_captch_site}）"
                )
            else:
                logger.error(
                    f"自动过码失败: {e}，未配置手动兜底站点（captcha_site），本次登录将失败"
                )
            if admin_id is None:
                raise Exception("自动过码失败，且未提供管理员ID，无法手动过码")
            try:
                challenge, gt_user_id, captch_done = await manual_captch(
                    cap['challenge'], cap['gt'], cap['gt_user_id'], admin_id, bili_account)
            except Exception as e:
                raise Exception(f'手动过码失败: {e}')

        login_sta = await _login(bili_account, bili_pwd, challenge, gt_user_id, captch_done)
        if login_sta.get("message", "") == "用户名或密码错误":
            raise Exception("用户名或密码错误")
        return login_sta
    else:
        return login_sta


class bsdkclient:

    def __init__(self, account: str, password: str, platform: int, admin_id=None):
        self.account = account
        self.password = password
        self.qudao = platform
        self.admin_id = admin_id          # Matrix 管理员 user_id，如 "@wscx456:alfinreisearnor.uk"
        if self.qudao == 0:
            self.platform = "2"
        else:
            self.platform = "4"

    async def b_login(self):
        if self.qudao == 0:
            last_err = None
            for i in range(3):
                try:
                    resp = await login(self.account, self.password, self.admin_id)
                except Exception as e:
                    last_err = e
                    logger.warning(f"[pcrjjc] 登录尝试 {i + 1} 失败: {e}")
                    await asyncio.sleep(2)
                    continue
                if resp.get('code') == 0:
                    logger.info("geetest or captcha succeed")
                    return resp['uid'], resp['access_key']
                last_err = Exception(f"登录返回异常: {resp}")
            raise Exception(f"登录失败: {last_err}")
        elif self.qudao == 1:
            return self.account, self.password


async def manual_captch_listener(user_id: str):
    while True:
        async with httpx.AsyncClient() as client:
            url = f'{manual_captch_site}/api/block?userid={user_id}'
            try:
                response = await client.get(url, timeout=28)
            except httpx.TimeoutException:
                pass
            else:
                if response.status_code == 200:
                    res = response.json()
                    return res["validate"]


async def manual_captch(challenge: str, gt: str, user_id: str, target_id, bili_account):
    if not manual_captch_site:
        # 自动过码（默认 https://pcrd.tencentbot.top）已经失败过一次才会走到这里，
        # 所以这条提示要把“默认自动过码是谁”一起说清楚，免得看起来像完全没配过码。
        raise RuntimeError(
            f"自动过码（当前接口 {auto_captcha_api or '未配置'}）未成功，"
            "且未配置手动过码站点（WebUI 的 captcha_site），无法继续验证；"
            "如需手动兜底，请按 README 自建过码站点后填入 captcha_site"
        )
    url = f"{manual_captch_site}/?captcha_type=1&challenge={challenge}&gt={gt}&userid={user_id}&gs=1"
    await private_send(target_id, f'pcr账号{bili_account}登录触发验证码，请在{gt_wait}秒内完成以下链接中的验证内容。')
    await private_send(target_id, url)

    try:
        return (challenge, user_id, await asyncio.wait_for(manual_captch_listener(user_id), gt_wait))
    except asyncio.TimeoutError:
        await private_send(target_id, "手动过码获取结果超时")
        raise RuntimeError("手动过码获取结果超时")
    except Exception as e:
        await private_send(target_id, f'手动过码获取结果异常：{e}')
        raise e
