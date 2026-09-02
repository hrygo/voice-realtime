#!/usr/bin/env bash
# 校验物理输出采集 Helper 的应用包；不会发起真实系统音频采集。
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
APP_PATH=${SONA_AUDIO_CAPTURE_APP_PATH:-"${PROJECT_ROOT}/build/sona-audio-capture/sona-audio-capture.app"}
MODE=${1:---static}
INFO_PLIST="${APP_PATH}/Contents/Info.plist"
EXECUTABLE="${APP_PATH}/Contents/MacOS/sona-audio-capture-helper"

fail() {
    echo "audio capture helper check failed: $1" >&2
    exit 1
}

plist_value() {
    /usr/bin/plutil -extract "$1" raw -o - "${INFO_PLIST}"
}

check_static_bundle() {
    [[ -d "${APP_PATH}" ]] || fail "app bundle is missing"
    [[ -f "${INFO_PLIST}" ]] || fail "Info.plist is missing"
    [[ -x "${EXECUTABLE}" ]] || fail "helper executable is missing"

    [[ "$(plist_value CFBundleIdentifier)" == "local.sona.audio-capture" ]] || \
        fail "unexpected bundle identifier"
    [[ "$(plist_value CFBundleExecutable)" == "sona-audio-capture-helper" ]] || \
        fail "unexpected executable name"
    [[ "$(plist_value CFBundlePackageType)" == "APPL" ]] || \
        fail "unexpected package type"
    [[ "$(plist_value LSUIElement)" == "true" ]] || fail "LSUIElement must be true"
    [[ "$(plist_value LSMinimumSystemVersion)" == "14.2" ]] || \
        fail "minimum macOS version must be 14.2"
    [[ -n "$(plist_value NSAudioCaptureUsageDescription)" ]] || \
        fail "audio capture usage description is missing"

    /usr/bin/file -b "${EXECUTABLE}" | /usr/bin/grep -q "Mach-O" || \
        fail "helper is not a Mach-O executable"
    /usr/bin/lipo -archs "${EXECUTABLE}" | /usr/bin/grep -qw "arm64" || \
        fail "helper does not contain arm64"
    /usr/bin/codesign --verify --deep --strict --verbose=2 "${APP_PATH}"

    local signature_details
    signature_details=$(/usr/bin/codesign -dvvv "${APP_PATH}" 2>&1)
    /usr/bin/grep -q "flags=.*runtime" <<<"${signature_details}" || \
        fail "Hardened Runtime is not enabled"

    local entitlements
    entitlements=$(/usr/bin/codesign -d --entitlements - "${APP_PATH}" 2>&1 || true)
    if /usr/bin/grep -Eq \
        "com\.apple\.security\.network\.(client|server)" <<<"${entitlements}"; then
        fail "network entitlement is forbidden"
    fi

    local forbidden_resource
    forbidden_resource=$(
        /usr/bin/find "${APP_PATH}" -type f \
            \( -name "*.wav" -o -name "*.pcm" -o -name "*.sock" -o -name "*.socket" \) \
            -print -quit
    )
    [[ -z "${forbidden_resource}" ]] || fail "audio or socket resource is bundled"

    echo "static bundle checks passed"
}

check_device_enumeration() {
    local devices_json
    devices_json=$("${EXECUTABLE}" --list-devices-json)
    /usr/bin/python3 -c '
import json
import re
import sys

devices = json.load(sys.stdin)
assert isinstance(devices, list) and len(devices) <= 128
allowed = {"built_in", "bluetooth", "usb", "hdmi", "display", "airplay", "virtual", "other"}
for device in devices:
    assert set(device) == {"device_ref", "label", "transport", "is_default"}
    assert re.fullmatch(r"vrdev1_[A-Za-z0-9_-]{43}", device["device_ref"])
    assert isinstance(device["label"], str) and 1 <= len(device["label"]) <= 128
    assert device["transport"] in allowed
    assert isinstance(device["is_default"], bool)
print({"device_count": len(devices), "default_count": sum(d["is_default"] for d in devices)})
' <<<"${devices_json}"
}

case "${MODE}" in
    --static)
        check_static_bundle
        ;;
    --list-devices)
        check_static_bundle
        check_device_enumeration
        ;;
    *)
        fail "usage: scripts/test-audio-capture-helper.sh [--static|--list-devices]"
        ;;
esac
