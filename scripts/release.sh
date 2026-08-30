#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
  cat <<'EOF'
用法：scripts/release.sh [major|minor|patch]

默认递增 patch 版本。脚本会更新项目版本号，创建提交和 v<版本> tag，
然后依次推送当前分支与 tag 到 origin（可通过 RELEASE_REMOTE 覆盖远程名）。
执行前请先提交或丢弃工作区中的其他改动。
EOF
}

fail() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

if [[ $# -gt 1 ]]; then
  usage >&2
  exit 2
fi

bump_type="${1:-patch}"
if [[ "$bump_type" == "-h" || "$bump_type" == "--help" ]]; then
  usage
  exit 0
fi

case "$bump_type" in
  major|minor|patch) ;;
  *)
    usage >&2
    exit 2
    ;;
esac

for required_command in git awk mktemp; do
  command -v "$required_command" >/dev/null 2>&1 || fail "找不到必要命令：$required_command"
done

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

git_root="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "当前目录不是 Git 仓库"
cd "$git_root"

branch_name="$(git symbolic-ref --quiet --short HEAD)" || fail "当前处于 detached HEAD，请先切换到分支"
remote_name="${RELEASE_REMOTE:-origin}"
git remote get-url "$remote_name" >/dev/null 2>&1 || fail "找不到 Git 远程：$remote_name"

if [[ -n "$(git status --porcelain)" ]]; then
  fail "工作区存在未提交改动，请先提交或清理后再发布"
fi

project_version="$(awk -F'"' '
  $0 ~ /^version[[:space:]]*=[[:space:]]*"[0-9][0-9]*[.][0-9][0-9]*[.][0-9][0-9]*"[[:space:]]*$/ {
    print $2
    matches++
  }
  END {
    if (matches != 1) exit 1
  }
' pyproject.toml)" || fail "无法从 pyproject.toml 读取唯一的三段式版本号"

package_version="$(awk -F'"' '
  $0 ~ /^__version__[[:space:]]*=[[:space:]]*"[0-9][0-9]*[.][0-9][0-9]*[.][0-9][0-9]*"[[:space:]]*$/ {
    print $2
    matches++
  }
  END {
    if (matches != 1) exit 1
  }
' src/tg_botx/__init__.py)" || fail "无法从 src/tg_botx/__init__.py 读取唯一的三段式版本号"

[[ "$project_version" == "$package_version" ]] || \
  fail "版本号不一致：pyproject.toml=$project_version，src/tg_botx/__init__.py=$package_version"

IFS=. read -r current_major current_minor current_patch <<< "$project_version"
current_major=$((10#$current_major))
current_minor=$((10#$current_minor))
current_patch=$((10#$current_patch))

case "$bump_type" in
  major)
    next_major=$((current_major + 1))
    next_minor=0
    next_patch=0
    ;;
  minor)
    next_major=$current_major
    next_minor=$((current_minor + 1))
    next_patch=0
    ;;
  patch)
    next_major=$current_major
    next_minor=$current_minor
    next_patch=$((current_patch + 1))
    ;;
esac

next_version="$next_major.$next_minor.$next_patch"
tag_name="v$next_version"

git rev-parse --verify --quiet "refs/tags/$tag_name" >/dev/null && \
  fail "本地 tag 已存在：$tag_name"

remote_tag="$(git ls-remote --tags "$remote_name" "refs/tags/$tag_name" 2>/dev/null)" || \
  fail "无法查询远程 tag，请检查网络和远程权限：$remote_name"
[[ -z "$remote_tag" ]] || fail "远程 tag 已存在：$tag_name"

update_version_file() {
  local file_path="$1"
  local version_key="$2"
  local replacement="$3"
  local temporary_file

  temporary_file="$(mktemp "${TMPDIR:-/tmp}/tg-botx-release.XXXXXX")" || \
    fail "无法创建临时文件"

  if ! awk -v key="$version_key" -v new_version="$replacement" '
    BEGIN {
      pattern = "^" key "[[:space:]]*=[[:space:]]*\"[0-9][0-9]*[.][0-9][0-9]*[.][0-9][0-9]*\"[[:space:]]*$"
    }
    {
      if ($0 ~ pattern) {
        matches++
        if (matches == 1) {
          sub(/"[0-9][0-9]*[.][0-9][0-9]*[.][0-9][0-9]*"/, "\"" new_version "\"")
        }
      }
      print
    }
    END {
      if (matches != 1) exit 1
    }
  ' "$file_path" > "$temporary_file"; then
    rm -f "$temporary_file"
    fail "无法更新版本号：$file_path"
  fi

  mv "$temporary_file" "$file_path"
}

update_version_file pyproject.toml version "$next_version"
update_version_file src/tg_botx/__init__.py __version__ "$next_version"

git add pyproject.toml src/tg_botx/__init__.py
git diff --cached --quiet && fail "版本号没有产生变更"
git commit -m "chore(release): 发布 v$next_version"
git push "$remote_name" "$branch_name"

git tag -a "$tag_name" -m "发布 v$next_version"
git push "$remote_name" "$tag_name"

printf '发布完成：v%s\n' "$next_version"
