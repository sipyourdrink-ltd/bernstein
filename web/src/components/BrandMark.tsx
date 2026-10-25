// The Bernstein mark: a rounded hexagon with the terminal prompt `>_` cut
// through it, drawn as one even-odd path. Same geometry as
// docs/assets/brand/bernstein-mark.svg (the mask-based master) and the
// favicon in web/index.html; see docs/design/brand.md. Inline so the shell
// needs no asset fetch and the mark scales with the sidebar.
const MARK_PATH =
  'M 380.00 165.36 A 50 50 0 0 1 420.00 165.36 L 587.85 262.28 A 50 50 0 0 1 607.85 296.92 ' +
  'L 607.85 503.08 A 50 50 0 0 1 587.85 537.72 L 420.00 634.64 A 50 50 0 0 1 380.00 634.64 ' +
  'L 212.15 537.72 A 50 50 0 0 1 192.15 503.08 L 192.15 296.92 A 50 50 0 0 1 212.15 262.28 Z ' +
  'M 216 290 L 433 400 L 216 510 L 216 450 L 315 400 L 216 350 Z M 453 452 H 583 V 510 H 453 Z';

export const BRAND_AMBER = '#F5A524';

export function BrandMark({ size = 26, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="80 80 640 640"
      aria-hidden="true"
      focusable="false"
      className={className}
    >
      <path fill={BRAND_AMBER} fillRule="evenodd" d={MARK_PATH} />
    </svg>
  );
}
