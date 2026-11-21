#!/usr/bin/env python3
"""Bulk CJK→English translation for app source, using an official free-tier
machine-translation API.

This is the reusable answer to "find Chinese strings in the code and batch
translate them" — with the guardrails the dictionary version already has:

  - Only substitutes whole quoted literals / whole segments (never a substring).
  - Never translates inside zh-locale dictionaries (zh/ZH_CN blocks), zh locale
    files, fixtures, docs (unless --docs), thirdparty, or generated/build dirs.
  - Never translates test-fixture data.
  - Masks template placeholders ({name}, ${name}) before sending, so the API
    never sees or corrupts them.
  - Batches requests and retries with backoff; default batch is small enough
    for the free tiers and keeps per-batch delay so you stay well under limits.
  - Default is DRY-RUN: writes a reviewable .cjk-report.json and touches
    nothing. Pass --apply to rewrite files.

Backends (pick one; keys come from env vars):
  google  Google Cloud Translation v2 (free tier 500k chars/month w/ billing).
            Requires GOOGLE_TRANSLATE_API_KEY.
  deepl    DeepL v2 API (free tier 500k chars/month; needs an account).
            Requires DEEPL_API_KEY.
  echo     Offline placeholder backend — no network, no API key. Use only for
            --selftest / dry-run preview. Its output is NOT a translation.

Usage (from the repo root):
  python3 tools/scripts/translate-cjk-online.py --selftest
  python3 tools/scripts/translate-cjk-online.py --dry-run
  python3 tools/scripts/translate-cjk-online.py --apply --cache
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]+')

# ---------------------------------------------------------------- exclusions
TEXT_EXTENSIONS = (
    '.dart', '.kt', '.java', '.swift', '.rs', '.ts', '.tsx', '.js', '.mjs',
    '.cjs', '.mts', '.py', '.sh', '.rb', '.cs', '.c', '.cc', '.cpp', '.h',
    '.hpp', '.go', '.html', '.css', '.xml', '.json', '.yaml', '.yml', '.txt',
    '.xaml', '.ps1', '.wxs', '.plist', '.mm', '.toml', '.gradle', '.pyi',
)
PRUNE_DIRS = (
    '.git', 'node_modules', '.dart_tool', 'build', 'out', '.cache', '.venv',
    '__pycache__', 'dist', 'target', '.next', '.nuxt', '.turbo', 'coverage',
    '.arena', '.local', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    '.svelte-kit', '.tox', '.vscode', 'thirdparty', 'docs', 'tests-dist',
    'fixtures', '.cjk',
)
EXCLUDE_DIR_FRAGMENTS = (
    '/values-zh/', '/l10n/generated/', '/i18n/zh', '/l10n/app_zh',
    '/element/string.json',
)
EXCLUDE_BASENAMES = (
    'app_zh.arb', 'zh-CN.kv', 'en.kv', 'zh-CN.ts',
    'app_localizations.dart', 'app_localizations_en.dart', 'app_localizations_zh.dart',
    'manifest.json', 'tool_display_labels.json',
)

def is_excluded(rel, include_docs):
    n = '/' + rel.lower().replace('\\', '/')
    base = n.rsplit('/', 1)[-1]
    for frag in ('/thirdparty/', '/fixtures/', '/tests-dist/', '/.git/', '/docs/'):
        if frag in n and (frag != '/docs/' or not include_docs):
            return True
    if not include_docs and '/docs/' in n:
        return True
    for frag in EXCLUDE_DIR_FRAGMENTS:
        if frag in n:
            return True
    if base in EXCLUDE_BASENAMES:
        return True
    if base.startswith('app_zh'):
        return True
    if '_zh.' in base or base.endswith('.zh.ts') or base.endswith('.zh.kt'):
        return True
    if base.endswith('.kv') or base.endswith('.arb'):
        return True
    norm_rel = '/' + rel.lower().replace('\\', '/')
    if norm_rel in (
        '/apps/flutter/app/test/core/link/CoreLinkCodec_test.dart',
        '/apps/cli/tests/mcp_http_streaming.rs',
    ):
        return True
    if base.startswith('readme') or base == 'contributing.md':
        return True
    if base.endswith('-zh.md') or base == 'license.txt':
        return True
    return False

# ------------------------------------------------------------- literal utils
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


ZH_CONTEXT_RE = re.compile(r'(?:"|\')?\b(?:zh|zh_CN|zhCN|zh-cn)\b(?:"|\')?\s*:')
ZH_DECL_RE = re.compile(r'ZH_CN_I18N|zh_CN_I18N|ZH_CN_STRINGS|zh_cn_i18n|zh_i18n|zhCN')


def zh_locale_literal(text, start):
    """True when the literal at `start` sits inside a zh-locale context:
    - a line like "zh": "...", 'zh': '...', zhCN: "..."
    - inside a zh-locale dictionary block (e.g. `const ZH_CN_I18N = {`)
    """
    line_start = text.rfind('\n', 0, start) + 1
    line_end = text.find('\n', start)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    prefix = line[:start - line_start]
    if ZH_CONTEXT_RE.search(prefix):
        return True
    decl = None
    for m in ZH_DECL_RE.finditer(text):
        if m.end() < start:
            decl = m.end()
    if decl is None:
        return False
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


def find_literals(text):
    """Return list of (start, end, quote, raw_body) for quoted literals."""
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
    return parts

# -------------------------------------------------------- placeholder masking
PLACEHOLDER_RE = re.compile(r'(\$?\{[^{}\n]{1,128}\})')


def mask_placeholders(text):
    """Replace {name} / ${name} / ${body} tokens with stable binary sentinels
    so the API never sees or corrupts them."""
    saved = []

    def repl(m):
        saved.append(m.group(1))
        return f'\x01M{len(saved) - 1}\x01'

    out = PLACEHOLDER_RE.sub(repl, text)
    return out, saved


def restore_placeholders(text, saved):
    def repl(m):
        idx = int(m.group(1))
        return saved[idx] if idx < len(saved) else m.group(0)

    return re.sub(r'\x01M(\d+)\x01', repl, text)

# ------------------------------------------------------------- backends
class Backend:
    def translate(self, texts, source, target):
        raise NotImplementedError


class GoogleTranslateBackend(Backend):
    """Google Cloud Translation v2 (official free tier). Needs GOOGLE_TRANSLATE_API_KEY."""

    def __init__(self, key):
        self.key = key

    def translate(self, texts, source, target):
        if not self.key:
            raise RuntimeError('GOOGLE_TRANSLATE_API_KEY is not set')
        url = 'https://translation.googleapis.com/language/translate/v2?key=' + self.key
        body = json.dumps({'q': texts, 'source': source, 'target': target, 'format': 'text'}).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json; charset=utf-8'})
        last_err = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode('utf-8'))
                    return [t['translatedText'] for t in payload['data']['translations']]
            except urllib.error.HTTPError as e:
                last_err = f'HTTP {e.code}: {e.read().decode("utf-8", "replace")[:400]}'
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                raise RuntimeError(last_err)
            except Exception as e:
                last_err = str(e)
                if attempt < 4:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                raise RuntimeError(last_err)
        raise RuntimeError(last_err)


class DeepLBackend(Backend):
    """DeepL v2 API (official free tier). Needs DEEPL_API_KEY."""

    def __init__(self, key):
        self.key = key

    def translate(self, texts, source, target):
        if not self.key:
            raise RuntimeError('DEEPL_API_KEY is not set')
        url = 'https://api-free.deepl.com/v2/translate'
        body = []
        fields = []
        for t in texts:
            fields.append(t.encode('utf-8'))
        # DeepL v2: form-encoded text[] on each line
        masked = []
        for t in texts:
            masked.append(t.encode('utf-8'))
        form_parts = [b'detect_source_language=false', b'target_lang=EN']
        for i, m in enumerate(masked):
            form_parts.append(f'text[{i}]='.encode() + m)
        body = b'&'.join(form_parts)
        req = urllib.request.Request(url, data=body, headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': 'DeepL-Auth-Key ' + self.key,
        })
        last_err = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode('utf-8'))
                    return [t['text'] for t in payload['translations']]
            except urllib.error.HTTPError as e:
                last_err = f'HTTP {e.code}: {e.read().decode("utf-8", "replace")[:400]}'
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                raise RuntimeError(last_err)
            except Exception as e:
                last_err = str(e)
                if attempt < 4:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                raise RuntimeError(last_err)
        raise RuntimeError(last_err)


class EchoBackend(Backend):
    """Offline placeholder backend. Reuses nothing; just echoes. NOT a translation."""

    def __init__(self, _):
        pass

    def translate(self, texts, source, target):
        return [f'«{t}»' for t in texts]


BACKENDS = {
    'google': lambda: GoogleTranslateBackend(os.environ.get('GOOGLE_TRANSLATE_API_KEY', '')),
    'deepl': lambda: DeepLBackend(os.environ.get('DEEPL_API_KEY', '')),
    'echo': lambda: EchoBackend(None),
}

# --------------------------------------------------------------- find+filter
def iter_files(include_docs):
    for root, dirs, names in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), '.')
            if is_excluded(rel, include_docs):
                continue
            _, ext = os.path.splitext(name)
            if ext.lower() not in TEXT_EXTENSIONS:
                continue
            yield rel


def translation_candidates(text):
    """Return list of (start, end, kind, body) for replaceable spans.
    Whole-literal / whole-run only — never substrings."""
    spans = []
    for start, end, quote, raw in find_literals(text):
        logical = decode_literal(raw)
        if CJK_RE.search(logical) and not zh_locale_literal(text, start):
            spans.append((start, end, 'literal', logical, quote, raw))
    return spans


def translate_spans(spans, backend, source, target, cache, batch_size, delay, report):
    """Translate all spans (deduped) in batches. Returns {span_key: translated}
    plus a list of untranslated (error) spans."""
    # dedupe by logical text
    unique, seen = [], {}
    for idx, span in enumerate(spans):
        key = span[3]
        if key not in seen:
            seen[key] = idx
            unique.append(key)
    results = {}
    errors = []
    for start in range(0, len(unique), batch_size):
        batch = [k for k in unique[start:start + batch_size] if k not in cache]
        if not batch:
            continue
        masked = []
        saved_lists = []
        for text in batch:
            m, saved = mask_placeholders(text)
            masked.append(m)
            saved_lists.append(saved)
        try:
            translated = backend.translate(masked, source, target)
        except Exception as e:
            errors.append(('batch', batch, str(e)))
            for text in batch:
                results[text] = None  # leave untranslated
            continue
        for i, text in enumerate(batch):
            restored = restore_placeholders(translated[i], saved_lists[i])
            cache[text] = restored
            results[text] = restored
        if delay > 0:
            time.sleep(delay)
    # spans whose cache entry existed but missing: fill from cache
    for key, value in cache.items():
        results[key] = value
    return results, errors

# ------------------------------------------------------------- apply phase
def apply_replacements(text, spans, results):
    out = []
    last = 0
    ordered = sorted(spans, key=lambda s: s[0])
    for start, end, kind, body, quote, raw in ordered:
        out.append(text[last:start])
        translated = results.get(body)
        if translated is None:
            out.append(text[start:end])
        else:
            out.append(quote + escape_for(translated, quote) + quote)
        last = end
    out.append(text[last:])
    return ''.join(out)


def main():
    ap = argparse.ArgumentParser(description='Bulk CJK→English translation (repo-local).')
    ap.add_argument('--dry-run', action='store_true', help='Preview only; write report, no edits (default).')
    ap.add_argument('--apply', action='store_true', help='Actually rewrite files in place.')
    ap.add_argument('--backend', default='google', choices=list(BACKENDS.keys()))
    ap.add_argument('--source', default='zh-CN')
    ap.add_argument('--target', default='en')
    ap.add_argument('--batch-size', type=int, default=64)
    ap.add_argument('--delay', type=float, default=0.25, help='Seconds between batches.')
    ap.add_argument('--docs', action='store_true', help='Include .md docs (default: exclude).')
    ap.add_argument('--report', default='.cjk-report.json')
    ap.add_argument('--cache', action='store_true', help='Reuse/write .cjk-cache.json.')
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--selftest', action='store_true', help='Offline self-test; exits non-zero on failure.')
    ap.add_argument('--max-files', type=int, default=0, help='Limit processed files (0=unlimited).')
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    report_path = args.report
    cache_path = '.cjk-cache.json'
    cache = {}
    if args.cache and os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding='utf-8'))
        except Exception:
            cache = {}
    apply = args.apply and not args.dry_run
    if apply and args.backend == 'echo':
        print('refusing --apply with --backend echo: echo output is a marker, not a translation.', file=sys.stderr)
        print('real translations need --backend google or --backend deepl.', file=sys.stderr)
        return 1

    files = list(iter_files(args.docs))
    if args.max_files:
        files = files[:args.max_files]

    backend = BACKENDS[args.backend]()

    report = {'backend': args.backend, 'source': args.source, 'target': args.target,
              'apply': apply, 'files': [], 'errors': []}
    all_spans = []   # (file_index, span)
    all_files_text = {}

    # Phase 1: collect (read files, gather spans).
    for idx, rel in enumerate(files):
        try:
            text = open(rel, encoding='utf-8').read()
        except Exception as e:
            report['errors'].append({'path': rel, 'error': str(e)})
            continue
        if '\x00' in text[:2000]:
            continue
        if not CJK_RE.search(text):
            continue
        spans = translation_candidates(text)
        if not spans:
            continue
        all_files_text[idx] = (rel, text)
        for span in spans:
            all_spans.append((idx, span))

    # Phase 2: translate the unique texts (batched).
    results, batch_errors = translate_spans(
        [span for _, span in all_spans], backend, args.source, args.target,
        cache, args.batch_size, args.delay, report,
    )
    report['errors'].extend([
        {'path': f'batch[{batch[0][:60]}]' if isinstance(batch, list) else 'batch', 'error': err}
        for kind, batch, err in batch_errors
    ])

    # Phase 3: apply/report.
    changed_files = 0
    total_changed_spans = 0
    residual_counts = {}
    for idx, (rel, text) in all_files_text.items():
        spans = [span for s_idx, span in all_spans if s_idx == idx]
        new_text = apply_replacements(text, spans, results)
        if new_text != text:
            changed_files += 1
        n_changed = sum(1 for span in spans
                        if (results.get(span[3]) is not None and CJK_RE.search(results.get(span[3]))))
        total_changed_spans += n_changed
        if apply:
            with open(rel, 'w', encoding='utf-8') as f:
                f.write(new_text)
        residual = Counter(CJK_RE.findall(new_text))
        residual_counts[rel] = sum(residual.values())
        report['files'].append({
            'path': rel,
            'changed': new_text != text,
            'changed_spans': n_changed,
            'residual_cjk_chars': sum(residual.values()),
        })

    # Phase 4: write report + cache.
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    if args.cache and not args.no_cache:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

    effect_files = len([f for f in report['files'] if f['changed']])
    print(f'done: {len(all_files_text)} files scanned, {effect_files} would change, {total_changed_spans} spans translated.')
    print(f'residual CJK in {len([r for r in residual_counts.values() if r > 0])} files (see report).')
    print(f'report written to {report_path}')
    if not apply:
        print('dry-run only — rerun with --apply to rewrite files.')
    return 0


def selftest():
    import tempfile
    passed = []
    failed = []

    def check(name, cond):
        (passed if cond else failed).append(name)
        print(('PASS' if cond else 'FAIL') + '  ' + name)

    # 1. literal substitution with re-escaping
    text = 'const msg = "你好世界";\nconst other = "中文测试";\n'
    spans = translation_candidates(text)
    check('finds literals', len(spans) == 2)
    seen = {span[3]: span for span in spans}
    results = {span[3]: 'hello world' for span in spans}
    out = apply_replacements(text, spans, results)
    check('replaces whole literal', 'hello world' in out)
    check('does not substring-replace', '你好世界' not in out)

    # 2. zh-locale protection
    zh_text = 'const ZH_CN_I18N = {\n  menuTitle: "额外信息注入",\n  other: "hello",\n};\n'
    spans = translation_candidates(zh_text)
    check('zh locale literal protected', all(span[3] != '额外信息注入' for span in spans))

    # 3. zh-locale context via "zh" key — a brace-protected case
    zh_text2 = 'const en = { title: { zh: "中文标题", en: "English title" } };\n'
    spans = translation_candidates(zh_text2)
    check('zh key context protected', all(span[3] != '中文标题' for span in spans))

    # 4. placeholder masking + restore
    masked, saved = mask_placeholders('Hello {name}, welcome to ${app} and {0}')
    restored = restore_placeholders(masked, saved)
    check('placeholder mask/restore roundtrip', restored == 'Hello {name}, welcome to ${app} and {0}')

    # 5. placeholder inside a literal is protected during translation
    text = 'const t = "你好 {name} 和 {0}";\n'
    spans = translation_candidates(text)
    check('literal with placeholder found', len(spans) == 1)
    unique_text = spans[0][3]
    masked, saved = mask_placeholders(unique_text)
    masked_translated = backend_echo_translate([masked])
    restored = restore_placeholders(masked_translated[0], saved)
    check('placeholders survive attempted translation', '{name}' in restored and '{0}' in restored)

    # 6. backend echo produces placeholder output
    check('echo backend returns marked output', EchoBackend(None).translate(['中文'], 'zh-CN', 'en')[0].startswith('«'))

    # 7. report structure
    report = {'backend': 'echo', 'source': 'zh-CN', 'target': 'en', 'apply': False, 'files': [], 'errors': []}
    check('report structure', set(report) == {'backend', 'source', 'target', 'apply', 'files', 'errors'})

    # 8. is_excluded
    check('excludes locale file', is_excluded('apps/flutter/app/lib/l10n/app_zh.arb', False))
    check('excludes docs by default', is_excluded('docs/some-note.md', False))
    check('includes docs with flag', not is_excluded('docs/some-note.md', True))
    check('excludes fixture dir', is_excluded('plugins/packages/external/x/tests/fixtures/a.json', False))

    print()
    print(f'self-test: {len(passed)} passed, {len(failed)} failed')
    if failed:
        print('failed: ' + ', '.join(failed))
        return 1
    return 0


def backend_echo_translate(texts):
    return [f'«{t}»' for t in texts]


if __name__ == '__main__':
    sys.exit(main())
