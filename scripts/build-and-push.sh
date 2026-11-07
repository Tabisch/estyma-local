#!/usr/bin/env bash
# Pull the latest code, build the estyma-proxy image and push it to a registry.
#
# Usage:  scripts/build-and-push.sh            (uses the settings below)
#         REGISTRY=nas.lan:5000 PLATFORMS=linux/amd64,linux/arm64 scripts/build-and-push.sh
#
# Settings (environment variables):
#   REGISTRY   registry host:port                  (default localhost:5000)
#   IMAGE      image name inside the registry      (default estyma-local)
#   REPO_URL   git repository to build             (default GitHub repo)
#   BRANCH     branch to build                     (default main)
#   SRC_DIR    where the checkout is kept          (default ~/.cache/estyma-local-src)
#   PLATFORMS  empty = build for this machine only with plain `docker build`.
#              Set e.g. linux/amd64,linux/arm64,linux/arm/v7 for a multi-arch
#              image via buildx (needs QEMU, see README).
#   INSECURE   1 = registry is plain HTTP (default 1). Only matters for buildx.
set -euo pipefail

REGISTRY="${REGISTRY:-localhost:5000}"
IMAGE="${IMAGE:-estyma-local}"
REPO_URL="${REPO_URL:-https://github.com/Tabisch/estyma-local.git}"
BRANCH="${BRANCH:-main}"
SRC_DIR="${SRC_DIR:-$HOME/.cache/estyma-local-src}"
PLATFORMS="${PLATFORMS:-}"
INSECURE="${INSECURE:-1}"
BUILDER="estyma-builder"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# 1. Get the source ------------------------------------------------------
if [ -d "$SRC_DIR/.git" ]; then
  log "Updating $SRC_DIR ($BRANCH)"
  git -C "$SRC_DIR" fetch --quiet origin "$BRANCH"
  git -C "$SRC_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
  git -C "$SRC_DIR" reset --quiet --hard "origin/$BRANCH"
else
  log "Cloning $REPO_URL ($BRANCH) into $SRC_DIR"
  mkdir -p "$(dirname "$SRC_DIR")"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"
fi
SHA="$(git -C "$SRC_DIR" rev-parse --short HEAD)"
REF="$REGISTRY/$IMAGE"
log "Building $REF:latest and $REF:$SHA"

# 2. Build and push ------------------------------------------------------
if [ -z "$PLATFORMS" ]; then
  # Native architecture only. For a plain-HTTP registry other than localhost,
  # the Docker daemon needs it in "insecure-registries" (see README).
  docker build --pull -t "$REF:latest" -t "$REF:$SHA" "$SRC_DIR"
  docker push "$REF:latest"
  docker push "$REF:$SHA"
else
  # Multi-arch via buildx. The buildx builder runs in its own container, so it
  # does not see the daemon's insecure-registries setting; give it its own.
  if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    log "Creating buildx builder $BUILDER"
    cfg="$(mktemp)"
    if [ "$INSECURE" = "1" ]; then
      printf '[registry."%s"]\n  http = true\n  insecure = true\n' "$REGISTRY" > "$cfg"
    fi
    # host networking so "localhost:5000" means the host, not the builder container
    docker buildx create --name "$BUILDER" --driver docker-container \
      --driver-opt network=host --config "$cfg" >/dev/null
    rm -f "$cfg"
  fi
  docker buildx build --builder "$BUILDER" --pull \
    --platform "$PLATFORMS" \
    -t "$REF:latest" -t "$REF:$SHA" \
    --push "$SRC_DIR"
fi

log "Done: $REF:latest ($SHA)"
