#!/usr/bin/env bash
# Generate Python gRPC stubs from proto definitions.
# CI checks the output of grpcio-tools==1.83.0.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PROTO_DIR="$ROOT_DIR/proto"
OUT_DIR="$ROOT_DIR/src/bernstein/core/grpc_gen"

if [[ $# -gt 1 || (${1:-} != "" && ${1:-} != "--check") ]]; then
    echo "Usage: $0 [--check]" >&2
    exit 2
fi

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
generated="$stage/bernstein/v1"

python -m grpc_tools.protoc \
    -I"$PROTO_DIR" \
    --python_out="$stage" \
    --pyi_out="$stage" \
    --grpc_python_out="$stage" \
    "$PROTO_DIR"/bernstein/v1/*.proto

# protoc mirrors the source package. Bernstein ships these modules together
# in the flat grpc_gen package, with relative imports between siblings.
for f in "$generated"/*.py "$generated"/*.pyi; do
    sed -i.bak 's/^from bernstein\.v1 import/from . import/' "$f"
    rm -f "$f.bak"
done

if [[ ${1:-} == "--check" ]]; then
    diff -ru -x '__init__.py' -x '__pycache__' "$OUT_DIR" "$generated"
    exit 0
fi

mkdir -p "$OUT_DIR"
for f in "$OUT_DIR"/*_pb2.py "$OUT_DIR"/*_pb2.pyi "$OUT_DIR"/*_pb2_grpc.py; do
    if [[ -f "$f" && ! -e "$generated/${f##*/}" ]]; then
        rm "$f"
    fi
done
cp "$generated"/*.py "$generated"/*.pyi "$OUT_DIR/"
touch "$OUT_DIR/__init__.py"

echo "Generated gRPC stubs in $OUT_DIR"
