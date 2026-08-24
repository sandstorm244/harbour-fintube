#!/bin/sh
# Shadow build — keeps this source tree pristine. All build output (intermediates AND the RPM)
# goes to a sibling "<app>.build/" directory; nothing lands in the source dir.
#   RPM ends up in:  ../harbour-fintube.build/RPMS/
# Override the SDK target with:  TARGET=SailfishOS-x.y.z-aarch64 sh build.sh
set -e

TARGET="${TARGET:-SailfishOS-5.1.0.11-aarch64.default}"
SRC="$(cd "$(dirname "$0")" && pwd)"
BUILD="$SRC.build"

mkdir -p "$BUILD"
cd "$BUILD"
echo "Shadow-building $SRC"
echo "            → $BUILD  (target=$TARGET)"
sfdk -c "target=$TARGET" build "$SRC"
echo "Done. RPM(s) in: $BUILD/RPMS"
