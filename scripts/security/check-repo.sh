#!/bin/bash
# Orion repository scanner: looks for the signs of the 2026 "tip-rewrite" supply-chain worm,
# which hides obfuscated JavaScript in build config files, fake font files and editor autorun tasks.
# Run it BEFORE npm install / npm run dev / npm run build on any code you cloned or pulled.
#
#   bash scripts/security/check-repo.sh            # scans HEAD
#   bash scripts/security/check-repo.sh origin/main
#
# Exit 0 = nothing found. Exit 1 = a warning sign was found (or the target could not be scanned): do not build.
t="${1:-HEAD}"; found=0
say() { echo "  worm-guard: $1" >&2; found=1; }
# Fail closed: a tree-ish that does not resolve was never scanned, so it is not clean.
git rev-parse --verify -q "$t^{tree}" >/dev/null 2>&1 || { echo "  worm-guard: cannot resolve $t to a tree; nothing was scanned" >&2; exit 1; }
EXCL=(':(exclude).github' ':(exclude)docs/incident' ':(exclude)*.quarantine' ':(exclude)*.md')
git grep -q -I -E -e "global\[['\"][^'\"]{1,3}['\"]\]\s*=\s*['\"][A-Z]?[0-9]-[0-9]{4}" -e 'global\.[A-Za-z_]{1,3}\s*=\s*"[A-Z][0-9]-[0-9]{4}' -e '_padNc''Ywam' -e '\(global, ?require\)\);' -e "var [A-Za-z_$]{1,3} = '-?[0-9]{4}-[0-9]';\(function\(" "$t" -- . "${EXCL[@]}" 2>/dev/null && say "payload campaign marker present "
while read -r mode type sha size path; do
  [ -z "${path:-}" ] && continue
  case "$path" in
    *postcss.config.*|*tailwind.config.*|*next.config.*|*vite.config.*|*vitest.config.*|*eslint.config.*|*babel.config.*|*webpack.config.*)
      [ "$size" -gt 16384 ] && say "$path is $size bytes (implant makes build configs 30-38 KB)"
      n=$(git cat-file -p "$sha" | LC_ALL=C grep -aoE '_0x[0-9a-f]{4,}' | wc -l | tr -d ' '); [ "$n" -ge 20 ] && say "$path contains $n obfuscator identifiers (_0x....)" ;;
    *.woff|*.woff2|*.ttf|*.eot|*.otf)
      m=$(git cat-file -p "$sha" | head -c 4 | xxd -p)
      case "$m" in 774f4646|774f4632|00010000|4f54544f|74727565) ;; *) nul=$(git cat-file -p "$sha" | head -c 512 | LC_ALL=C tr -cd '\000' | wc -c | tr -d ' '); [ "$nul" = 0 ] && git cat-file -p "$sha" | head -c 64 | LC_ALL=C grep -aqE 'global\[|var _0x|function|const |let ' && say "fake font (JavaScript text): $path";; esac ;;
    */fonts/*.llf|*/fonts/*.js|*/fonts/*.mjs|*/fonts/*.cjs|*/fonts/*.orc) say "script file hiding in a fonts dir: $path" ;;
    */fonts/*)
      # 2026-10-31 variant: payload named fa-brand-400.orc. Any non-font, non-text file under fonts/ that starts like JS is the implant.
      case "$path" in *.woff|*.woff2|*.ttf|*.eot|*.otf|*.svg|*.css|*.txt|*.md|*.json|*.html|*.png|*.jpg|*.gif|*.webp) ;;
        *) git cat-file -p "$sha" | head -c 64 | LC_ALL=C grep -aqE '\(function\(|var _0x|global\[|const |let ' && say "script file hiding in a fonts dir: $path" ;; esac ;;
  esac
done < <(git ls-tree -r -l "$t" 2>/dev/null)
git show "$t:.vscode/tasks.json" 2>/dev/null | grep -q 'folderOpen' && say ".vscode/tasks.json runs a task on folder open"
git show "$t:.vscode/tasks.json" 2>/dev/null | grep -qE 'fonts/|\.orc|\.llf' && say ".vscode/tasks.json runs a file from a fonts dir"
git show "$t:.vscode/settings.json" 2>/dev/null | grep -q 'task.allowAutomaticTasks' && say ".vscode/settings.json enables automatic tasks"
# 2026-11-01: earlier-wave residue keeps an autorun task inside settings.json ("tasks": {"runOn": "folderOpen"})
git show "$t:.vscode/settings.json" 2>/dev/null | grep -q 'folderOpen' && say ".vscode/settings.json carries a folderOpen task"
git grep -q -e 'atob(process.env' "$t" -- '*.ts' '*.js' '*.mjs' '*.cjs' "${EXCL[@]}" 2>/dev/null && say "remote eval loader: atob(process.env...)"
git grep -q -I -e 'AUTH_API''_KEY' -e 'auth-confirm''-navy' -e 'aHR0cHM6Ly9hdXRo''LWNvbmZpcm0' "$t" -- . "${EXCL[@]}" ':(exclude)scripts/security/*' 2>/dev/null && say "stage-2 loader marker found"
git show "$t:.gitignore" 2>/dev/null | tr -d '\r' | grep -Eq '^(config\.bat|temp_auto_push|temp_interactive_push|branch_structure)' && say ".gitignore carries worm entries"
if [ "$found" = 1 ]; then echo "check-repo: warning signs found in $t. Do not build or open this folder in an editor." >&2; exit 1; fi
echo "check-repo: no warning signs found in $t."; exit 0
