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

TOKEN="${1:-${METRO_TOKEN:-}}"
[ -z "$TOKEN" ] && [ -f ../data/token.txt ] && TOKEN="$(cat ../data/token.txt)"
[ -z "$TOKEN" ] && { echo "Falta token (arg1, METRO_TOKEN o ../data/token.txt)"; exit 1; }
BASE_LAN="${BASE_LAN:-http://192.168.1.100/metroviolencia/}"     # dins de la LAN
HOST_EXT="${HOST_EXT:-home.pr1.es}"
BASE_EXT="${BASE_EXT:-https://home.pr1.es:8751/metroviolencia/}"  # fora de la LAN
URL_LAN="${BASE_LAN}?token=${TOKEN}"
URL_EXT="${BASE_EXT}?token=${TOKEN}"

OUT=build
rm -rf "$OUT"; mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/src/com/mdk/metroviolencia"

echo "[0/8] MainActivity.java (token + LAN/externa embebidos)"
sed -e "s|__URL_LAN__|${URL_LAN}|g" -e "s|__URL_EXT__|${URL_EXT}|g" \
    -e "s|__HOST_EXT__|${HOST_EXT}|g" MainActivity.java.tmpl \
    > "$OUT/src/com/mdk/metroviolencia/MainActivity.java"

echo "[1/8] aapt2 compile recursos"
"$BT/aapt2.exe" compile --dir res -o "$OUT/res.zip"

echo "[2/8] aapt2 link"
"$BT/aapt2.exe" link -o "$OUT/base.apk" -I "$ANDJAR" \
  --manifest AndroidManifest.xml \
  --java "$OUT/gen" --min-sdk-version 21 --target-sdk-version 34 \
  "$OUT/res.zip"

echo "[3/8] javac"
"$JDK/bin/javac.exe" -source 8 -target 8 -bootclasspath "$ANDJAR" -classpath "$ANDJAR" \
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
  --key-pass pass:metroviolencia --out "$OUT/metroviolencia.apk" "$OUT/aligned.apk"

echo "OK -> $HERE/$OUT/metroviolencia.apk"
