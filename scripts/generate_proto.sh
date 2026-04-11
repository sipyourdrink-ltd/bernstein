#!/usr/bin/env bash
# Generate Python gRPC stubs from proto definitions.
# Requires: pip install grpcio-tools
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PROTO_DIR="$ROOT_DIR/proto"
OUT_DIR="$ROOT_DIR/src/bernstein/core/grpc_gen"

mkdir -p "$OUT_DIR"

python -m grpc_tools.protoc \
    -I"$PROTO_DIR" \
    --python_out="$OUT_DIR" \
    --pyi_out="$OUT_DIR" \
    --grpc_python_out="$OUT_DIR" \
    "$PROTO_DIR"/bernstein/v1/tasks.proto \
    "$PROTO_DIR"/bernstein/v1/cluster.proto

# Fix relative imports in generated code.
for f in "$OUT_DIR"/*.py; do
    sed -i.bak 's/^from bernstein\.v1 import/from . import/' "$f"
    rm -f "$f.bak"
done

touch "$OUT_DIR/__init__.py"

echo "Generated gRPC stubs in $OUT_DIR"
