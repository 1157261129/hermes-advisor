#!/usr/bin/env bash
# 检查上游 packages/rpiv-advisor 是否有新提交/新版本，与 .upstream.json 的固定记录对比。
# 用法：./scripts/check-upstream.sh      有漂移时退出码 1（不修改任何文件）。
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
pin="$root/.upstream.json"

read_pin() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$pin" "$1"
}

repo=$(read_pin repo)
path=$(read_pin path)
pkg=$(read_pin package)
pinned_commit=$(read_pin commit)
pinned_version=$(read_pin version)

latest_commit=$(curl -sSf "https://api.github.com/repos/$repo/commits?path=$path&per_page=1" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["sha"])')
latest_commit_date=$(curl -sSf "https://api.github.com/repos/$repo/commits?path=$path&per_page=1" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["commit"]["committer"]["date"][:10])')
latest_version=$(curl -sSf "https://registry.npmjs.org/$pkg/latest" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')

echo "pinned : $pkg $pinned_version @ ${pinned_commit:0:12}"
echo "latest : $pkg $latest_version @ ${latest_commit:0:12} ($latest_commit_date)"

drift=0
[ "$latest_commit" = "$pinned_commit" ] || drift=1
[ "$latest_version" = "$pinned_version" ] || drift=1

if [ "$drift" -eq 0 ]; then
  echo "OK: 上游未变（按 UPSTREAM.md 的核对清单复查即可）"
  exit 0
fi

echo "DRIFT: 上游已更新，按 UPSTREAM.md「上游更新时怎么办」逐项核对后更新 .upstream.json"
exit 1
