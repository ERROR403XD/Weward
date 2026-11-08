#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
upstream_commit=8ca042cd79221d05f6a74cb48064c7ba14220dc9
upstream_dir="$project_root/vendor/webox"
docker buildx version >/dev/null
if [ ! -d "$upstream_dir/.git" ]; then
  git clone https://github.com/netcat-ai/webox.git "$upstream_dir"
fi
if [ -n "$(git -C "$upstream_dir" status --porcelain)" ]; then
  echo 'vendor/webox 存在源码修改，请先检查。' >&2
  exit 1
fi
git -C "$upstream_dir" checkout --detach "$upstream_commit"
case "$(uname -m)" in
  x86_64)
    package=WeChatLinux_x86_64.deb
    archive_package=WeChatLinux_4.1.1.8_x86_64.deb
    package_sha256=c9765e87ee5133bf4bb50d585c1814fafd995e3fb0da62c5ed07259b43dada7b
    ;;
  aarch64)
    package=WeChatLinux_arm64.deb
    archive_package=WeChatLinux_4.1.1.8_arm64.deb
    package_sha256=c3ed1a481247e6a1b166e87a66cccdee898c3ae0b76613b39bb6e9795e50929f
    ;;
  *) echo '仅支持 amd64/arm64' >&2; exit 1 ;;
esac
package_path="$upstream_dir/docker/wechat/$package"
if [ ! -f "$package_path" ]; then
  curl --fail --location --retry 3 --output "$package_path.part" \
    "https://github.com/trouter-ai/wechat-linux-versions/releases/download/v4.1.1.8/$archive_package"
  printf '%s  %s\n' "$package_sha256" "$package_path.part" | sha256sum -c -
  dpkg-deb --info "$package_path.part" >/dev/null
  mv "$package_path.part" "$package_path"
fi
dpkg-deb --info "$package_path" >/dev/null
printf '%s  %s\n' "$package_sha256" "$package_path" | sha256sum -c -
mkdir -p "$project_root/.local"
build_dir="$(mktemp -d "$project_root/.local/webox-build-XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT
git -C "$upstream_dir" archive "$upstream_commit" | tar -x -C "$build_dir"
# Isolate git apply from the parent project's worktree so no patch is skipped.
git -C "$build_dir" init --quiet
git -C "$build_dir" apply "$project_root/patches/webox/compat.patch"
git -C "$build_dir" apply --unidiff-zero "$project_root/patches/webox/groups.patch"
git -C "$build_dir" apply "$project_root/patches/webox/files.patch"
cp "$project_root/patches/webox/image_key.go" "$build_dir/internal/wechatdb/weward_image_key.go"
cp "$project_root/patches/webox/image_key_test.go" "$build_dir/internal/wechatdb/weward_image_key_test.go"
cp "$project_root/patches/webox/media_error_test.go" "$build_dir/internal/wechatapi/weward_media_error_test.go"
cp "$project_root/patches/webox/groups_db.go" "$build_dir/internal/wechatdb/weward_groups.go"
cp "$project_root/patches/webox/groups_state.go" "$build_dir/internal/wechat/weward_groups.go"
cp "$project_root/patches/webox/groups_api.go" "$build_dir/internal/wechatapi/weward_groups.go"
cp "$project_root/patches/webox/groups_db_test.go" "$build_dir/internal/wechatdb/weward_groups_test.go"
cp "$project_root/patches/webox/groups_api_test.go" "$build_dir/internal/wechatapi/weward_groups_test.go"
cp "$project_root/patches/webox/files_api.go" "$build_dir/internal/wechatapi/weward_files.go"
cp "$project_root/patches/webox/files_api_test.go" "$build_dir/internal/wechatapi/weward_files_test.go"
cp "$project_root/patches/webox/files_db_test.go" "$build_dir/internal/wechatdb/weward_files_test.go"
ln "$package_path" "$build_dir/docker/wechat/$package"
docker build --network=host \
  --build-arg HTTP_PROXY="${HTTP_PROXY:-}" \
  --build-arg HTTPS_PROXY="${HTTPS_PROXY:-}" \
  --build-arg NO_PROXY="${NO_PROXY:-}" \
  -t weward-webox:8ca042c-wx4.1.1.8-compat3 "$build_dir"
