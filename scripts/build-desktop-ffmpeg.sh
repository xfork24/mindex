#!/usr/bin/env bash
# 从源码编译桌面版随包的 ffmpeg/ffprobe（Apple Silicon，GPL 构建）。
#
# - 成片编码用 libx264（GPL），因此整个构建是 GPL：发布时必须附带本脚本与 sources/ 下的全部源码包。
# - 烧录字幕需要 libass（subtitles 滤镜）。libass 使用 macOS CoreText 字体后端，不依赖 fontconfig；
#   字幕字体由 studio.subtitle.font_file 指定（桌面版为随包的 Noto Sans CJK SC）。
# - 依赖（x264、freetype、fribidi、harfbuzz、libass、dav1d）全部静态链接，产物只依赖 macOS 系统库与框架。
# - 所有源码按版本与 sha256 固定；pkg-config 只看本构建的前缀，不会混入 Homebrew 的库。
#
# 产物：desktop/build/ffmpeg/bin/{ffmpeg,ffprobe}；源码：desktop/build/ffmpeg/sources/。
# 需要：Xcode 命令行工具、pkg-config、uv（构建用的 meson/ninja 按固定版本装进 desktop/build/ffmpeg/tools）。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/desktop/build/ffmpeg"
SOURCES="${OUT}/sources"
WORK="${OUT}/work"
PREFIX="${OUT}/prefix"
JOBS="$(sysctl -n hw.ncpu)"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"

X264_COMMIT="b35605ace3ddf7c1a5d67a2eb553f034aef41d55"  # x264 stable 分支
# 文件名 | 下载地址 | sha256
PACKAGES=(
  "ffmpeg-9.0.2.tar.xz|https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz|8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
  "x264-${X264_COMMIT}.tar.bz2|https://code.videolan.org/videolan/x264/-/archive/${X264_COMMIT}/x264-${X264_COMMIT}.tar.bz2|6eeb82934e69fd51e043bd8c5b0d152839638d1ce7aa4eea65a3fedcf83ff224"
  "freetype-2.15.0.tar.xz|https://downloads.sourceforge.net/project/freetype/freetype2/2.15.0/freetype-2.15.0.tar.xz|93cd47eb719c325107f2d3cf087d06299e944c3b48a40b55537302304dea1dd5"
  "fribidi-1.0.17.tar.xz|https://github.com/fribidi/fribidi/releases/download/v1.0.17/fribidi-1.0.17.tar.xz|6949dcde27d41cebad1fd741fcafc36d55a1020d2d872d4a6eb3914caabbada2"
  "harfbuzz-15.0.0.tar.xz|https://github.com/harfbuzz/harfbuzz/releases/download/15.0.0/harfbuzz-15.0.0.tar.xz|968536e9abd782f07f7954044c0c4020199fcdd39b41d221ec42d2a3be4a44df"
  "libass-0.17.5.tar.xz|https://github.com/libass/libass/releases/download/0.17.5/libass-0.17.5.tar.xz|2dca25c0e0c837ddf00b52011b3f82cac1e4ddd3ad018227806b0c2288864acc"
  "dav1d-1.5.4.tar.xz|https://downloads.videolan.org/pub/videolan/dav1d/1.5.4/dav1d-1.5.4.tar.xz|686616b7c69eb88d44459391ab25cac13b6647a3b288835c5784e71c1514a5c5"
)

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "只支持在 Apple Silicon macOS 上构建" >&2
  exit 1
fi
for tool in pkg-config clang make uv; do
  command -v "$tool" >/dev/null || { echo "缺少构建工具：$tool" >&2; exit 1; }
done
MESON_VERSION="1.12.1"
NINJA_VERSION="1.13.2"
TOOLS="${OUT}/tools"
if [[ ! -x "${TOOLS}/bin/meson" ]] || [[ "$("${TOOLS}/bin/meson" --version)" != "${MESON_VERSION}" ]]; then
  rm -rf "${TOOLS}"
  uv venv --quiet --python 3.13 "${TOOLS}"
  uv pip install --quiet --python "${TOOLS}/bin/python" "meson==${MESON_VERSION}" "ninja==${NINJA_VERSION}"
fi

mkdir -p "${SOURCES}"
for entry in "${PACKAGES[@]}"; do
  IFS="|" read -r name url digest <<<"${entry}"
  file="${SOURCES}/${name}"
  if [[ ! -f "${file}" ]] || [[ "$(shasum -a 256 "${file}" | cut -d' ' -f1)" != "${digest}" ]]; then
    echo "下载 ${name}"
    curl -fsSL --retry 5 --retry-all-errors -o "${file}.partial" "${url}"
    mv "${file}.partial" "${file}"
  fi
  actual="$(shasum -a 256 "${file}" | cut -d' ' -f1)"
  if [[ "${actual}" != "${digest}" ]]; then
    echo "sha256 不符：${name}（期望 ${digest}，实际 ${actual}）" >&2
    exit 1
  fi
done
cp "${BASH_SOURCE[0]}" "${SOURCES}/build-desktop-ffmpeg.sh"  # 发布源码包时附带构建脚本

rm -rf "${WORK}" "${PREFIX}" "${OUT}/bin"
mkdir -p "${WORK}" "${PREFIX}" "${OUT}/bin"
# 只认本前缀的 .pc 文件；不让 Homebrew 的 glib/cairo/png 等被自动探测到。
export PKG_CONFIG_LIBDIR="${PREFIX}/lib/pkgconfig"
unset PKG_CONFIG_PATH
PKG_CONFIG_DIR="$(dirname "$(command -v pkg-config)")"
export PATH="${TOOLS}/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PKG_CONFIG_DIR}"
export CFLAGS="-O2 -mmacosx-version-min=${MACOSX_DEPLOYMENT_TARGET}"
export CXXFLAGS="${CFLAGS}"
export LDFLAGS="-mmacosx-version-min=${MACOSX_DEPLOYMENT_TARGET}"

unpack() { tar -xf "${SOURCES}/$1" -C "${WORK}"; }
meson_build() {  # 目录 选项…
  local dir="$1"; shift
  (cd "${WORK}/${dir}" && meson setup build --prefix="${PREFIX}" --libdir=lib --buildtype=release \
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

echo "构建 libass（CoreText 字体后端）"
unpack libass-0.17.5.tar.xz
meson_build libass-0.17.5 -Dfontconfig=disabled -Dcoretext=enabled -Ddirectwrite=disabled \
  -Dlibunibreak=disabled -Drequire-system-font-provider=true -Dtest=disabled -Dcompare=disabled \
  -Dprofile=disabled -Dfuzz=disabled -Dcheckasm=disabled

echo "构建 dav1d（AV1 软件解码）"
unpack dav1d-1.5.4.tar.xz
meson_build dav1d-1.5.4 -Denable_tools=false -Denable_tests=false -Denable_examples=false -Denable_docs=false

echo "构建 ffmpeg"
unpack ffmpeg-9.0.2.tar.xz
(cd "${WORK}/ffmpeg-9.0.2" && ./configure --prefix="${PREFIX}" --arch=arm64 --cc=clang --cxx=clang++ \
   --pkg-config-flags=--static --enable-gpl --disable-autodetect --disable-shared --enable-static \
   --enable-videotoolbox --enable-audiotoolbox --enable-zlib --enable-bzlib --enable-iconv \
   --enable-libx264 --enable-libass --enable-libfreetype --enable-libfribidi --enable-libharfbuzz --enable-libdav1d \
   --disable-ffplay --disable-doc --disable-debug \
   --extra-cflags="${CFLAGS} -I${PREFIX}/include" --extra-ldflags="${LDFLAGS} -L${PREFIX}/lib" --extra-libs="-lc++" \
   >"${WORK}/ffmpeg-configure.log" 2>&1 && make -j"${JOBS}" >"${WORK}/ffmpeg-build.log" 2>&1 \
   && make install >>"${WORK}/ffmpeg-build.log" 2>&1) || { tail -40 "${WORK}/ffmpeg-configure.log" "${WORK}/ffmpeg-build.log" >&2; exit 1; }

install -m 755 "${PREFIX}/bin/ffmpeg" "${PREFIX}/bin/ffprobe" "${OUT}/bin/"
strip -x "${OUT}/bin/ffmpeg" "${OUT}/bin/ffprobe"

# 验收：subtitles（libass）与 libx264 都在；只链接系统库与框架。先取完整输出再匹配（grep -q 提前退出会让
# pipefail 把 SIGPIPE 误判为失败）。
filters="$("${OUT}/bin/ffmpeg" -hide_banner -filters)"
encoders="$("${OUT}/bin/ffmpeg" -hide_banner -encoders)"
decoders="$("${OUT}/bin/ffmpeg" -hide_banner -decoders)"
grep -qE '^ \S+ subtitles ' <<<"${filters}" || { echo "缺少 subtitles 滤镜（libass）" >&2; exit 1; }
grep -qE '^ \S+ libx264 ' <<<"${encoders}" || { echo "缺少 libx264 编码器" >&2; exit 1; }
grep -qE '^ \S+ libdav1d ' <<<"${decoders}" || { echo "缺少 libdav1d 解码器" >&2; exit 1; }
links="$(otool -L "${OUT}/bin/ffmpeg" "${OUT}/bin/ffprobe")"
if grep -vE '^\S|/usr/lib/|/System/Library/' <<<"${links}" | grep -q .; then
  echo "${links}" >&2
  echo "ffmpeg 链接了非系统库" >&2
  exit 1
fi
if grep -q 'libhvf' <<<"${links}"; then echo "不应链接 libhvf（macOS 26 才有）" >&2; exit 1; fi
rm -rf "${WORK}" "${PREFIX}"
version="$("${OUT}/bin/ffmpeg" -hide_banner -version)"
echo "${version%%$'\n'*}"
echo "完成：${OUT}/bin（GPL 构建；发布时附带 ${SOURCES}）"
