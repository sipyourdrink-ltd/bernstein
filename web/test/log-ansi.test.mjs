import assert from 'node:assert/strict';
import test from 'node:test';

import { parseAnsi } from '../src/components/logs/ansi.ts';

test('test_unsupported_extended_colors_preserve_style', () => {
  const segments = parseAnsi('\x1b[4;38;5;31mX\x1b[1;32;44;48;2;0;22;24;3mY');
  assert.deepEqual(segments, [
    {
      text: 'X',
      fg: null,
      bg: null,
      bold: false,
      dim: false,
      italic: false,
      underline: true,
    },
    {
      text: 'Y',
      fg: 'ansi-green',
      bg: 'ansi-blue',
      bold: true,
      dim: false,
      italic: true,
      underline: true,
    },
  ]);
});
