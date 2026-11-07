#!/usr/bin/env bash
set -euo pipefail

state_dir="${WEBOX_STATE_DIR:-/webox/state}"
wechat_home="${WEBOX_WX_HOME:-${state_dir}/home}"
mkdir -p "$wechat_home/xwechat_files" "$state_dir/shared"
chown webox:webox "$state_dir" "$wechat_home"
storage_paths=("$wechat_home/xwechat_files" "$state_dir/shared")
if [ -n "${WEWARD_MEDIA_ACCOUNT:-}" ]; then
  if [[ "$WEWARD_MEDIA_ACCOUNT" == *'/'* || "$WEWARD_MEDIA_ACCOUNT" == '.' || "$WEWARD_MEDIA_ACCOUNT" == '..' ]]; then
    echo 'WEWARD_MEDIA_ACCOUNT must be a single account directory name' >&2
    exit 1
  fi
  account_path="$wechat_home/xwechat_files/$WEWARD_MEDIA_ACCOUNT"
  chown webox:webox "$account_path"
  storage_paths+=("$account_path/msg" "$account_path/resource")
fi
for storage_path in "${storage_paths[@]}"; do
  chown webox:webox "$storage_path" 2>/dev/null || true
  if ! gosu webox bash -c 'probe=$(mktemp "$1/.weward-write-XXXXXX") && rm -- "$probe"' _ "$storage_path"; then
    echo "WeChat storage is not writable by UID/GID 1000: $storage_path; prepare ownership on the storage host" >&2
    exit 1
  fi
done
