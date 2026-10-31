#!/usr/bin/env bash
# 从源码编译桌面版随包的 ffmpeg/ffprobe（macOS Apple Silicon / Linux x64 / Windows x64[MSYS2 UCRT64]，GPL 构建）。
#
# - 成片编码用 libx264（GPL），因此整个构建是 GPL：发布时必须附带本脚本与 sources/ 下的全部源码包。
# - 烧录字幕需要 libass（subtitles 滤镜）。字体后端按平台：macOS CoreText、Windows DirectWrite
#   （均为系统自带，不依赖 fontconfig）；Linux 不启用任何系统字体后端（studio.subtitle.font_file
#   总是显式指定字体文件，libass 直接加载该文件，不需要系统字体发现）。
# - 依赖（x264、freetype、fribidi、harfbuzz、libass、dav1d）全部静态链接；产物只依赖系统库
#   （macOS 系统库与框架 / Linux glibc 等 / Windows 系统 DLL）。
# - 所有源码按版本与 sha256 固定（三平台同一张表）；pkg-config 只看本构建的前缀，不混入系统第三方库。
#
# 产物：desktop/build/ffmpeg/bin/{ffmpeg,ffprobe}[.exe]；源码：desktop/build/ffmpeg/sources/。
# 需要：macOS——Xcode 命令行工具、pkg-config、uv（meson/ninja 按固定版本装进 tools）；
#       Linux——gcc/g++、make、pkg-config、nasm、uv；Windows——MSYS2 UCRT64 环境
#       （gcc/make/nasm/pkgconf/meson/ninja，经 CI 的 msys2/setup-msys2 或 pacman 安装）。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"  # 无论从哪个目录调用，后续步骤一律以项目根为工作目录
OUT="${ROOT}/desktop/build/ffmpeg"
SOURCES="${OUT}/sources"
WORK="${OUT}/work"
PREFIX="${OUT}/prefix"

OS="$(uname -s)"
PLATFORM="unsupported"
case "${OS}" in
  Darwin) PLATFORM="mac" ;;
  Linux) PLATFORM="linux" ;;
  MINGW64_NT*|MINGW32_NT*|UCRT64_NT*|MSYS_NT*)
    # 必须在 UCRT64（或 MINGW64）环境里跑；纯 MSYS shell 的工具链产物面向 cygwin，不能用。
    case "${MSYSTEM:-}" in
      UCRT64|MINGW64) PLATFORM="msys2" ;;
      *) echo "请在 MSYS2 的 UCRT64 环境运行（当前 MSYSTEM='${MSYSTEM:-无}'）" >&2; exit 1 ;;
    esac ;;
esac
if [[ "${PLATFORM}" == "unsupported" ]]; then
  echo "不支持的平台：${OS}（支持 macOS、Linux、MSYS2 UCRT64）" >&2
  exit 1
fi

JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc)"
if [[ "${PLATFORM}" == "mac" ]]; then
  export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
  for tool in pkg-config clang make uv; do
    command -v "$tool" >/dev/null || { echo "缺少构建工具：$tool" >&2; exit 1; }
  done
elif [[ "${PLATFORM}" == "linux" ]]; then
  for tool in gcc g++ make pkg-config nasm uv; do
    command -v "$tool" >/dev/null || { echo "缺少构建工具：$tool" >&2; exit 1; }
  done
else  # MSYS2 UCRT64：meson/ninja 来自 pacman（CI 经 msys2/setup-msys2 安装），不用 Windows 原生 uv。
  for tool in gcc make nasm pkgconf meson ninja; do
    command -v "$tool" >/dev/null || { echo "缺少构建工具：$tool（pacman -S mingw-w64-ucrt-x86_64-toolchain mingw-w64-ucrt-x86_64-nasm mingw-w64-ucrt-x86_64-pkgconf mingw-w64-ucrt-x86_64-meson mingw-w64-ucrt-x86_64-ninja）" >&2; exit 1; }
  done
fi

# meson/ninja：macOS/Linux 用固定版本装进独立 venv（不污染仓库 .venv）；MSYS2 用 pacman 的版本。
MESON="meson"
if [[ "${PLATFORM}" != "msys2" ]]; then
  MESON_VERSION="1.12.1"
  NINJA_VERSION="1.13.2"
  TOOLS="${OUT}/tools"
  if [[ ! -x "${TOOLS}/bin/meson" ]] || [[ "$("${TOOLS}/bin/meson" --version)" != "${MESON_VERSION}" ]]; then
    rm -rf "${TOOLS}"
    uv venv --quiet --python 3.13 "${TOOLS}"
    uv pip install --quiet --python "${TOOLS}/bin/python" "meson==${MESON_VERSION}" "ninja==${NINJA_VERSION}"
  fi
  MESON="${TOOLS}/bin/meson"
fi

X264_COMMIT="b35605ace3ddf7c1a5d67a2eb553f034aef41d55"  # x264 stable 分支
# 文件名 | 下载地址 | sha256（三平台同一张表）
PACKAGES=(
  "ffmpeg-9.0.2.tar.xz|https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz|8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
  "x264-${X264_COMMIT}.tar.bz2|https://code.videolan.org/videolan/x264/-/archive/${X264_COMMIT}/x264-${X264_COMMIT}.tar.bz2|6eeb82934e69fd51e043bd8c5b0d152839638d1ce7aa4eea65a3fedcf83ff224"
  "freetype-2.15.0.tar.xz|https://downloads.sourceforge.net/project/freetype/freetype2/2.15.0/freetype-2.15.0.tar.xz|93cd47eb719c325107f2d3cf087d06299e944c3b48a40b55537302304dea1dd5"
  "fribidi-1.0.17.tar.xz|https://github.com/fribidi/fribidi/releases/download/v1.0.17/fribidi-1.0.17.tar.xz|6949dcde27d41cebad1fd741fcafc36d55a1020d2d872d4a6eb3914caabbada2"
  "harfbuzz-15.0.0.tar.xz|https://github.com/harfbuzz/harfbuzz/releases/download/15.0.0/harfbuzz-15.0.0.tar.xz|968536e9abd782f07f7954044c0c4020199fcdd39b41d221ec42d2a3be4a44df"
  "libass-0.17.5.tar.xz|https://github.com/libass/libass/releases/download/0.17.5/libass-0.17.5.tar.xz|2dca25c0e0c837ddf00b52011b3f82cac1e4ddd3ad018227806b0c2288864acc"
  "dav1d-1.5.4.tar.xz|https://downloads.videolan.org/pub/videolan/dav1d/1.5.4/dav1d-1.5.4.tar.xz|686616b7c69eb88d44459391ab25cac13b6647a3b288835c5784e71c1514a5c5"
)

digest() {  # sha256（macOS 无 sha256sum）
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

mkdir -p "${SOURCES}"
for entry in "${PACKAGES[@]}"; do
  IFS="|" read -r name url sum <<<"${entry}"
  file="${SOURCES}/${name}"
  if [[ ! -f "${file}" ]] || [[ "$(digest "${file}")" != "${sum}" ]]; then
    echo "下载 ${name}"
    curl -fsSL --retry 5 --retry-all-errors -o "${file}.partial" "${url}"
    mv "${file}.partial" "${file}"
  fi
  actual="$(digest "${file}")"
  if [[ "${actual}" != "${sum}" ]]; then
    echo "sha256 不符：${name}（期望 ${sum}，实际 ${actual}）" >&2
    exit 1
  fi
done
cp "${BASH_SOURCE[0]}" "${SOURCES}/build-desktop-ffmpeg.sh"  # 发布源码包时附带构建脚本

rm -rf "${WORK}" "${PREFIX}" "${OUT}/bin"
mkdir -p "${WORK}" "${PREFIX}" "${OUT}/bin"
# 只认本前缀的 .pc 文件；不让 Homebrew/系统第三方（glib、cairo、png 等）被自动探测到。
export PKG_CONFIG_LIBDIR="${PREFIX}/lib/pkgconfig"
unset PKG_CONFIG_PATH
if [[ "${PLATFORM}" == "mac" ]]; then
  PKG_CONFIG_DIR="$(dirname "$(command -v pkg-config)")"
  export PATH="${TOOLS}/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PKG_CONFIG_DIR}"
  export CFLAGS="-O2 -mmacosx-version-min=${MACOSX_DEPLOYMENT_TARGET}"
  export CXXFLAGS="${CFLAGS}"
  export LDFLAGS="-mmacosx-version-min=${MACOSX_DEPLOYMENT_TARGET}"
elif [[ "${PLATFORM}" == "linux" ]]; then
  PKG_CONFIG_DIR="$(dirname "$(command -v pkg-config)")"
  export PATH="${TOOLS}/bin:/usr/bin:/bin:${PKG_CONFIG_DIR}"
  export CFLAGS="-O2 -fPIC"
  export CXXFLAGS="${CFLAGS}"
  export LDFLAGS=""
else  # MSYS2：PATH 由 UCRT64 环境提供（含 meson 的 python），隔离只靠 PKG_CONFIG_LIBDIR。
  export CFLAGS="-O2"
  export CXXFLAGS="${CFLAGS}"
  export LDFLAGS=""
fi

unpack() { tar -xf "${SOURCES}/$1" -C "${WORK}"; }
meson_build() {  # 目录 选项…
  local dir="$1"; shift
  (cd "${WORK}/${dir}" && "${MESON}" setup build --prefix="${PREFIX}" --libdir=lib --buildtype=release \
     --default-library=static --wrap-mode=nodownload "$@" >"${WORK}/${dir}.log" 2>&1 \
   && ninja -C build install >>"${WORK}/${dir}.log" 2>&1) || { tail -40 "${WORK}/${dir}.log" >&2; exit 1; }
}

echo "构建 x264"
unpack "x264-${X264_COMMIT}.tar.bz2"
(cd "${WORK}/x264-${X264_COMMIT}" && ./configure --prefix="${PREFIX}" --enable-static --enable-pic --disable-cli \
   --disable-opencl --disable-lsmash --disable-swscale --disable-lavf --disable-ffms --disable-gpac >/dev/null \
   && make -j"${JOBS}" >/dev/null 2>"${WORK}/x264-build.log" && make install-lib-static >/dev/null)

echo "构建 freetype"
unpack freetype-2.15.0.tar.xz
meson_build freetype-2.15.0 -Dbrotli=disabled -Dbzip2=disabled -Dharfbuzz=disabled -Dpng=disabled \
  -Dzlib=disabled -Dhvf=disabled -Dtests=disabled

echo "构建 fribidi"
unpack fribidi-1.0.17.tar.xz
meson_build fribidi-1.0.17 -Ddocs=false -Dbin=false -Dtests=false

echo "构建 harfbuzz"
unpack harfbuzz-15.0.0.tar.xz
meson_build harfbuzz-15.0.0 -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled \
  -Dchafa=disabled -Dpng=disabled -Dzlib=disabled -Dicu=disabled -Dcoretext=disabled -Draster=disabled \
  -Dvector=disabled -Dgpu=disabled -Dgpu_demo=disabled -Dsubset=disabled -Dtests=disabled \
  -Dintrospection=disabled -Ddocs=disabled -Dutilities=disabled -Dbenchmark=disabled

echo "构建 libass（字体后端：macOS CoreText / Windows DirectWrite / Linux 显式 font_file）"
unpack libass-0.17.5.tar.xz
if [[ "${PLATFORM}" == "mac" ]]; then
  meson_build libass-0.17.5 -Dfontconfig=disabled -Dcoretext=enabled -Ddirectwrite=disabled \
    -Dlibunibreak=disabled -Drequire-system-font-provider=true -Dtest=disabled -Dcompare=disabled \
    -Dprofile=disabled -Dfuzz=disabled -Dcheckasm=disabled
elif [[ "${PLATFORM}" == "linux" ]]; then
  # 不启用任何系统字体后端：studio.subtitle.font_file 总是显式指定，libass 直接加载该字体文件。
  meson_build libass-0.17.5 -Dfontconfig=disabled -Dcoretext=disabled -Ddirectwrite=disabled \
    -Dlibunibreak=disabled -Drequire-system-font-provider=false -Dtest=disabled -Dcompare=disabled \
    -Dprofile=disabled -Dfuzz=disabled -Dcheckasm=disabled
else
  meson_build libass-0.17.5 -Dfontconfig=disabled -Dcoretext=disabled -Ddirectwrite=enabled \
    -Dlibunibreak=disabled -Drequire-system-font-provider=true -Dtest=disabled -Dcompare=disabled \
    -Dprofile=disabled -Dfuzz=disabled -Dcheckasm=disabled
fi

echo "构建 dav1d（AV1 软件解码）"
unpack dav1d-1.5.4.tar.xz
meson_build dav1d-1.5.4 -Denable_tools=false -Denable_tests=false -Denable_examples=false -Denable_docs=false

echo "构建 ffmpeg"
COMMON_CONFIGURE="--pkg-config-flags=--static --enable-gpl --disable-autodetect --disable-shared --enable-static \
   --enable-libx264 --enable-libass --enable-libfreetype --enable-libfribidi --enable-libharfbuzz --enable-libdav1d \
   --disable-ffplay --disable-doc --disable-debug"
unpack ffmpeg-9.0.2.tar.xz
if [[ "${PLATFORM}" == "mac" ]]; then
  (cd "${WORK}/ffmpeg-9.0.2" && ./configure --prefix="${PREFIX}" --arch=arm64 --cc=clang --cxx=clang++ \
     ${COMMON_CONFIGURE} --enable-videotoolbox --enable-audiotoolbox --enable-zlib --enable-bzlib --enable-iconv \
     --extra-cflags="${CFLAGS} -I${PREFIX}/include" --extra-ldflags="${LDFLAGS} -L${PREFIX}/lib" --extra-libs="-lc++" \
     >"${WORK}/ffmpeg-configure.log" 2>&1 && make -j"${JOBS}" >"${WORK}/ffmpeg-build.log" 2>&1 \
     && make install >>"${WORK}/ffmpeg-build.log" 2>&1) || { tail -40 "${WORK}/ffmpeg-configure.log" "${WORK}/ffmpeg-build.log" >&2; exit 1; }
elif [[ "${PLATFORM}" == "linux" ]]; then
  (cd "${WORK}/ffmpeg-9.0.2" && ./configure --prefix="${PREFIX}" --cc=gcc --cxx=g++ \
     ${COMMON_CONFIGURE} --enable-pthreads --enable-zlib --enable-bzlib --enable-iconv \
     --extra-cflags="${CFLAGS} -I${PREFIX}/include" --extra-ldflags="${LDFLAGS} -L${PREFIX}/lib" --extra-libs="-lpthread -lm" \
     >"${WORK}/ffmpeg-configure.log" 2>&1 && make -j"${JOBS}" >"${WORK}/ffmpeg-build.log" 2>&1 \
     && make install >>"${WORK}/ffmpeg-build.log" 2>&1) || { tail -40 "${WORK}/ffmpeg-configure.log" "${WORK}/ffmpeg-build.log" >&2; exit 1; }
else
  # 静态链接 MinGW 运行时（-static-libgcc/-static-libwinpthread），不携带任何 MinGW DLL。
  (cd "${WORK}/ffmpeg-9.0.2" && ./configure --prefix="${PREFIX}" --arch=x86_64 --cc=gcc --cxx=g++ \
     ${COMMON_CONFIGURE} --enable-w32threads --disable-iconv \
     --extra-cflags="${CFLAGS} -I${PREFIX}/include" --extra-ldflags="-static -static-libgcc -static-libwinpthread -L${PREFIX}/lib" \
     >"${WORK}/ffmpeg-configure.log" 2>&1 && make -j"${JOBS}" >"${WORK}/ffmpeg-build.log" 2>&1 \
     && make install >>"${WORK}/ffmpeg-build.log" 2>&1) || { tail -40 "${WORK}/ffmpeg-configure.log" "${WORK}/ffmpeg-build.log" >&2; exit 1; }
fi

EXE=""
if [[ "${PLATFORM}" == "msys2" ]]; then EXE=".exe"; fi
install -m 755 "${PREFIX}/bin/ffmpeg${EXE}" "${PREFIX}/bin/ffprobe${EXE}" "${OUT}/bin/"
strip -x "${OUT}/bin/ffmpeg${EXE}" "${OUT}/bin/ffprobe${EXE}"

# 验收：subtitles（libass）与 libx264、libdav1d 都在；只依赖系统库。先取完整输出再匹配（grep -q 提前
# 退出会让 pipefail 把 SIGPIPE 误判为失败）。
filters="$("${OUT}/bin/ffmpeg${EXE}" -hide_banner -filters)"
encoders="$("${OUT}/bin/ffmpeg${EXE}" -hide_banner -encoders)"
decoders="$("${OUT}/bin/ffmpeg${EXE}" -hide_banner -decoders)"
grep -qE '^ \S+ subtitles ' <<<"${filters}" || { echo "缺少 subtitles 滤镜（libass）" >&2; exit 1; }
grep -qE '^ \S+ libx264 ' <<<"${encoders}" || { echo "缺少 libx264 编码器" >&2; exit 1; }
grep -qE '^ \S+ libdav1d ' <<<"${decoders}" || { echo "缺少 libdav1d 解码器" >&2; exit 1; }
if [[ "${PLATFORM}" == "mac" ]]; then
  links="$(otool -L "${OUT}/bin/ffmpeg" "${OUT}/bin/ffprobe")"
  if grep -vE '^\S|/usr/lib/|/System/Library/' <<<"${links}" | grep -q .; then
    echo "${links}" >&2
    echo "ffmpeg 链接了非系统库" >&2
    exit 1
  fi
  if grep -q 'libhvf' <<<"${links}"; then echo "不应链接 libhvf（macOS 26 才有）" >&2; exit 1; fi
elif [[ "${PLATFORM}" == "linux" ]]; then
  # 允许 glibc/zlib/bz2 等系统库（/lib、/usr/lib、ld-linux）；第三方路径（构建前缀、/opt、$HOME）不允许。
  links="$(ldd "${OUT}/bin/ffmpeg" "${OUT}/bin/ffprobe")"
  if grep -vE '^\S|/lib/|/lib64/|/usr/lib|ld-linux' <<<"${links}" | grep -qE '/'; then
    echo "${links}" >&2
    echo "ffmpeg 链接了非系统库" >&2
    exit 1
  fi
else
  # 只允许 Windows 系统 DLL；出现 libgcc/libwinpthread/libstdc++ 等 MinGW 运行时即静态链接不彻底。
  # DWrite.dll 是 libass DirectWrite 字体后端（系统组件，Windows 7+ 自带）的必然依赖。
  dlls="$(objdump -p "${OUT}/bin/ffmpeg.exe" "${OUT}/bin/ffprobe.exe" | grep "DLL Name:")"
  if grep -viE 'DLL Name: (KERNEL32|USER32|msvcrt|ucrtbase|api-ms-win-|ws2_32|advapi32|bcrypt|ole32|shell32|SHCore|secur32|crypt32|DWrite)\.dll' <<<"${dlls}" | grep -q .; then
    echo "${dlls}" >&2
    echo "ffmpeg.exe 依赖了非系统 DLL（MinGW 运行时未静态链接）" >&2
    exit 1
  fi
fi
rm -rf "${WORK}" "${PREFIX}"
version="$("${OUT}/bin/ffmpeg${EXE}" -hide_banner -version)"
echo "${version%%$'\n'*}"
echo "完成：${OUT}/bin（GPL 构建；发布时附带 ${SOURCES}）"
