#!/usr/bin/env python3
"""Translate hardcoded Chinese (CJK) text in app source files to English.

Design:
  - Whole-literal replacement: for every quoted string literal whose decoded
    content exists in the dictionary, replace it with the English translation.
  - Whole-segment replacement: for comment lines, HTML text segments, and
    remaining non-quoted text runs, replace the whole segment if it is in the
    dictionary. These segments are never matched as substrings of larger
    strings, so partial translations can never corrupt a string.
  - Anything without a dictionary entry is left unchanged and reported.

Usage:
    python3 tools/scripts/translate-cjk-to-en.py [--dry-run] [--report]
"""

import argparse
import os
import re
import sys
import json
from collections import Counter

CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]+')

EXCLUDE_BASENAMES = (
    'readme.md', 'readme-zh.md', 'readme-zh-cn.md', 'pull_request_template.md',
    'app_zh.arb', 'zh-CN.kv', 'en.kv', 'zh-CN.ts',
    'app_localizations.dart', 'app_localizations_en.dart', 'app_localizations_zh.dart',
    'manifest.json', 'tool_display_labels.json',
)
EXCLUDE_DIR_FRAGMENTS = (
    '/values-zh/', '/l10n/generated/', '/i18n/zh', '/l10n/app_zh',
    '/element/string.json',
)
EXCLUDE_TEST_PATHS = (
    'apps/flutter/app/test/core/link/CoreLinkCodec_test.dart',
    'apps/cli/tests/mcp_http_streaming.rs',
)
TEXT_EXTENSIONS = (
    '.dart', '.kt', '.java', '.swift', '.rs', '.ts', '.tsx', '.js', '.mjs',
    '.cjs', '.mts', '.py', '.sh', '.rb', '.cs', '.c', '.cc', '.cpp', '.h',
    '.hpp', '.go', '.html', '.css', '.xml', '.json', '.yaml', '.yml', '.md',
    '.txt', '.xaml', '.ps1', '.wxs', '.kv', '.plist', '.mm', '.toml',
    '.gradle', '.d.ts', '.d.mts', '.map', '.pyi',
)
PRUNE_DIRS = (
    '.git', 'node_modules', '.dart_tool', 'build', 'out', '.cache', '.venv',
    '__pycache__', 'dist', 'target', '.next', '.nuxt', '.turbo', 'coverage',
    '.arena', '.local', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    '.svelte-kit', '.tox', '.vscode', 'thirdparty', 'docs', 'tests-dist',
    'fixtures',
)


def is_excluded(rel):
    n = rel.lower().replace('\\', '/')
    base = n.rsplit('/', 1)[-1]
    parts = n.split('/')
    for frag in ('/docs/', '/thirdparty/', '/fixtures/', '/tests-dist/', '/.git/'):
        if frag in n: return True
    for frag in EXCLUDE_DIR_FRAGMENTS:
        if frag in n: return True
    if base in EXCLUDE_BASENAMES: return True
    if base.startswith('app_zh'): return True
    if '_zh.' in base or base.endswith('.zh.ts') or base.endswith('.zh.kt'): return True
    if base.endswith('.kv'): return True
    if base.endswith('.arb'): return True
    if base.endswith('-zh.md') or base == 'readme-zh.md': return True
    if rel in EXCLUDE_TEST_PATHS or n in EXCLUDE_TEST_PATHS: return True
    if base.startswith('readme'): return True
    if base == 'contributing.md': return True
    if base in ('license', 'license.md', 'license.txt'): return True
    return False


def decode_literal(raw):
    out = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == '\\' and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == 'n': out.append('\n')
            elif nxt == 't': out.append('\t')
            elif nxt == 'r': out.append('\r')
            elif nxt == '\\': out.append('\\')
            elif nxt == '"': out.append('"')
            elif nxt == "'": out.append("'")
            elif nxt == '`': out.append('`')
            elif nxt == '$': out.append('$')
            elif nxt == 'u':
                try:
                    cp = int(raw[i + 2:i + 6], 16)
                    if 0xD800 <= cp <= 0xDBFF:
                        out.append(c); out.append(nxt)
                    else:
                        out.append(chr(cp))
                    i += 5
                    continue
                except Exception:
                    out.append(c); out.append(nxt)
            else:
                out.append(c); out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def escape_for(text, quote):
    out = []
    for ch in text:
        if ch == '\\': out.append('\\\\')
        elif quote in ('"', "'") and ch == quote: out.append('\\' + quote)
        elif quote == '`' and ch == '`': out.append('\\`')
        elif ch == '\n': out.append('\\n')
        elif ch == '\t': out.append('\\t')
        elif ch == '\r': out.append('\\r')
        else: out.append(ch)
    return ''.join(out)


def load_dict(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


ZH_CONTEXT_RE = re.compile(r'(?:"|' + "'" + r')?\b(?:zh|zh_CN|zhCN|zh-cn)\b(?:"|' + "'" + r')?\s*:')


ZH_CONTEXT_RE = re.compile(r'(?:"|' + "'" + r')?\b(?:zh|zh_CN|zhCN|zh-cn)\b(?:"|' + "'" + r')?\s*:')
ZH_DECL_RE = re.compile(r'ZH_CN_I18N|zh_CN_I18N|ZH_CN_STRINGS|zh_cn_i18n|zh_i18n|zhCN')


def zh_locale_literal(text, start):
    """True when the literal at `start` sits inside a zh-locale context:
    - a line like "zh": "...", 'zh': '...', zh: "..."
    - inside a zh-locale dictionary block (e.g. `const ZH_CN_I18N = {`)
    Conservative line-level test: only lines mentioning a zh-ish token are
    protected, and inside such a block we protect everything until a balanced
    closing brace.
    """
    line_start = text.rfind('\n', 0, start) + 1
    line_end = text.find('\n', start)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    if re.search(r'\bzh\b|ZH_CN|zh_CN|zh-cn|zhCN|中文', line):
        prefix = line[:start - line_start]
        if ZH_CONTEXT_RE.search(prefix):
            return True
        # A plain line inside the declaration block.
        return True
    # Track into a zh-locale dictionary body: scan back for an opening
    # `const ZH_CN... = {` and count braces between it and start.
    decl = None
    for m in ZH_DECL_RE.finditer(text):
        if m.end() < start:
            decl = m.end()
    if decl is None:
        return False
    # Walk from the declaration up to start, tracking brace depth from the
    # first '{' after the declaration.
    depth = 0
    entered = False
    for ch in text[decl:start]:
        if ch == '{':
            depth += 1
            entered = True
        elif ch == '}':
            depth -= 1
            if depth == 0:
                entered = False
                return False
    return entered


def substitute_literals(text, dictionary):
    """Whole-literal replacement for quoted strings."""
    parts = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ('"', "'", '`'):
            quote = c
            j = i + 1
            while j < n:
                ch = text[j]
                if ch == '\\':
                    j += 2
                    continue
                if ch == quote:
                    break
                j += 1
            parts.append((i, j + 1, quote, text[i + 1:j]))
            i = j + 1
        else:
            i += 1
    out = []
    last = 0
    for start, end, quote, raw in parts:
        out.append(text[last:start])
        logical = decode_literal(raw)
        if (
            not zh_locale_literal(text, start)
            and logical in dictionary
        ):
            out.append(quote + escape_for(dictionary[logical], quote) + quote)
        else:
            out.append(text[start:end])
        last = end
    out.append(text[last:])
    return ''.join(out)


def is_comment_line(stripped):
    s = stripped.lstrip()
    return (
        s.startswith('//') or s.startswith('///') or s.startswith('/**')
        or s.startswith('/*') or s.startswith('#') or s.startswith('<!--')
        or s.startswith('-- ') or s.startswith('<!--')
    )


def substitute_comment_lines(text, dictionary):
    """Whole-line comment replacement for //, #, <!-- style comments."""
    lines = text.splitlines(keepends=True)
    changed = False
    for idx, line in enumerate(lines):
        body_start = idx  # placeholder
        stripped = line.lstrip()
        if not is_comment_line(stripped):
            continue
        # isolate the comment marker
        m = re.match(r'^(\s*)((?://{1,3}|/\*{1,2}|#|<!--|--)\s*)', line)
        if not m:
            continue
        indent = m.group(1)
        marker = m.group(2)
        content = line[m.end():].rstrip('\r\n')
        if re.search(r'[\u4e00-\u9fff]', content):
            stripped_content = content.strip()
            # try the whole content as one segment, then drop a trailing block closer
            for candidate in (stripped_content, stripped_content.rstrip('*').rstrip(), stripped_content.rstrip('\n')):
                cand = candidate.strip()
                if cand and cand in dictionary:
                    lines[idx] = indent + marker + ' ' + dictionary[cand] + ('\n' if line.endswith('\n') else '')
                    changed = True
                    break
    return ''.join(lines) if changed else text


def substitute_html_text(text, dictionary):
    """Whole-segment replacement for HTML text between tags."""
    parts = re.split(r'(<[^>]+>)', text)
    out = []
    for part in parts:
        if part.startswith('<'):
            out.append(part)
            continue
        stripped = part.strip()
        if stripped and stripped in dictionary:
            lead = part[:len(part) - len(part.lstrip())]
            trail = part[len(part.rstrip()):]
            out.append(lead + dictionary[stripped] + trail)
        else:
            out.append(part)
    return ''.join(out)


def substitute_non_literal_text(text, dictionary):
    """Whole-line replacement for remaining non-literal content that isn't a
    comment and isn't HTML. Handles plain-text durables (e.g. notes files)."""
    lines = text.splitlines(keepends=True)
    changed = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if is_comment_line(stripped):
            continue
        if not re.search(r'[\u4e00-\u9fff]', stripped):
            continue
        # whole-line match only — avoids substring corruption
        if stripped in dictionary:
            out = line[len(line) - len(line.lstrip()):]
            lines[idx] = dictionary[stripped] + ('\n' if line.endswith('\n') else '')
            changed = True
    return ''.join(lines) if changed else text


def process_text(text, dictionary, _miss=None):
    new_text = substitute_literals(text, dictionary)
    new_text = substitute_comment_lines(new_text, dictionary)
    new_text = substitute_html_text(new_text, dictionary)
    new_text = substitute_non_literal_text(new_text, dictionary)
    return new_text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--dictionary', default='tools/scripts/cjk_translations.json')
    ap.add_argument('--no-report', action='store_true')
    args = ap.parse_args()

    dict_path = args.dictionary
    if not os.path.isabs(dict_path):
        dict_path = os.path.join(os.getcwd(), dict_path)
    try:
        dictionary = load_dict(dict_path)
    except FileNotFoundError:
        print(f'dictionary not found: {dict_path}', file=sys.stderr)
        return 1

    changed_files = []
    remaining = []

    for root, dirs, names in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), '.')
            if is_excluded(rel):
                continue
            _, ext = os.path.splitext(name)
            if ext.lower() not in TEXT_EXTENSIONS:
                continue
            try:
                with open(rel, 'r', encoding='utf-8') as f:
                    text = f.read()
            except Exception:
                continue
            if '\x00' in text[:2000]:
                continue
            if not CJK_RE.search(text):
                continue
            new_text = process_text(text, dictionary)
            if new_text != text:
                changed_files.append(rel)
                if not args.dry_run:
                    with open(rel, 'w', encoding='utf-8') as f:
                        f.write(new_text)
            left = CJK_RE.findall(new_text)
            if left:
                remaining.append((rel, Counter(left)))

    print(f'changed files: {len(changed_files)}')
    print(f'files with remaining CJK: {len(remaining)}')
    if args.report and not args.no_report:
        remaining.sort(key=lambda x: -sum(x[1].values()))
        for rel, c in remaining:
            total = sum(c.values())
            print(f'\n{rel} ({total} remaining CJK chars):')
            for frag, count in c.most_common(12):
                print(f'    {count:4d}x  {frag}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
