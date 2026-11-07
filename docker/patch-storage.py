from pathlib import Path


entrypoint = Path("/webox/weagent/bin/entrypoint.sh")
source = entrypoint.read_text()
original = 'chown -R webox:webox "$WEBOX_STATE_DIR" "$XDG_RUNTIME_DIR"'
replacement = '''find "$WEBOX_STATE_DIR" "$XDG_RUNTIME_DIR" -xdev \\
    \\( -path "$HOME/xwechat_files" -o -path "$WEBOX_STATE_DIR/shared" \\) -prune \\
    -o -exec chown -h webox:webox {} +'''
if source.count(original) != 1:
    raise SystemExit("WeBox entrypoint changed: review storage compatibility before building")
entrypoint.write_text(source.replace(original, replacement))
