#!/usr/bin/env bash
# Сборка автономного APK.
#
# Android-сборка не работает, если в пути есть кириллица (aapt2 и проверка
# AGP), поэтому проект копируется в ASCII-путь и собирается там, а готовый
# APK возвращается в папку проекта.
#
# Нужны: JDK 17 (JAVA_HOME), Android SDK с platforms/android-34 и
# build-tools/34.0.0 (ANDROID_HOME), Gradle 8.x (GRADLE или gradle в PATH).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
: "${JAVA_HOME:?задайте JAVA_HOME (JDK 17)}"
: "${ANDROID_HOME:?задайте ANDROID_HOME (папка Android SDK)}"
GRADLE_BIN="${GRADLE:-gradle}"
BUILD_DIR="${SHIPTRACKER_BUILD_DIR:-C:/Users/Public/st-android-build}"
# В имени файла — версия из build.gradle: по папке сразу видно, что за сборка.
VERSION_NAME="$(sed -n 's/.*versionName[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' \
  "$HERE/app/build.gradle" | head -1)"
[ -n "$VERSION_NAME" ] || VERSION_NAME="dev"
OUT="$HERE/ShipTracker-autonomous-$VERSION_NAME.apk"

# Модули и шаблон настроек (android_defaults.py) генерируются из исходников;
# в git его нет, потому что в нём остаются рабочие ключи без ограничений.
if [ -f "$HERE/sync_python.sh" ] && [ -f "$HERE/../ship_tracker/data/config.json" ]; then
  bash "$HERE/sync_python.sh" >/dev/null
fi

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cp -r "$HERE/app" "$HERE/build.gradle" "$HERE/settings.gradle" \
      "$HERE/gradle.properties" "$BUILD_DIR/"

SDK_POSIX="$(cd "$ANDROID_HOME" && pwd -W 2>/dev/null || echo "$ANDROID_HOME")"
printf 'sdk.dir=%s\n' "$SDK_POSIX" > "$BUILD_DIR/local.properties"

cd "$BUILD_DIR"
"$GRADLE_BIN" --no-daemon --console=plain assembleDebug

APK="$BUILD_DIR/app/build/outputs/apk/debug/app-debug.apk"
[ -f "$APK" ] || { echo "APK не собран: $APK" >&2; exit 1; }
cp "$APK" "$OUT"
# в папке должен остаться один APK: прежние сборки (без версии в имени и
# с другим номером) убираем, чтобы не перепутать файлы
for old in "$HERE"/ShipTracker-autonomous*.apk; do
  [ "$old" = "$OUT" ] || rm -f "$old"
done
ls -la "$OUT"
echo "Готово: $OUT"
