from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import io
import base64
import threading

LINE_CHAR_COUNT = 30*2  # 每行字符数：30个中文字符(=60英文字符)
CHAR_SIZE = 32
CHAR_SIZE_h = 47
TABLE_WIDTH = 4

_tls = threading.local()


def _get_font(size: int):
    """按线程缓存字体对象。

    · 复用同一个 FreeType 字体实例可以保留字形缓存（实测省约 14% 绘制时间）；
    · 按线程各存一份而不是全局共享：FreeType 的 face 不是线程安全的，
      出图现在跑在 asyncio.to_thread 的线程池里，共享实例有竞态风险。
    """
    fonts = getattr(_tls, "fonts", None)
    if fonts is None:
        fonts = _tls.fonts = {}
    font = fonts.get(size)
    if font is None:
        font = ImageFont.truetype(
            str(Path(__file__).parent / 'fonts' / 'SourceHanSansCN-Medium.otf'), size)
        fonts[size] = font
    return font


def line_break(line):
    """折行并返回 (折行后的文本, 实际最大行宽)。

    原实现把最大行宽写进模块全局 LINE_CHAR_COUNT_MAX，image_draw 再读回来。
    这样一来出图只能串行跑在事件循环里：一旦丢进 asyncio.to_thread，
    多个渲染协程会互相覆盖这个全局值（画布宽度算错）。改成返回值后
    line_break / image_draw 都是纯函数，才可以安全地放线程池。
    """
    line_char_count_max = 0
    ret = ''
    width = 0
    for c in line:
        if len(c.encode('utf8')) == 3:  # 中文
            if LINE_CHAR_COUNT == width + 1:  # 剩余位置不够一个汉字
                width = 2
                ret += '\n' + c
            else:  # 中文宽度加2，注意换行边界
                width += 2
                ret += c
        else:
            if c == '\t':
                space_c = TABLE_WIDTH - width % TABLE_WIDTH  # 已有长度对TABLE_WIDTH取余
                ret += ' ' * space_c
                width += space_c
            elif c == '\n':
                width = 0
                ret += c
            else:
                width += 1
                ret += c
        if width >= LINE_CHAR_COUNT:
            ret += '\n'
            width = 0
            line_char_count_max = LINE_CHAR_COUNT
        if width > line_char_count_max:
            line_char_count_max = width

    if ret.endswith('\n'):
        return ret, line_char_count_max
    return ret + '\n', line_char_count_max


def image_draw(msg):
    output_str, line_char_count_max = line_break(msg)
    d_font = _get_font(CHAR_SIZE)
    lines = output_str.count('\n')  # 计算行数
    image = Image.new(mode="RGB", size=(line_char_count_max*CHAR_SIZE //
                                        2+84, CHAR_SIZE_h*lines+84), color=(255, 252, 245))
    draw_table = ImageDraw.Draw(im=image)
    draw_table.text(xy=(42, 42), text=output_str, fill=(
        125, 101, 89), font=d_font, spacing=CHAR_SIZE//2)  # spacing调节机制不清楚如何计算
    draw_table.rectangle(xy=(16, 16, line_char_count_max*CHAR_SIZE // 2+69,
                         CHAR_SIZE_h*lines+69), fill=None, outline=(220, 211, 196), width=2)
    b_io = io.BytesIO()
    image.save(b_io, format="JPEG")
    base64_str = 'base64://' + base64.b64encode(b_io.getvalue()).decode()
    return base64_str
