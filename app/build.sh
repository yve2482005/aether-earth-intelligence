#!/usr/bin/env bash
# AETHER Earth Intelligence — manual debug APK build (no Gradle plugin downloads).
# Steps:
#   (a) copy ../web/* -> src/main/assets/www/ (web team output)
#   (b) compile with javac against android.jar
#   (c) package with aapt2, dex with d8, sign debug with apksigner
# Output: build/aether-debug.apk
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$ROOT")"
TOOLING="$HOME/workspace/.tooling"
export JAVA_HOME="$TOOLING/jdk"
export PATH="$JAVA_HOME/bin:$PATH"
SDK="$TOOLING/android-sdk"
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
JAVAC="$TOOLING/jdk/bin/javac"
KEYTOOL="$TOOLING/jdk/bin/keytool"
BUILD="$ROOT/build"
ASSETS="$ROOT/src/main/assets/www"

echo "== AETHER Android build =="

# (a) Copy the web team's PWA into assets.
mkdir -p "$ASSETS"
WEB_FILES=0
if [ -d "$PROJECT/web" ]; then
    WEB_FILES=$(find "$PROJECT/web" -type f | wc -l)
fi

# (b) Clean + compile resources.
rm -rf "$BUILD"
mkdir -p "$BUILD/gen" "$BUILD/classes" "$BUILD/dex"

if [ "$WEB_FILES" -gt 0 ]; then
    # Clean-copy hygiene (2026-10-06): clear stale files first so deleted or
    # renamed web/ files (e.g. old snapshot JSONs) cannot linger in the APK.
    find "$ASSETS" -mindepth 1 -delete
    cp -r "$PROJECT/web/." "$ASSETS/"
    echo "copied $WEB_FILES file(s) from web/ -> src/main/assets/www/"
else
    echo "WARNING: ../web/ is empty or missing — APK will show the honest"
    echo "missing-assets error screen. Re-run build.sh after the web team delivers."
    cat > "$BUILD/ASSETS_NOTE.txt" <<'EOF'
../web/ was empty or missing at build time, so src/main/assets/www/ has no
index.html and the app shows its honest "content unavailable" error screen.
Follow-up: when the web team delivers web/, re-run app/build.sh — it copies
web/* -> src/main/assets/www/ automatically.
EOF
fi

"$BT/aapt2" compile --dir "$ROOT/src/res" -o "$BUILD/compiled_res.zip"

"$BT/aapt2" link -o "$BUILD/app-unsigned.apk" \
    -I "$PLATFORM" \
    --manifest "$ROOT/src/AndroidManifest.xml" \
    -A "$ROOT/src/main/assets" \
    --java "$BUILD/gen" \
    --min-sdk-version 26 \
    --target-sdk-version 34 \
    "$BUILD/compiled_res.zip"

# (c) Compile Java sources against android.jar.
"$JAVAC" -encoding UTF-8 -source 8 -target 8 -nowarn \
    -classpath "$PLATFORM" \
    -d "$BUILD/classes" \
    "$BUILD/gen/com/aether/earth/R.java" \
    "$ROOT/src/java/com/aether/earth/"*.java

# (d) Dex.
"$BT/d8" --min-api 26 --lib "$PLATFORM" \
    --output "$BUILD/dex" \
    $(find "$BUILD/classes" -name "*.class")

# (e) Add classes.dex to the APK.
python3 - "$BUILD/app-unsigned.apk" "$BUILD/dex/classes.dex" <<'EOF'
import sys, zipfile
apk, dex = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk, "a") as z:
    z.write(dex, "classes.dex")
print("added classes.dex")
EOF

# (f) Align.
"$BT/zipalign" -f 4 "$BUILD/app-unsigned.apk" "$BUILD/app-aligned.apk"

# (g) Debug keystore (auto-generated; release keys stay with the user).
KS="$BUILD/debug.keystore"
if [ ! -f "$KS" ]; then
    "$KEYTOOL" -genkeypair -keystore "$KS" -alias androiddebugkey \
        -keyalg RSA -keysize 2048 -validity 10950 \
        -storepass android -keypass android \
        -dname "CN=Android Debug,O=Android,C=US"
    echo "generated debug keystore"
fi

# (h) Sign.
"$BT/apksigner" sign \
    --ks "$KS" --ks-pass pass:android --key-pass pass:android \
    --out "$BUILD/aether-debug.apk" \
    "$BUILD/app-aligned.apk"

echo "== verify =="
"$BT/apksigner" verify --print-certs "$BUILD/aether-debug.apk"

SIZE=$(stat -c%s "$BUILD/aether-debug.apk")
echo "== done: $BUILD/aether-debug.apk ($SIZE bytes) =="
