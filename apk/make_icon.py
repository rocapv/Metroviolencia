"""
Genera el icono de la APK: una locomotora azul con carita sonriente (estilo
'Thomas'), dibujada de forma original con Pillow (sin usar la imagen protegida).
Salida: apk/res/mipmap-*/ic_launcher.png en las densidades Android.
"""
import os
from PIL import Image, ImageDraw

S = 512


def draw(s=S):
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = s / 512.0

    def R(*v):
        return [x * k for x in v]

    blue = (26, 122, 201, 255)
    dark = (32, 33, 38, 255)
    grey = (214, 218, 223, 255)
    edge = (150, 156, 164, 255)
    red = (200, 46, 46, 255)

    # fons quadrat arrodonit
    d.rounded_rectangle(R(0, 0, 512, 512), radius=96 * k, fill=blue)
    # xemeneia (funnel)
    d.polygon([tuple(R(224, 58)[0:2]), tuple(R(288, 58)[0:2]),
               tuple(R(302, 120)[0:2]), tuple(R(210, 120)[0:2])], fill=dark)
    d.rounded_rectangle(R(228, 104, 284, 150), radius=8 * k, fill=dark)
    # vapor
    for cx, cy, r in [(256, 40, 24), (226, 28, 16), (288, 30, 18)]:
        d.ellipse(R(cx - r, cy - r, cx + r, cy + r), fill=(240, 244, 248, 255))
    # cara (placa frontal)
    d.ellipse(R(120, 150, 392, 432), fill=grey, outline=edge, width=int(7 * k))
    # ulls
    for ex in (198, 314):
        d.ellipse(R(ex - 42, 222, ex + 42, 304), fill=(255, 255, 255, 255),
                  outline=(96, 102, 110, 255), width=int(4 * k))
        d.ellipse(R(ex - 17, 250, ex + 17, 294), fill=dark)
    # celles
    d.arc(R(168, 196, 240, 252), start=200, end=345, fill=dark, width=int(13 * k))
    d.arc(R(272, 196, 344, 252), start=195, end=340, fill=dark, width=int(13 * k))
    # somriure (boca oberta)
    d.pieslice(R(198, 296, 314, 404), start=18, end=162, fill=(70, 44, 48, 255))
    d.chord(R(198, 296, 314, 356), start=18, end=162, fill=grey)
    # travesser roig inferior amb el número 1
    d.rounded_rectangle(R(150, 402, 362, 446), radius=16 * k, fill=red)
    d.text((256 * k, 424 * k), "1", fill=(255, 255, 255, 255), anchor="mm")
    return img


def main():
    base = draw(512)
    here = os.path.dirname(os.path.abspath(__file__))
    base.save(os.path.join(here, "icon_preview.png"))
    dens = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    for name, px in dens.items():
        outdir = os.path.join(here, "res", f"mipmap-{name}")
        os.makedirs(outdir, exist_ok=True)
        ic = base.resize((px, px), Image.LANCZOS)
        ic.save(os.path.join(outdir, "ic_launcher.png"))
        ic.save(os.path.join(outdir, "ic_launcher_round.png"))
    print("iconos generados. preview:", os.path.join(here, "icon_preview.png"))


if __name__ == "__main__":
    main()
