#!/usr/bin/env bash
# 构建并签名物理输出采集 Helper。默认 ad-hoc，仅用于本机开发与验收。
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PACKAGE_DIR="${PROJECT_ROOT}/native/sona-audio-capture"
RESOURCE_DIR="${PACKAGE_DIR}/Resources"
OUTPUT_ROOT="${PROJECT_ROOT}/build/sona-audio-capture"
APP_PATH="${OUTPUT_ROOT}/sona-audio-capture.app"
SIGNING_IDENTITY=${SONA_AUDIO_CAPTURE_SIGNING_IDENTITY:--}
TIMESTAMP_MODE=${SONA_AUDIO_CAPTURE_CODESIGN_TIMESTAMP:-}
STAGING_ROOT=

fail() {
    echo "audio capture helper build failed: $1" >&2
    exit 1
}

cleanup() {
    if [[ -n "${STAGING_ROOT}" && -d "${STAGING_ROOT}" ]]; then
        /bin/rm -rf "${STAGING_ROOT}"
    fi
}

trap cleanup EXIT

command -v swift >/dev/null 2>&1 || fail "swift is unavailable"
[[ "$(uname -s)" == "Darwin" ]] || fail "macOS is required"
/usr/bin/plutil -lint "${RESOURCE_DIR}/Info.plist" >/dev/null
/usr/bin/plutil -lint "${RESOURCE_DIR}/SonaAudioCapture.entitlements" >/dev/null

if [[ -z "${TIMESTAMP_MODE}" ]]; then
    if [[ "${SIGNING_IDENTITY}" == "-" ]]; then
        TIMESTAMP_MODE=none
    else
        TIMESTAMP_MODE=auto
    fi
fi
case "${TIMESTAMP_MODE}" in
    none|auto|https://*) ;;
    *) fail "SONA_AUDIO_CAPTURE_CODESIGN_TIMESTAMP must be none, auto, or an HTTPS URL" ;;
esac

swift build \
    --package-path "${PACKAGE_DIR}" \
    -c release \
    --product sona-audio-capture-helper
BIN_DIR=$(swift build --package-path "${PACKAGE_DIR}" -c release --show-bin-path)
BUILT_EXECUTABLE="${BIN_DIR}/sona-audio-capture-helper"
[[ -x "${BUILT_EXECUTABLE}" ]] || fail "release executable is missing"

case "${APP_PATH}" in
    "${OUTPUT_ROOT}"/*.app) ;;
    *) fail "unsafe output path" ;;
esac
/bin/mkdir -p "${OUTPUT_ROOT}"
STAGING_ROOT=$(/usr/bin/mktemp -d "${OUTPUT_ROOT}/.audio-capture-build.XXXXXX")
STAGED_APP_PATH="${STAGING_ROOT}/sona-audio-capture.app"
CONTENTS_DIR="${STAGED_APP_PATH}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
EXECUTABLE="${MACOS_DIR}/sona-audio-capture-helper"
/bin/mkdir -p "${MACOS_DIR}"
/bin/cp "${RESOURCE_DIR}/Info.plist" "${CONTENTS_DIR}/Info.plist"
/bin/cp "${BUILT_EXECUTABLE}" "${EXECUTABLE}"
/bin/chmod 0755 "${EXECUTABLE}"

CODESIGN_ARGS=(
    --force
    --sign "${SIGNING_IDENTITY}"
    --options runtime
    --entitlements "${RESOURCE_DIR}/SonaAudioCapture.entitlements"
)
case "${TIMESTAMP_MODE}" in
    none) ;;
    auto) CODESIGN_ARGS+=(--timestamp) ;;
    https://*) CODESIGN_ARGS+=("--timestamp=${TIMESTAMP_MODE}") ;;
esac

/usr/bin/codesign "${CODESIGN_ARGS[@]}" "${STAGED_APP_PATH}"
/usr/bin/codesign --verify --deep --strict --verbose=2 "${STAGED_APP_PATH}"
/bin/rm -rf "${APP_PATH}"
/bin/mv "${STAGED_APP_PATH}" "${APP_PATH}"

if [[ "${SIGNING_IDENTITY}" == "-" ]]; then
    echo "warning: created an ad-hoc signed development bundle; this is not a release artifact" >&2
else
    echo "created a hardened signed bundle; notarization is still a separate release step"
fi
echo "audio capture helper app: ${APP_PATH}"
