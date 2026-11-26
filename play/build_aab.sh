#!/usr/bin/env bash
# MetroViu · genera el Android App Bundle firmado para Google Play (es.metroviu.app).
# Sin Gradle: aapt2 (formato proto) + javac + d8 + bundletool + jarsigner.
#
# Variables:
#   ANDROID_SDK   SDK con platforms;android-36 y build-tools;36.0.0
#   BUNDLETOOL    ruta a bundletool-all-*.jar
#   KEYSTORE      upload key (.jks) — NUNCA dentro del repo
#   KS_PASS_FILE  fichero con la contraseña del keystore (y de la clave)
#   KEY_ALIAS     alias de la clave (por defecto: upload)
#   VERSION_CODE / VERSION_NAME  opcionales, sobrescriben los del manifest
#
# Uso: ANDROID_SDK=... BUNDLETOOL=... KEYSTORE=... KS_PASS_FILE=... ./build_aab.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

: "${ANDROID_SDK:?falta ANDROID_SDK}"
: "${BUNDLETOOL:?falta BUNDLETOOL}"
: "${KEYSTORE:?falta KEYSTORE}"
: "${KS_PASS_FILE:?falta KS_PASS_FILE}"
KEY_ALIAS="${KEY_ALIAS:-upload}"
API=36
BT="$ANDROID_SDK/build-tools/36.0.0"
ANDJAR="$ANDROID_SDK/platforms/android-$API/android.jar"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) EXE=.exe; BAT=.bat ;; *) EXE=; BAT= ;; esac

case "$KEYSTORE" in "$HERE"/*|"$(dirname "$HERE")"/*) echo "El keystore no puede estar dentro del repo"; exit 1;; esac

OUT=build
rm -rf "$OUT"; mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex" "$OUT/module"

VERARGS=()
[ -n "${VERSION_CODE:-}" ] && VERARGS+=(--version-code "$VERSION_CODE")
[ -n "${VERSION_NAME:-}" ] && VERARGS+=(--version-name "$VERSION_NAME")

echo "[1/7] aapt2 compile"
"$BT/aapt2$EXE" compile --dir res -o "$OUT/res.zip"

echo "[2/7] aapt2 link (proto)"
"$BT/aapt2$EXE" link --proto-format -o "$OUT/base.apk" -I "$ANDJAR" \
  --manifest AndroidManifest.xml --java "$OUT/gen" "${VERARGS[@]}" "$OUT/res.zip"

echo "[3/7] javac"
javac -source 8 -target 8 -Xlint:-options -encoding UTF-8 \
  -bootclasspath "$ANDJAR" -classpath "$ANDJAR" -d "$OUT/classes" \
  MainActivity.java "$OUT/gen/es/metroviu/app/R.java"

echo "[4/7] d8"
"$BT/d8$BAT" --release --min-api 24 --lib "$ANDJAR" --output "$OUT/dex" \
  $(find "$OUT/classes" -name '*.class')

echo "[5/7] módulo base"
( cd "$OUT/module" && unzip -q ../base.apk \
  && mkdir -p manifest dex && mv AndroidManifest.xml manifest/ && cp ../dex/classes.dex dex/ \
  && zip -q -r ../base.zip . )

echo "[6/7] bundletool build-bundle"
java -jar "$BUNDLETOOL" build-bundle --modules="$OUT/base.zip" --output="$OUT/metroviu-unsigned.aab"

echo "[7/7] firma (upload key)"
MV_KS="$(cat "$KS_PASS_FILE")" jarsigner -sigalg SHA256withRSA -digestalg SHA-256 \
  -keystore "$KEYSTORE" -storepass:env MV_KS -keypass:env MV_KS \
  -signedjar "$OUT/metroviu.aab" "$OUT/metroviu-unsigned.aab" "$KEY_ALIAS" >/dev/null
java -jar "$BUNDLETOOL" validate --bundle="$OUT/metroviu.aab" >/dev/null
echo "OK -> $HERE/$OUT/metroviu.aab"
