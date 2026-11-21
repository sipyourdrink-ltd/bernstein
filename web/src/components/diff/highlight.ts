// Tiny, dependency-free syntax tokenizer. We deliberately avoid Prism here -
// the diff payload is usually < 2 MiB and the heavyweight grammars are
// overkill for a glanceable preview. Each language gets a small regex set
// that covers strings, comments, keywords, numbers, and a few common bits.
//
// Output is a flat list of `{ text, klass }` spans the renderer can wrap with
// `<span class={klass}>...</span>`. The class names map onto the Tailwind
// utility classes defined in the diff stylesheet below.

export type TokenKind =
  | 'plain'
  | 'comment'
  | 'string'
  | 'number'
  | 'keyword'
  | 'meta'
  | 'tag'
  | 'punct';

export interface Token {
  text: string;
  kind: TokenKind;
}

interface Rule {
  kind: TokenKind;
  re: RegExp;
}

const PY_KEYWORDS = new Set([
  'False','None','True','and','as','assert','async','await','break','class',
  'continue','def','del','elif','else','except','finally','for','from','global',
  'if','import','in','is','lambda','nonlocal','not','or','pass','raise','return',
  'try','while','with','yield','match','case','self','cls',
]);

const JS_KEYWORDS = new Set([
  'abstract','any','as','async','await','break','case','catch','class','const',
  'continue','debugger','declare','default','delete','do','else','enum','export',
  'extends','false','finally','for','from','function','if','implements','import',
  'in','instanceof','interface','keyof','let','new','null','of','package',
  'private','protected','public','readonly','return','satisfies','static',
  'super','switch','this','throw','true','try','type','typeof','undefined',
  'var','void','while','with','yield','namespace','module',
]);

const YAML_KEYWORDS = new Set(['true', 'false', 'null', 'yes', 'no', 'on', 'off']);

function makeRules(lang: string | null | undefined): Rule[] {
  switch (lang) {
    case 'python':
      return [
        { kind: 'comment', re: /^#[^\n]*/ },
        { kind: 'string', re: /^([rbuRBU]{0,2})("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')/ },
        { kind: 'number', re: /^0[xX][0-9a-fA-F_]+|^\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?/ },
        { kind: 'keyword', re: /^[A-Za-z_][A-Za-z0-9_]*/, /* filtered below */ } as Rule,
        { kind: 'punct', re: /^[(){}\[\],:;@.=+\-*/%<>!&|^~?]/ },
      ];
    case 'ts':
    case 'tsx':
    case 'js':
    case 'jsx':
      return [
        { kind: 'comment', re: /^\/\*[\s\S]*?\*\/|^\/\/[^\n]*/ },
        { kind: 'string', re: /^`(?:\\.|[^`\\])*`|^"(?:\\.|[^"\\\n])*"|^'(?:\\.|[^'\\\n])*'/ },
        { kind: 'number', re: /^0[xX][0-9a-fA-F_]+n?|^\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?n?/ },
        { kind: 'keyword', re: /^[A-Za-z_$][A-Za-z0-9_$]*/, } as Rule,
        { kind: 'punct', re: /^[(){}\[\],:;.=+\-*/%<>!&|^~?@]/ },
      ];
    case 'yaml':
      return [
        { kind: 'comment', re: /^#[^\n]*/ },
        { kind: 'string', re: /^"(?:\\.|[^"\\\n])*"|^'(?:\\.|[^'\\\n])*'/ },
        { kind: 'tag', re: /^[A-Za-z_][\w.-]*\s*(?=:)/ },
        { kind: 'number', re: /^-?\d+(?:\.\d+)?/ },
        { kind: 'keyword', re: /^[A-Za-z_][A-Za-z0-9_]*/, } as Rule,
        { kind: 'punct', re: /^[-:?#,\[\]{}|>]/ },
      ];
    case 'json':
      return [
        { kind: 'string', re: /^"(?:\\.|[^"\\\n])*"/ },
        { kind: 'number', re: /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/ },
        { kind: 'keyword', re: /^(?:true|false|null)\b/ },
        { kind: 'punct', re: /^[\[\]{},:]/ },
      ];
    case 'markdown':
      return [
        { kind: 'meta', re: /^#{1,6}\s[^\n]*/ },
        { kind: 'string', re: /^`[^`\n]*`|^\*\*[^*\n]+\*\*|^_[^_\n]+_/ },
        { kind: 'tag', re: /^\[[^\]\n]+\]\([^)\n]+\)/ },
        { kind: 'punct', re: /^[-*+>]/ },
      ];
    case 'bash':
    case 'shell':
      return [
        { kind: 'comment', re: /^#[^\n]*/ },
        { kind: 'string', re: /^"(?:\\.|[^"\\\n])*"|^'[^'\n]*'/ },
        { kind: 'meta', re: /^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?/ },
        { kind: 'keyword', re: /^(?:if|then|else|elif|fi|for|in|do|done|while|case|esac|function|return|exit|export|local)\b/ },
        { kind: 'number', re: /^\d+/ },
        { kind: 'punct', re: /^[|&;()<>]/ },
      ];
    default:
      return [];
  }
}

function isKeywordFor(lang: string | null | undefined, word: string): boolean {
  switch (lang) {
    case 'python':
      return PY_KEYWORDS.has(word);
    case 'ts':
    case 'tsx':
    case 'js':
    case 'jsx':
      return JS_KEYWORDS.has(word);
    case 'yaml':
      return YAML_KEYWORDS.has(word);
    default:
      return false;
  }
}

/**
 * Tokenize a single line. We never cross newline boundaries - every diff line
 * is highlighted in isolation, which keeps the renderer simple and predictable
 * for the kinds of small hunks we typically display.
 */
export function tokenize(line: string, lang: string | null | undefined): Token[] {
  if (!lang) return [{ text: line, kind: 'plain' }];
  const rules = makeRules(lang);
  if (rules.length === 0) return [{ text: line, kind: 'plain' }];

  const out: Token[] = [];
  let rest = line;
  let plain = '';

  outer: while (rest.length > 0) {
    for (const rule of rules) {
      const m = rule.re.exec(rest);
      if (m && m.index === 0) {
        const matched = m[0];
        // Identifier rule: only emit as keyword if it actually is one.
        if (rule.kind === 'keyword' && !isKeywordFor(lang, matched)) {
          plain += matched;
        } else {
          if (plain) {
            out.push({ text: plain, kind: 'plain' });
            plain = '';
          }
          out.push({ text: matched, kind: rule.kind });
        }
        rest = rest.slice(matched.length);
        continue outer;
      }
    }
    plain += rest[0];
    rest = rest.slice(1);
  }
  if (plain) out.push({ text: plain, kind: 'plain' });
  return out;
}

export function tokenClass(kind: TokenKind): string {
  switch (kind) {
    case 'comment':
      return 'text-meta-foreground italic';
    case 'string':
      return 'text-success/90';
    case 'number':
      return 'text-warning';
    case 'keyword':
      return 'text-primary font-medium';
    case 'meta':
      return 'text-primary';
    case 'tag':
      return 'text-accent font-medium';
    case 'punct':
      return 'text-meta-foreground';
    case 'plain':
    default:
      return '';
  }
}
