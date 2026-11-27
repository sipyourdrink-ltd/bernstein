// Minimal ANSI escape parser - strips control sequences and emits coloured
// segments for inline rendering.
//
// This intentionally implements a subset of ANSI SGR (Select Graphic
// Rendition): the 16 base colours, bright variants, and the most common
// attributes (bold, dim, italic, underline, reset). Anything else is dropped
// silently - coding agents almost never emit 256-colour or truecolor.

const ESC_RE = /\x1b\[((?:\d+;)*\d*)m/g;
const STRIP_RE = /\x1b\[[0-9;]*[A-Za-z]/g;

/** Removes every ANSI escape sequence from `s`. */
export function stripAnsi(s: string): string {
  return s.replace(STRIP_RE, '');
}

export interface AnsiSegment {
  text: string;
  /** Foreground colour token (CSS variable name fragment, e.g. `red`, `bright-cyan`). */
  fg: string | null;
  /** Background colour token. */
  bg: string | null;
  bold: boolean;
  dim: boolean;
  italic: boolean;
  underline: boolean;
}

const FG_BASE: Record<number, string> = {
  30: 'ansi-black',
  31: 'ansi-red',
  32: 'ansi-green',
  33: 'ansi-yellow',
  34: 'ansi-blue',
  35: 'ansi-magenta',
  36: 'ansi-cyan',
  37: 'ansi-white',
};

const FG_BRIGHT: Record<number, string> = {
  90: 'ansi-bright-black',
  91: 'ansi-bright-red',
  92: 'ansi-bright-green',
  93: 'ansi-bright-yellow',
  94: 'ansi-bright-blue',
  95: 'ansi-bright-magenta',
  96: 'ansi-bright-cyan',
  97: 'ansi-bright-white',
};

const BG_BASE: Record<number, string> = {
  40: 'ansi-black',
  41: 'ansi-red',
  42: 'ansi-green',
  43: 'ansi-yellow',
  44: 'ansi-blue',
  45: 'ansi-magenta',
  46: 'ansi-cyan',
  47: 'ansi-white',
};

const BG_BRIGHT: Record<number, string> = {
  100: 'ansi-bright-black',
  101: 'ansi-bright-red',
  102: 'ansi-bright-green',
  103: 'ansi-bright-yellow',
  104: 'ansi-bright-blue',
  105: 'ansi-bright-magenta',
  106: 'ansi-bright-cyan',
  107: 'ansi-bright-white',
};

const DEFAULT_SEG: AnsiSegment = Object.freeze({
  text: '',
  fg: null,
  bg: null,
  bold: false,
  dim: false,
  italic: false,
  underline: false,
});

function applyCode(seg: AnsiSegment, code: number): AnsiSegment {
  if (code === 0) {
    return { ...DEFAULT_SEG, text: '' };
  }
  if (code === 1) return { ...seg, bold: true };
  if (code === 2) return { ...seg, dim: true };
  if (code === 3) return { ...seg, italic: true };
  if (code === 4) return { ...seg, underline: true };
  if (code === 22) return { ...seg, bold: false, dim: false };
  if (code === 23) return { ...seg, italic: false };
  if (code === 24) return { ...seg, underline: false };
  if (code === 39) return { ...seg, fg: null };
  if (code === 49) return { ...seg, bg: null };
  if (FG_BASE[code]) return { ...seg, fg: FG_BASE[code] };
  if (FG_BRIGHT[code]) return { ...seg, fg: FG_BRIGHT[code] };
  if (BG_BASE[code]) return { ...seg, bg: BG_BASE[code] };
  if (BG_BRIGHT[code]) return { ...seg, bg: BG_BRIGHT[code] };
  return seg;
}

/**
 * Parses `s` into a sequence of coloured segments. Always returns at least
 * one segment (possibly with empty text + default styling) so callers can map
 * blindly. Any ANSI escape that doesn't match an SGR pattern is dropped.
 */
export function parseAnsi(s: string): AnsiSegment[] {
  if (s.length === 0) return [{ ...DEFAULT_SEG }];
  if (!s.includes('\x1b')) return [{ ...DEFAULT_SEG, text: s }];

  const segments: AnsiSegment[] = [];
  let cursor = 0;
  let style: AnsiSegment = { ...DEFAULT_SEG };
  ESC_RE.lastIndex = 0;

  let match: RegExpExecArray | null;
  while ((match = ESC_RE.exec(s)) !== null) {
    if (match.index > cursor) {
      const text = s.slice(cursor, match.index);
      segments.push({ ...style, text });
    }
    cursor = match.index + match[0].length;
    const params = match[1];
    const codes = params === '' ? [0] : params.split(';').map((p) => Number.parseInt(p, 10) || 0);
    for (let i = 0; i < codes.length; i++) {
      const code = codes[i];
      if (code === 38 || code === 48) {
        const mode = codes[i + 1];
        if (mode === 5) i += 2; // Mode and palette index.
        else if (mode === 2) i += 4; // Mode and RGB channels.
        continue;
      }
      style = applyCode(style, code);
    }
  }
  if (cursor < s.length) {
    segments.push({ ...style, text: s.slice(cursor) });
  }
  // Drop any non-SGR escapes that survived (cursor moves, clear-screen, etc.).
  return segments
    .map((seg) => ({ ...seg, text: seg.text.replace(STRIP_RE, '') }))
    .filter((seg) => seg.text.length > 0);
}
