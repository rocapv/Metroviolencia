#!/usr/bin/env bash
# Construye la APK WebView de Metroviolencia con el token embebido.
# Uso: ./build_apk.sh [TOKEN]   (si no, usa $METRO_TOKEN o ../data/token.txt)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

JDK="${TOOLS:-$HERE/../../.tools}/jdk"
export JAVA_HOME="$JDK"
export PATH="$JDK/bin:$PATH"
SDK="${TOOLS:-$HERE/../../.tools}/android-sdk"
ANDJAR="$SDK/platforms/android-34/android.jar"
BT="$SDK/build-tools/34.0.0"
PY="$HERE/../.venv/Scripts/python.exe"

# Variante: por defecto PERSONAL (token embebido). Con --dist o DIST=1 => la app
# PIDE el token (para enviar a otros teléfonos).
DIST=0
if [ "$1" = "--dist" ]; then DIST=1; shift; fi
[ "${DIST:-0}" = "1" ] && DIST=1

if [ "$DIST" = "1" ]; then
  TOKEN=""                       # sin token: la app lo pedirá
  OUTNAME="metroviolencia-dist.apk"
else
  TOKEN="${1:-${METRO_TOKEN:-}}"
  [ -z "$TOKEN" ] && [ -f ../data/token.txt ] && TOKEN="$(cat ../data/token.txt)"
  [ -z "$TOKEN" ] && { echo "Falta token (arg1, METRO_TOKEN o ../data/token.txt)"; exit 1; }
  OUTNAME="metroviolencia.apk"
fi
BASE_LAN="${BASE_LAN:-http://192.168.1.100/metroviolencia/}"                  # dins de la LAN
BASE_EXT_HTTPS="${BASE_EXT_HTTPS:-https://home.pr1.es:8751/metroviolencia/}"  # extern HTTPS (port dedicat)
BASE_EXT_HTTP="${BASE_EXT_HTTP:-http://home.pr1.es:5580/metroviolencia/}"     # extern HTTP (port ja reenviat)

OUT=build
rm -rf "$OUT"; mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/src/com/mdk/metroviolencia"

echo "[0/8] MainActivity.java (variante $([ "$DIST" = 1 ] && echo distribución || echo personal))"
sed -e "s|__TOKEN__|${TOKEN}|g" -e "s|__BASE_LAN__|${BASE_LAN}|g" \
    -e "s|__BASE_HTTPS__|${BASE_EXT_HTTPS}|g" -e "s|__BASE_HTTP__|${BASE_EXT_HTTP}|g" \
    MainActivity.java.tmpl > "$OUT/src/com/mdk/metroviolencia/MainActivity.java"

echo "[1/8] aapt2 compile recursos"
"$BT/aapt2.exe" compile --dir res -o "$OUT/res.zip"

echo "[2/8] aapt2 link"
"$BT/aapt2.exe" link -o "$OUT/base.apk" -I "$ANDJAR" \
  --manifest AndroidManifest.xml \
  --java "$OUT/gen" --min-sdk-version 21 --target-sdk-version 34 \
  "$OUT/res.zip"

echo "[3/8] javac"
"$JDK/bin/javac.exe" -source 8 -target 8 -encoding UTF-8 -bootclasspath "$ANDJAR" -classpath "$ANDJAR" \
  -d "$OUT/classes" \
  "$OUT/src/com/mdk/metroviolencia/MainActivity.java" \
  "$OUT/gen/com/mdk/metroviolencia/R.java"

echo "[4/8] d8 -> classes.dex"
"$BT/d8.bat" --min-api 21 --lib "$ANDJAR" --output "$OUT" $(find "$OUT/classes" -name '*.class')

echo "[5/8] insertar classes.dex en el apk"
cp "$OUT/base.apk" "$OUT/unsigned.apk"
"$PY" -c "import zipfile; z=zipfile.ZipFile(r'$OUT/unsigned.apk','a',zipfile.ZIP_DEFLATED); z.write(r'$OUT/classes.dex','classes.dex'); z.close()"

echo "[6/8] zipalign"
"$BT/zipalign.exe" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

echo "[7/8] keystore"
if [ ! -f debug.keystore ]; then
  "$JDK/bin/keytool.exe" -genkeypair -keystore debug.keystore -alias mdk \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass metroviolencia -keypass metroviolencia \
    -dname "CN=Metroviolencia, O=MDK"
fi

echo "[8/8] apksigner"
"$BT/apksigner.bat" sign --ks debug.keystore --ks-pass pass:metroviolencia \
  --key-pass pass:metroviolencia --out "$OUT/${OUTNAME}" "$OUT/aligned.apk"

echo "OK -> $HERE/$OUT/${OUTNAME}"
