#!/usr/bin/env bash
# Сборка ShipTracker.apk без Gradle.
# Нужны: JDK 17 (JAVA_HOME) и Android SDK с build-tools 34 и platforms/android-34.
# aapt2.exe не понимает пути с кириллицей, поэтому ему передаются короткие
# имена Windows (C:/Users/1F43~1/...) и сборка идёт во временной папке.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$HERE/.." && pwd)"
: "${JAVA_HOME:?задайте JAVA_HOME (JDK 17)}"
: "${ANDROID_HOME:?задайте ANDROID_HOME (папка Android SDK)}"

JAVA="$JAVA_HOME/bin/java"
BUILD_TOOLS="$ANDROID_HOME/build-tools/34.0.0"
ANDROID_JAR="$ANDROID_HOME/platforms/android-34/android.jar"
STAGE="${SHIPTRACKER_STAGE:-$ANDROID_HOME/apkstage}"
BUILD="$STAGE/build"
OUT="$APP_DIR/ShipTracker.apk"
KEYSTORE="${SHIPTRACKER_KEYSTORE:-$ANDROID_HOME/shiptracker.jks}"
KS_PASS="${SHIPTRACKER_KS_PASS:-shiptracker}"
PYTHON="${PYTHON:-python}"

find_tool() {
  for name in "$1.exe" "$1.bat" "$1"; do
    if [ -f "$BUILD_TOOLS/$name" ]; then echo "$BUILD_TOOLS/$name"; return 0; fi
  done
  echo "не найден инструмент $1 в $BUILD_TOOLS" >&2
  exit 1
}

AAPT2="$(find_tool aapt2)"
ZIPALIGN="$(find_tool zipalign)"
D8_JAR="$BUILD_TOOLS/lib/d8.jar"
APKSIGNER_JAR="$BUILD_TOOLS/lib/apksigner.jar"
[ -f "$D8_JAR" ] || { echo "нет $D8_JAR" >&2; exit 1; }
[ -f "$APKSIGNER_JAR" ] || { echo "нет $APKSIGNER_JAR" >&2; exit 1; }
[ -f "$ANDROID_JAR" ] || { echo "нет $ANDROID_JAR" >&2; exit 1; }

rm -rf "$STAGE"
mkdir -p "$BUILD/res" "$BUILD/classes" "$BUILD/dex" "$STAGE/src"
cp -r "$HERE/res" "$STAGE/src/res"
cp -r "$HERE/java" "$STAGE/src/java"
cp "$HERE/AndroidManifest.xml" "$STAGE/src/AndroidManifest.xml"

AAPT2_TOOL="$AAPT2"
if ! cygpath -ms "$BUILD" >/dev/null 2>&1; then
  echo "не удалось получить короткий путь для $BUILD" >&2
  exit 1
fi
SHORT_BUILD="$(cygpath -ms "$BUILD")"
SHORT_SRC="$(cygpath -ms "$STAGE/src")"
SHORT_JAR="$(cygpath -ms "$ANDROID_JAR")"

echo "1/5 ресурсы"
"$AAPT2_TOOL" compile --dir "$SHORT_SRC/res" -o "$SHORT_BUILD/res/resources.zip"
"$AAPT2_TOOL" link -o "$SHORT_BUILD/app-unsigned.apk" \
  -I "$SHORT_JAR" \
  --manifest "$SHORT_SRC/AndroidManifest.xml" \
  -R "$SHORT_BUILD/res/resources.zip" \
  --auto-add-overlay

echo "2/5 java"
# javac и d8 запускаются из папки сборки с относительными путями:
# так кириллица в пути не мешает ни им, ни файлам-спискам.
(cd "$STAGE/src" && find java -name '*.java') > "$STAGE/sources.txt"
(cd "$STAGE/src" && "$JAVA_HOME/bin/javac" -source 8 -target 8 -encoding UTF-8   -bootclasspath "$SHORT_JAR" -classpath "$SHORT_JAR"   -d "$SHORT_BUILD/classes" @"$STAGE/sources.txt")

echo "3/5 dex"
(cd "$BUILD" && find classes -name '*.class') > "$BUILD/classes.txt"
(cd "$BUILD" && "$JAVA" -cp "$D8_JAR" com.android.tools.r8.D8   --min-api 21 --lib "$SHORT_JAR"   --output dex @"classes.txt")

echo "4/5 упаковка"
"$PYTHON" - "$BUILD/app-unsigned.apk" "$BUILD/dex/classes.dex" <<'PY'
import sys, zipfile
apk, dex = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk, "a", zipfile.ZIP_DEFLATED) as z:
    z.write(dex, "classes.dex")
print("classes.dex добавлен")
PY
"$ZIPALIGN" -f 4 "$SHORT_BUILD/app-unsigned.apk" "$SHORT_BUILD/app-aligned.apk"

echo "5/5 подпись"
if [ ! -f "$KEYSTORE" ]; then
  "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$KEYSTORE" -alias shiptracker \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass "$KS_PASS" -keypass "$KS_PASS" \
    -dname "CN=Ship Tracker, O=Ship Tracker, C=UA" >/dev/null
fi
"$JAVA" -jar "$APKSIGNER_JAR" sign --ks "$KEYSTORE" \
  --ks-pass "pass:$KS_PASS" --key-pass "pass:$KS_PASS" \
  --out "$OUT" "$BUILD/app-aligned.apk"
"$JAVA" -jar "$APKSIGNER_JAR" verify "$OUT"

ls -la "$OUT"
echo "Готово: $OUT"
