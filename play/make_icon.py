"""
Icono de MetroViu (variante Google Play): trazado de línea con estaciones y un
marcador de posición. Diseño propio y neutro (sin personajes ni logos de terceros).
Salida: play/res/mipmap-*/ic_launcher(_round).png y play/store/icon-512.png
Uso: python play/make_icon.py
"""
import os
from PIL import Image, ImageDraw

NAVY = (18, 38, 66, 255)
LINE1 = (229, 57, 53, 255)     # rojo
LINE2 = (255, 179, 0, 255)     # ámbar
WHITE = (255, 255, 255, 255)
PIN = (38, 198, 218, 255)      # cian


def draw(s=1024, round_=False, square=False):
    k = s / 512.0
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def R(*v):
        return [x * k for x in v]

    if square:      # icono de la ficha de Play: cuadrado completo, Play aplica la máscara
        d.rectangle(R(0, 0, 512, 512), fill=NAVY)
    elif round_:
        d.ellipse(R(0, 0, 512, 512), fill=NAVY)
    else:
        d.rounded_rectangle(R(0, 0, 512, 512), radius=104 * k, fill=NAVY)

    w = int(34 * k)
    # línea 1: diagonal con codo
    p1 = [R(92, 390), R(200, 282), R(330, 282), R(420, 192)]
    d.line([tuple(p) for p in p1], fill=LINE1, width=w, joint="curve")
    # línea 2: vertical que cruza
    p2 = [R(200, 110), R(200, 282), R(200, 420)]
    d.line([tuple(p) for p in p2], fill=LINE2, width=w, joint="curve")
    for cx, cy in [(92, 390), (420, 192), (200, 110), (200, 420)]:
        d.ellipse(R(cx - 26, cy - 26, cx + 26, cy + 26), fill=WHITE)
    # intercambiador
    d.ellipse(R(200 - 40, 282 - 40, 200 + 40, 282 + 40), fill=WHITE, outline=NAVY, width=int(10 * k))
    # marcador de posición sobre la línea 1
    cx, cy = 330, 282
    d.ellipse(R(cx - 46, cy - 150, cx + 46, cy - 58), fill=PIN)
    d.polygon([tuple(R(cx - 40, cy - 92)), tuple(R(cx + 40, cy - 92)), tuple(R(cx, cy - 22))], fill=PIN)
    d.ellipse(R(cx - 18, cy - 122, cx + 18, cy - 86), fill=NAVY)
    d.ellipse(R(cx - 16, cy - 16, cx + 16, cy + 16), fill=WHITE)
    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    big = draw(1024)
    big_r = draw(1024, round_=True)
    os.makedirs(os.path.join(here, "store"), exist_ok=True)
    draw(1024, square=True).resize((512, 512), Image.LANCZOS).convert("RGB").save(os.path.join(here, "store", "icon-512.png"))
    dens = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    for name, px in dens.items():
        out = os.path.join(here, "res", f"mipmap-{name}")
        os.makedirs(out, exist_ok=True)
        big.resize((px, px), Image.LANCZOS).save(os.path.join(out, "ic_launcher.png"))
        big_r.resize((px, px), Image.LANCZOS).save(os.path.join(out, "ic_launcher_round.png"))
    print("ok")


if __name__ == "__main__":
    main()
