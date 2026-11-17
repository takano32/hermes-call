#!/usr/bin/env bash
# Build the official PJSUA2 Python binding into the sidecar venv.
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo 'Usage: build-pjsip.sh /absolute/venv /absolute/build-dir' >&2
  exit 1
fi
venv_dir=$1
build_root=$2
if [[ "$venv_dir" != /* || "$build_root" != /* || ! -x "$venv_dir/bin/python" ]]; then
  echo 'An existing venv and absolute build path are required.' >&2
  exit 1
fi
if "$venv_dir/bin/python" -c \
  'import pjsua2; import importlib.metadata; assert importlib.metadata.version("pjsua2") == "2.17"' \
  2>/dev/null; then
  exit 0
fi
mkdir -p "$build_root"
build_dir=$(mktemp -d "$build_root/pjsip-XXXXXXXX")
git clone --depth 1 --branch 2.17 https://github.com/pjsip/pjproject.git "$build_dir/source"
cd "$build_dir/source"
# Verify the official release commit prefix as well as the release tag.
if [[ $(git rev-parse --short=7 HEAD) != 5a45745 ]]; then
  echo 'Unexpected PJSIP 2.17 source revision. Refusing to build.' >&2
  exit 1
fi
printf '#define PJMEDIA_HAS_VIDEO 0\n' > pjlib/include/pj/config_site.h
CFLAGS='-O2 -fPIC' CXXFLAGS='-O2 -fPIC' ./configure \
  --disable-video --disable-sound --disable-opencore-amr --disable-silk \
  --disable-libyuv --disable-ffmpeg --with-external-srtp
make dep
make -j2
make -C pjsip-apps/src/swig/python wheel PYTHON_EXE="$venv_dir/bin/python"
wheels=(pjsip-apps/src/swig/python/dist/*.whl)
if [[ ${#wheels[@]} -ne 1 || ! -f ${wheels[0]} ]]; then
  echo 'Expected exactly one PJSUA2 wheel.' >&2
  exit 1
fi
"$venv_dir/bin/python" -m pip install --force-reinstall --no-deps "${wheels[0]}"
"$venv_dir/bin/python" -c 'import pjsua2; assert hasattr(pjsua2, "AudioMediaPort")'
printf 'PJSIP build retained at %s\n' "$build_dir"
