// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <windows.h>
#include <cstdint>
constexpr DWORD NATIVE_PROTOCOL=2;
constexpr DWORD CHECK_ONLY=1;
constexpr DWORD REFRESH_TIMELINE=4;
constexpr DWORD REFRESH_USER=8;
struct NativeArgs {
    DWORD protocol, flags;
    wchar_t report[768];
    wchar_t cancel_event[128];
    char recipient[256];
    char text[3072];
    DWORD allow_count;
    char allowlist[8][256];
};
