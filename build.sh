#!/bin/bash
# Build static yue-synth, yue-plan, yue-transcribe and yue-tokenize (Metal + Accelerate) from yue2.cpp into ./bin.
# Needs git, cmake and the Xcode command line tools.
set -eu
cd "$(dirname "$0")"
REV=8af0174

[ -d yue2.cpp ] || git clone https://github.com/ServeurpersoCom/yue2.cpp
cd yue2.cpp
git checkout -q "$REV"
git submodule update --init --depth 1

cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DGGML_BLAS=ON
cmake --build build --config Release -j "$(sysctl -n hw.ncpu)" --target yue-synth yue-plan yue-transcribe yue-tokenize

mkdir -p ../bin
cp build/yue-synth build/yue-plan build/yue-transcribe build/yue-tokenize ../bin/
echo "Built: $(cd ../bin && pwd)"
