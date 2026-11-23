"""Логотип бота «АСКУЭ + молния»: python make_logo.py <выход.png> [шрифт.ttf]
Квадрат 640×640; всё важное — внутри круга (Telegram обрезает аватар кругом)."""
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = sys.argv[1]
FONT = sys.argv[2] if len(sys.argv) > 2 else "DejaVuSans-Bold.ttf"
S = 2560                     # рисуем в 4 раза крупнее и уменьшаем — ровные края
W = 640


def P(x, y):
    """Координаты молнии в долях (нарисованы для центра 0.395) -> пиксели с масштабом K вокруг центра RC."""
    return ((0.5 + (x - 0.51) * K) * S, (RC + (y - 0.395) * K) * S)


# фон: радиальный градиент тёмно-синий
bg = Image.new("RGB", (S, S))
cx, cy = 0.5 * S, 0.42 * S
RC, RR, K = 0.36, 0.25, 0.857   # центр и радиус кольца, масштаб молнии
inner, outer = (28, 84, 168), (6, 22, 52)
small = Image.new("RGB", (256, 256))
px = small.load()
for y in range(256):
    for x in range(256):
        d = min(1.0, ((x - 128) ** 2 + (y - 108) ** 2) ** 0.5 / 190)
        px[x, y] = tuple(int(inner[i] + (outer[i] - inner[i]) * d) for i in range(3))
bg = small.resize((S, S), Image.BICUBIC).convert("RGBA")

# кольцо «шкалы» за молнией
ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
rd = ImageDraw.Draw(ring)
r = RR * S
rd.ellipse([cx - r, RC * S - r, cx + r, RC * S + r], outline=(110, 170, 255, 110), width=int(0.012 * S))
# риски шкалы по верхней дуге
import math
for k in range(-5, 6):
    a = math.radians(-90 + k * 15)
    r1, r2 = r * 1.06, r * (1.14 if k % 5 == 0 else 1.10)
    x1, y1 = cx + r1 * math.cos(a), RC * S + r1 * math.sin(a)
    x2, y2 = cx + r2 * math.cos(a), RC * S + r2 * math.sin(a)
    rd.line([x1, y1, x2, y2], fill=(110, 170, 255, 150), width=int(0.008 * S))
bg = Image.alpha_composite(bg, ring)

# молния
bolt = [P(0.600, 0.115), P(0.355, 0.425), P(0.490, 0.425), P(0.400, 0.675),
        P(0.665, 0.330), P(0.528, 0.330), P(0.600, 0.115)]
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).polygon(bolt, fill=255)
# свечение
glow = Image.new("RGBA", (S, S), (255, 200, 40, 0))
glow.putalpha(mask.filter(ImageFilter.GaussianBlur(0.035 * S)).point(lambda v: int(v * 0.85)))
bg = Image.alpha_composite(bg, glow)
# заливка молнии вертикальным градиентом жёлтый → оранжевый
grad = Image.new("RGBA", (1, 256))
for y in range(256):
    t = y / 255
    grad.putpixel((0, y), (255, int(232 - 80 * t), int(90 - 70 * t), 255))
grad = grad.resize((S, S))
bg.paste(grad, (0, 0), mask)
# тонкий светлый контур
edge = ImageDraw.Draw(bg)
edge.line(bolt, fill=(255, 245, 200, 255), width=int(0.006 * S), joint="curve")

# надпись АСКУЭ
d = ImageDraw.Draw(bg)
text = "АСКУЭ"
size = int(0.20 * S)
font = ImageFont.truetype(FONT, size)
while d.textlength(text, font=font) > 0.56 * S:
    size -= 10
    font = ImageFont.truetype(FONT, size)
tw = d.textlength(text, font=font)
ty = 0.655 * S
shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shadow).text(((S - tw) / 2, ty + 0.01 * S), text, font=font, fill=(0, 0, 0, 160), anchor="la")
bg = Image.alpha_composite(bg, shadow.filter(ImageFilter.GaussianBlur(0.012 * S)))
ImageDraw.Draw(bg).text(((S - tw) / 2, ty), text, font=font, fill=(255, 255, 255, 255), anchor="la")

bg.convert("RGB").resize((W, W), Image.LANCZOS).save(OUT, optimize=True)

# предпросмотр «как в Telegram» — кругом
prev = Image.open(OUT).convert("RGBA")
m = Image.new("L", (W, W), 0)
ImageDraw.Draw(m).ellipse([0, 0, W - 1, W - 1], fill=255)
out = Image.new("RGBA", (W, W), (240, 242, 245, 255))
out.paste(prev, (0, 0), m)
out.save(OUT.replace(".png", "_circle.png"))
print("ok", OUT)
