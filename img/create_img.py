import json
from PIL import Image, ImageDraw, ImageFont
import threading
import time
from pathlib import Path
import zhconv
import asyncio
import re
from ..var import Platform

path = Path(__file__).parent  # 获取 img 目录的绝对路径
font_cn_path = str(path / 'fonts' / 'SourceHanSansCN-Medium.otf')


# 头像框配置文件的唯一路径：必须与 main.py 写入处（StarTools.get_data_dir）保持一致。
# 原实现按「相对 CWD 的 data/plugin_data → 插件目录 → img 目录」三级回退查找，
# AstrBot 的工作目录不是安装根目录时（容器/systemd 启动）会读到插件目录里那份
# 过期文件，导致「更换头像框」写进配置目录的内容对出图完全不可见（P1-7）。
def _get_frame_json_path() -> Path:
    try:
        from astrbot.api.star import StarTools
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_pcrjjc"))
    except Exception:
        # 与 main.py / database.dal 中的同名回退保持一致
        data_dir = Path("data") / "plugin_data" / "astrbot_plugin_pcrjjc"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # 只读容器下建目录失败，下面按文件是否存在处理
    return data_dir / 'frame.json'


# 替代 hoshino.util.filt_message
def _filt_message(text: str) -> str:
    text = re.sub(r"\[CQ:[^\]]+\]", "", str(text))
    return text.replace("\r", "").replace("\n", " ").strip()


# 替代 hoshino.modules.priconne.chara 的名字获取
def get_chara_name(chara_id):
    return f"角色{chara_id}"


# 替代 hoshino.modules.priconne.chara 的头像获取
def get_chara_icon(chara_id):
    p = path / 'img' / 'icon' / f'{chara_id}.png'
    if p.exists():
        return str(p)
    return None


# 兼容新版 Pillow 的文字尺寸获取方法
def get_text_size(font, text):
    bbox = font.getbbox(str(text))
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def get_server(platform: int) -> str:
    if platform == Platform.b_id.value:
        return 'bilibili官方服务器'
    elif platform == Platform.qu_id.value:
        return '渠道服第三方服务器'
    return '未知服务器'


# ============ 渲染缓存（性能）============
# 出图跑在 asyncio.to_thread 的线程池里，这里按线程各缓存一份：
#   · 模板：省掉每张图一次 template.png 解码（实测 603×1097 约 19.6ms）；
#   · 字体：省掉按尺寸重复创建（同时保留 FreeType 字形缓存）；
#   · 头像框：省掉每张图一次解码 + 缩放。
# 不跨线程共享 Image / FreeType 字体实例，避免并发渲染时的竞态。
_tls = threading.local()


def _thread_cache() -> dict:
    cache = getattr(_tls, "cache", None)
    if cache is None:
        cache = _tls.cache = {
            "template": None,
            "fonts": {},
            "frames": {},
            "frame_conf": (None, None),
        }
    return cache


def _get_template() -> Image.Image:
    """返回模板副本（副本可随意绘制，缓存的原图不被修改）。"""
    cache = _thread_cache()
    if cache["template"] is None:
        with Image.open(path / 'img' / 'template.png') as f:
            cache["template"] = f.convert("RGBA")
    return cache["template"].copy()


def _get_font(size: int):
    fonts = _thread_cache()["fonts"]
    font = fonts.get(size)
    if font is None:
        font = ImageFont.truetype(font_cn_path, size)
        fonts[size] = font
    return font


def _get_frame_image(frame_tmp: str) -> Image.Image:
    """返回 100×100 的头像框（缓存的是缩放后的成品，直接贴即可）。"""
    frames = _thread_cache()["frames"]
    frame_img = frames.get(frame_tmp)
    if frame_img is None:
        with Image.open(path / 'img' / 'frame' / f'{frame_tmp}') as f:
            frame_img = f.convert("RGBA").resize((100, 100))
        frames[frame_tmp] = frame_img
    return frame_img


def _load_frame_config():
    """读取头像框配置，按 mtime 缓存。

    「更换头像框」写文件后 mtime 会变，下一次出图自然读到新值；不变时
    省掉每张图一次读盘 + json 解析。
    """
    conf_path = _get_frame_json_path()
    try:
        stat = conf_path.stat()
    except OSError:
        return None
    cache = _thread_cache()
    stamp = (str(conf_path), stat.st_mtime_ns, stat.st_size)
    if cache["frame_conf"][0] == stamp:
        return cache["frame_conf"][1]
    try:
        with open(conf_path, 'r', encoding='UTF-8') as f:
            data = json.load(f)
    except Exception:
        data = None
    cache["frame_conf"] = (stamp, data)
    return data


def get_frame(user_id):
    f_data = _load_frame_config()
    if not f_data:
        # main.py 在插件加载时才会建好这份文件；取不到时退回默认框，不再读旧位置
        return 'color.png'
    customize = f_data.get('customize') or {}
    return str(customize.get(str(user_id)) or f_data.get('default_frame') or 'color.png')



def _TraditionalToSimplified(hant_str: str):
    return zhconv.convert(str(hant_str), 'zh-hans')


def _cut_str(obj: str, sec: int):
    return [obj[i: i + sec] for i in range(0, len(obj), sec)]
def _generate_info_pic_internal(data, uid, platform, frame_user_id=None):
    # 头像框按「聊天平台用户 ID」配置（main.py 的更换头像框用 sender_id 写库），
    # 而这里的 uid 是 13 位游戏 UID，两者键空间不同；必须由调用方显式传入
    # frame_user_id，否则永远命中不了 customize（P1-6）。
    frame_tmp = get_frame(uid if frame_user_id is None else frame_user_id)
    im = _get_template()  # 图片模板（线程内缓存的副本）
    im_frame = _get_frame_image(frame_tmp)  # 头像框（已缩放为 100×100）
    try:
        id_favorite = int(str(data['favorite_unit']['id'])[0:4])
    except:
        id_favorite = 1000

    pic_dir = get_chara_icon(id_favorite)
    if pic_dir:
        user_avatar = Image.open(pic_dir).convert("RGBA")
        user_avatar = user_avatar.resize((90, 90))
        im.paste(user_avatar, (44, 150), mask=user_avatar)

    im.paste(im=im_frame, box=(39, 145), mask=im_frame)

    cn_font = _get_font(18)
    font = cn_font
    cn_font_resize = _get_font(16)
    font_resize = cn_font_resize

    draw = ImageDraw.Draw(im)
    font_black = (77, 76, 81, 255)

    user_name_text = _TraditionalToSimplified(data["user_info"]["user_name"])
    user_name_text = _filt_message(str(user_name_text))
    team_level_text = _TraditionalToSimplified(data["user_info"]["team_level"])
    team_level_text = _filt_message(str(team_level_text))
    total_power_text = _TraditionalToSimplified(data["user_info"]["total_power"])
    total_power_text = _filt_message(str(total_power_text))
    clan_name_text = _TraditionalToSimplified(data["clan_name"])
    clan_name_text = _filt_message(str(clan_name_text))
    user_comment_arr = _TraditionalToSimplified(data["user_info"]["user_comment"])
    user_comment_arr = _filt_message(str(user_comment_arr))
    user_comment_arr = _cut_str(user_comment_arr, 25)
    last_login_time_text = _TraditionalToSimplified(time.strftime(
        "%Y/%m/%d %H:%M:%S", time.localtime(data["user_info"]["last_login_time"]))).split(' ')

    draw.text((194, 120), user_name_text, font_black, font)

    w, h = get_text_size(font_resize, team_level_text)
    draw.text((568 - w, 168), team_level_text, font_black, font_resize)
    w, h = get_text_size(font_resize, total_power_text)
    draw.text((568 - w, 210), total_power_text, font_black, font_resize)
    w, h = get_text_size(font_resize, clan_name_text)
    draw.text((568 - w, 250), clan_name_text, font_black, font_resize)
    for index, value in enumerate(user_comment_arr):
        draw.text((170, 310 + (index * 22)), value, font_black, font_resize)
    draw.text((34, 350), last_login_time_text[0] + "\n" + last_login_time_text[1], font_black, font_resize)
    draw.text((34, 392), get_server(platform), font_black, font_resize)

    normal_quest_text = _TraditionalToSimplified(data["quest_info"]["normal_quest"][2])
    hard_quest_text = _TraditionalToSimplified(data["quest_info"]["hard_quest"][2])
    very_hard_quest_text = _TraditionalToSimplified(data["quest_info"]["very_hard_quest"][2])

    w, h = get_text_size(font_resize, normal_quest_text)
    draw.text((550 - w, 498), normal_quest_text, font_black, font_resize)
    w, h = get_text_size(font_resize, "H" + hard_quest_text + " / VH" + very_hard_quest_text)
    draw.text((550 - w, 530), "H" + hard_quest_text + " / VH", font_black, font_resize)
    w, h = get_text_size(font_resize, very_hard_quest_text)
    draw.text((550 - w, 530), very_hard_quest_text, font_black, font_resize)

    arena_group_text = _TraditionalToSimplified(data["user_info"]["arena_group"])
    arena_time_text = _TraditionalToSimplified(time.strftime("%Y/%m/%d", time.localtime(data["user_info"]["arena_time"])))
    arena_rank_text = _TraditionalToSimplified(data["user_info"]["arena_rank"])
    grand_arena_group_text = _TraditionalToSimplified(data["user_info"]["grand_arena_group"])
    grand_arena_time_text = _TraditionalToSimplified(time.strftime("%Y/%m/%d", time.localtime(data["user_info"]["grand_arena_time"])))
    grand_arena_rank_text = _TraditionalToSimplified(data["user_info"]["grand_arena_rank"])

    w, h = get_text_size(font_resize, arena_time_text)
    draw.text((550 - w, 598), arena_time_text, font_black, font_resize)
    w, h = get_text_size(font_resize, arena_group_text + "场")
    draw.text((550 - w, 630), arena_group_text + "场", font_black, font_resize)
    w, h = get_text_size(font_resize, arena_rank_text + "名")
    draw.text((550 - w, 662), arena_rank_text + "名", font_black, font_resize)
    w, h = get_text_size(font_resize, grand_arena_time_text)
    draw.text((550 - w, 704), grand_arena_time_text, font_black, font_resize)
    w, h = get_text_size(font_resize, grand_arena_group_text + "场")
    draw.text((550 - w, 738), grand_arena_group_text + "场", font_black, font_resize)
    w, h = get_text_size(font_resize, grand_arena_rank_text + "名")
    draw.text((550 - w, 772), grand_arena_rank_text + "名", font_black, font_resize)

    unit_num_text = _TraditionalToSimplified(data["user_info"]["unit_num"])
    open_story_num_text = _TraditionalToSimplified(data["user_info"]["open_story_num"])
    w, h = get_text_size(font_resize, unit_num_text)
    draw.text((550 - w, 844), unit_num_text, font_black, font_resize)
    w, h = get_text_size(font_resize, open_story_num_text)
    draw.text((550 - w, 880), open_story_num_text, font_black, font_resize)

    tower_cleared_floor_num_text = _TraditionalToSimplified(data["user_info"]["tower_cleared_floor_num"])
    tower_cleared_ex_quest_count_text = _TraditionalToSimplified(data["user_info"]["tower_cleared_ex_quest_count"])
    w, h = get_text_size(font_resize, tower_cleared_floor_num_text + "阶")
    draw.text((550 - w, 949), tower_cleared_floor_num_text + "阶", font_black, font_resize)
    w, h = get_text_size(font_resize, tower_cleared_ex_quest_count_text)
    draw.text((550 - w, 984), tower_cleared_ex_quest_count_text, font_black, font_resize)

    # 完整显示 UID（原实现写死只取前 3 段，13 位 UID 会被截成 9 位；位数不足时还会 IndexError）
    viewer_id_text = "  ".join(_cut_str(_TraditionalToSimplified(data["user_info"]["viewer_id"]), 3))
    w, h = get_text_size(font, viewer_id_text)
    draw.text((138 + (460 - 138) / 2 - w / 2, 1058), viewer_id_text, (255, 255, 255, 255), font)

    return im


async def generate_info_pic(*args, **kwargs):
    return await asyncio.to_thread(_generate_info_pic_internal, *args, **kwargs)
