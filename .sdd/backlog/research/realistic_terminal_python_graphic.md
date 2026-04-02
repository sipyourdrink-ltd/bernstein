# Demoscene-quality terminal graphics for a Python CLI

**Bernstein can achieve near-pixel-quality graphics in modern terminals using a tiered detection-and-fallback strategy, with all critical libraries installable via pip.** The ecosystem has matured dramatically since 2024: Sixel support now spans Windows Terminal, VS Code, iTerm2, and WezTerm; the Kitty graphics protocol is gaining multi-terminal adoption; and pure-Python libraries like TerminalTextEffects deliver stunning animated text reveals with zero dependencies. The optimal architecture renders graphics with Pillow and outputs via the best available protocol — Kitty/iTerm2 inline images for pixel-perfect display, truecolor half-block characters for broad compatibility, and graceful degradation to 256-color or plain text. This report covers every technique, library, and protocol in depth, with concrete pip commands and code.

---

## 1. Pixel-level graphics protocols have converged on three standards

Three competing inline image protocols now cover **~90% of modern terminal emulators**. The practical path is detecting which protocol the user's terminal supports and dispatching accordingly.

### Sixel: the widest reach

Sixel encodes bitmaps as DCS escape sequences, splitting images into 6-pixel-high horizontal bands with up to **256 indexed colors**. A 200×100 image generates ~50–200KB of escape data. Modern libsixel encodes in **1–5ms**, but bandwidth is the bottleneck — Kitty/iTerm2 protocols are 3–10× more data-efficient.

**Terminal support matrix (2025–2026):**

| Terminal | Sixel | Kitty Protocol | iTerm2 (OSC 1337) | Platform |
|---|---|---|---|---|
| **WezTerm** | ✅ | ✅ | ✅ | All |
| **iTerm2** | ✅ (v3.3+) | ✅ | ✅ (native) | macOS |
| **Kitty** | ❌ | ✅ (full + animation) | ❌ | Linux, macOS |
| **Ghostty** | ❌ | ✅ | ❌ | macOS, Linux |
| **Windows Terminal** | ✅ (v1.23+) | ❌ | ❌ | Windows |
| **VS Code terminal** | ✅ (v1.80+) | ❌ | ✅ | All |
| **Konsole** | ✅ (22.04+) | ✅ (partial) | ✅ | Linux |
| **foot** | ✅ (v1.2+) | ❌ | ❌ | Linux (Wayland) |
| **Alacritty** | ❌ | ❌ | ❌ | All |
| **xterm** | ✅ (native) | ❌ | ❌ | Linux |
| **mintty** | ✅ (v2.6+) | ❌ | ✅ | Windows |
| **tmux** | ✅ | passthrough | passthrough | Multiplexer |

The recommended fallback chain: **Kitty protocol → iTerm2 protocol → Sixel → Unicode half-blocks → ASCII**.

### Kitty graphics protocol: highest fidelity

Kitty's APC-based protocol (`ESC _G <key=value pairs> ; <base64 payload> ESC \`) transmits PNG data directly — no color quantization, no resolution loss. It supports **chunked transfer** (splitting payloads >4096 bytes), **z-indexed layering**, **animation frames** (since Kitty 0.20), and **shared memory transmission** for zero-copy performance. Detection works via a query-response handshake before the DA1 response.

```python
import sys
from base64 import standard_b64encode

def display_image_kitty(path):
    with open(path, 'rb') as f:
        data = standard_b64encode(f.read())
    while data:
        chunk, data = data[:4096], data[4096:]
        m = 1 if data else 0
        cmd = f'\033_Ga=T,f=100,m={m};'.encode() + chunk + b'\033\\'
        sys.stdout.buffer.write(cmd)
    sys.stdout.flush()
```

### iTerm2 inline images (OSC 1337): simplest implementation

The OSC 1337 protocol is the easiest to implement — a single escape sequence with base64-encoded image data and dimension parameters. It supports PNG, JPEG, GIF (animated), and any macOS-native format. WezTerm, Konsole, VS Code, and mintty all support it alongside iTerm2 itself.

```python
import sys, base64

def display_image_iterm2(data: bytes, width="auto", height="auto"):
    b64 = base64.b64encode(data).decode()
    sys.stdout.write(f"\033]1337;File=inline=1;width={width};height={height}:{b64}\a")
    sys.stdout.flush()
```

### The all-in-one Python solution: `term-image`

**`pip install term-image`** (v0.7.2, pure Python, py3-none-any wheel) is the single most practical library for Bernstein. It auto-detects the terminal's best available protocol — Kitty, iTerm2, Sixel, or Unicode half-blocks — and renders accordingly. It accepts PIL Image objects directly:

```python
from term_image.image import AutoImage
from PIL import Image

img = Image.open("splash.png")
image = AutoImage(img)
image.draw()  # auto-detects best protocol
```

Tested on Kitty, iTerm2, WezTerm, GNOME Terminal, Windows Terminal, Konsole, Alacritty, Termux, and more. The library handles terminal size detection, aspect ratio correction, and animated GIF support.

---

## 2. Sub-character rendering achieves 160×96 effective resolution on a standard terminal

When pixel protocols are unavailable, Unicode characters with truecolor ANSI codes produce surprisingly detailed graphics. Three approaches trade off color fidelity against geometric resolution.

### Half-blocks: best for photographic images

Using U+2584 (▄) with independent foreground/background 24-bit colors yields **2 vertical pixels per character cell**. On an **80×24 terminal**, effective resolution is **80×48 = 3,840 color-independent pixels** — each pixel gets its own RGB value. This is the technique Rich, rich-pixels, and most terminal image viewers use.

```python
from PIL import Image
import shutil

def render_halfblocks(img_path):
    cols = shutil.get_terminal_size().columns
    img = Image.open(img_path).convert("RGB")
    ratio = cols / img.width
    new_h = int(img.height * ratio) // 2 * 2  # must be even
    img = img.resize((cols, new_h), Image.LANCZOS)
    px = img.load()
    for y in range(0, new_h, 2):
        row = []
        for x in range(cols):
            r1, g1, b1 = px[x, y]      # top → background
            r2, g2, b2 = px[x, y + 1]  # bottom → foreground
            row.append(f"\033[48;2;{r1};{g1};{b1}m\033[38;2;{r2};{g2};{b2}m▄")
        print("".join(row) + "\033[0m")
```

**Key libraries:** `rich-pixels` (`pip install rich-pixels`) renders PIL images as Rich renderables using this exact technique. `textual-image` (`pip install textual-image`) adds auto-protocol detection with half-block fallback.

### Braille patterns: best for line art and plots

Each Braille character (U+2800–U+28FF) encodes a **2×4 dot grid** — 8 sub-pixels per cell. Effective resolution on 80×24: **160×96 = 15,360 sub-pixels**, which is **4× the geometric resolution** of half-blocks. The tradeoff: only 2 colors per cell (foreground/background), so color bleeds across 8 dots. Ideal for monochrome line drawings, plots, and graphs — terrible for photographs.

**`drawille`** (by asciimoo, `pip install drawille`) provides the classic Canvas API with `set(x, y)` / `unset(x, y)` for Braille drawing. **`PyDrawille`** (`pip install PyDrawille`, Apache-2.0, released 2025) is a modern reimplementation avoiding drawille's AGPL license. Both are pure Python.

### Sextants: the balanced middle ground

Unicode 13.0 sextant characters (U+1FB00–U+1FB3B) divide cells into a **2×3 grid** of filled rectangles — 6 sub-pixels with solid fill (vs. Braille's dots). Effective resolution: **160×72 = 11,520 sub-pixels**. Sextants produce smoother images than Braille because they fill rectangular sub-cells rather than leaving dot gaps. Notcurses' author Nick Black specifically notes that Braille "doesn't tend to work out very well for images" while sextants are preferred.

**Font support caveat:** Sextants require either terminal-native rendering (Kitty, WezTerm, Ghostty, and foot draw block elements directly) or a font that includes them. JetBrains Mono and Fira Code do not natively include sextants. Use terminals with built-in glyph rendering for reliability.

| Technique | Grid/cell | Resolution (80×24) | Colors/cell | Best for |
|---|---|---|---|---|
| Half-blocks (1×2) | 1×2 | 80×48 | 2 independent | **Color images** |
| Sextants (2×3) | 2×3 | 160×72 | 2 shared | Shapes, icons |
| Braille (2×4) | 2×4 | 160×96 | 2 shared | **Line art, plots** |
| tiv's 4×8 matching | 4×8 | 320×192 | 2 shared | Best Unicode quality |

### Chafa: the gold-standard converter

**Chafa** (C library + Python bindings) is the highest-quality image-to-terminal converter. It optimizes across multiple Unicode ranges simultaneously, supports Sixel/Kitty/iTerm2 output, and uses DIN99d perceptual color space. Python bindings: `pip install chafa.py` (binary wheels for macOS arm64/x86_64, Linux, Windows). Requires system `libchafa` and ImageMagick's MagickWand.

---

## 3. Animation at 30–60 FPS is achievable with the right techniques

Modern GPU-accelerated terminals can parse and render ANSI at **135–400+ FPS** (benchmarked via doom-fire-zig). The practical Python ceiling is **30–60 FPS** depending on scene complexity, limited primarily by Python's string construction overhead and `stdout.write()` syscalls.

### Three essential optimization techniques

**Synchronized output (Mode 2026)** prevents tearing by freezing terminal rendering during frame writes. Wrap each frame with `\033[?2026h` (begin) and `\033[?2026l` (end). Supported by Kitty, Alacritty (0.13+), WezTerm, Windows Terminal, iTerm2, foot, Ghostty, and Zellij. Terminals that don't understand the sequence silently ignore it — safe to emit unconditionally.

**Double buffering** means building the entire frame in a `StringIO` buffer and writing it in a **single `sys.stdout.write()` call**. This reduces syscalls from ~10,000 (per-cell writes) to 1 (bulk write), yielding **3–10× FPS improvement**. curses, asciimatics, Rich, and Textual all implement this internally.

**Dirty-rect rendering** compares the current frame buffer against the previous one and uses cursor movement (`\033[row;colH`) to update only changed cells. For scenes with 5% cell changes, this reduces data volume by **~20×**. curses does this automatically in its `refresh()` call.

```python
# Optimal animation frame pattern
import sys
from io import StringIO

def render_frame(frame_data):
    buf = StringIO()
    buf.write('\033[?2026h')   # begin synchronized update
    buf.write('\033[?25l')     # hide cursor
    buf.write('\033[H')        # cursor home
    buf.write(frame_data)      # pre-built frame string
    buf.write('\033[?2026l')   # end synchronized update
    sys.stdout.write(buf.getvalue())
    sys.stdout.flush()
```

### Animation libraries compared

**TerminalTextEffects (TTE)** is the standout discovery of this research. `pip install terminaltexteffects` — **pure Python, zero dependencies**, py3-none-any wheel. It provides **37+ built-in text animation effects** including Beams, Burn, Decrypt, Fireworks, Rain, Matrix, Spray, Swarm, VHSTape, Waves, BlackHole, and LaserEtch. Each effect supports configurable colors, speed, easing functions, and bezier curve paths. It targets **60 FPS** and uses standard ANSI sequences.

```python
from terminaltexteffects.effects.effect_beams import Beams

effect = Beams("BERNSTEIN")
with effect.terminal_output() as terminal:
    for frame in effect:
        terminal.print(frame)
```

**asciimatics** (`pip install asciimatics`, Apache 2.0, 3.6k GitHub stars) provides a scene/effect/renderer architecture with built-in Fire, Matrix, Particles (fireworks, explosions, rain), FigletText, Julia fractal, Stars, Mirage, and more. It uses internal double buffering at ~20 FPS. Cross-platform (curses on Unix, Win32 console API on Windows). The `Scene(effects, duration=150)` pattern enables timed splash screens that auto-exit.

**Textual** (`pip install textual`) supports CSS-like `styles.animate()` with easing functions and handles **10k widgets at ~45 FPS**. It uses virtual DOM diffing for efficient updates. Best for interactive TUI apps rather than raw frame animation.

**blessed** (`pip install blessed`) provides the thinnest Pythonic wrapper over terminal capabilities — `Terminal.move()`, `fullscreen()`, `hidden_cursor()`, `color_rgb()`, sixel detection via `does_sixel()`, and Kitty keyboard protocol support. Ideal for hand-rolled animation loops where you need maximum control.

---

## 4. The Pillow rendering pipeline is the practical sweet spot for splash screens

For a splash screen rendered once at startup, the most practical pipeline is: **render in Pillow → detect terminal → output via best available protocol**. This approach lets you use any TrueType font, anti-aliased text, gradients, effects, and compositing — then convert to terminal output in <100ms total.

### Pillow → terminal pipeline

```python
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import shutil

def create_splash():
    cols, rows = shutil.get_terminal_size()
    # Half-block: 2 vertical pixels per row
    w, h = cols, rows * 2

    img = Image.new('RGB', (w, h), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Gradient background
    for y in range(h):
        t = y / h
        draw.line([(0, y), (w, y)],
                  fill=(int(15 * t), int(10 + 30 * t), int(60 - 20 * t)))

    # Custom font text (any TTF)
    font = ImageFont.truetype("JetBrainsMono-Bold.ttf", 16)
    draw.text((w//2 - 60, h//2 - 8), "BERNSTEIN", fill=(0, 255, 160), font=font)

    # Optional glow: blur + composite
    glow = img.filter(ImageFilter.GaussianBlur(radius=3))
    img = Image.blend(img, glow, 0.4)

    return img
```

For Kitty/iTerm2 terminals, output the PIL image directly via `term-image` for pixel-perfect display. For other terminals, convert to half-blocks. The entire render+convert pipeline completes in **<100ms** — invisible at startup.

### GPU shader effects via ModernGL

For demoscene-quality CRT effects (barrel distortion, chromatic aberration, bloom, scanlines), **ModernGL** (`pip install moderngl`, pre-built wheels for all platforms) renders to an offscreen FBO via GLSL fragment shaders, reads pixels back, and converts to terminal output. This is the **highest quality pipeline** for complex visual effects, but adds GPU dependency.

```python
import moderngl
ctx = moderngl.create_context(standalone=True)  # headless, no window
fbo = ctx.framebuffer(color_attachments=[ctx.texture((256, 128), 3)])
fbo.use()
# ... render CRT shader ...
pixels = fbo.color_attachments[0].read()  # → bytes → PIL → terminal
```

Pillow alone handles simpler effects (gradients, blur, emboss) with zero GPU requirement and is sufficient for most splash screen needs.

---

## 5. CRT and retro effects can be layered progressively

The CRT aesthetic maps naturally to terminal constraints. Several effects are straightforward to implement with truecolor ANSI alone, without any image rendering pipeline.

**CRT power-on simulation** uses three phases: (1) a bright horizontal line appears at screen center, (2) the line expands vertically with decreasing brightness, (3) phosphor glow settles into the final background. Each phase uses `\033[row;1H` cursor positioning and `\033[48;2;R;G;Bm` background colors, running at 30 FPS over ~2 seconds total.

**Scanline simulation** alternates dimmed rows: even rows render at 60% brightness of their true color. With half-block rendering, this produces convincing CRT-style horizontal bands.

**Phosphor fade** uses exponential color decay from bright green/amber to black over 8 steps with increasing delays (30ms → 300ms), simulating P1 (green) or P3 (amber) phosphor persistence. The phosphor palette: `(0,255,65) → (0,200,50) → (0,140,35) → ... → (0,0,0)`.

**Chromatic aberration** in terminal is best achieved via the Pillow pipeline: split a rendered image into R/G/B channels, offset each by 1–2 pixels, recombine, then convert to half-blocks. Direct ANSI approximation renders the same text three times at shifted positions in red, green, and blue.

**Terminal-side CRT shaders** exist in Ghostty (custom GLSL shader support), Windows Terminal (`experimental.pixelShaderPath` for HLSL), and cool-retro-term (built-in OpenGL effects). Detection: check `$TERM_PROGRAM` or parent process name. These are "free" visual upgrades when available.

---

## 6. Fonts and large text rendering offer quick wins

**pyfiglet** (`pip install pyfiglet`, pure Python, **419 bundled FIGlet fonts**) renders large ASCII art text instantly. The best fonts for a modern CLI: `slant` (elegant italic), `small` (compact), `thin` (minimal), `banner3` (wide), `doom` (clean block), and `cybermedium` (techy). Combined with Rich's markup or TTE's animation engine, FIGlet text becomes a powerful splash element.

```python
import pyfiglet
from rich.console import Console
from rich.text import Text

console = Console()
ascii_art = pyfiglet.figlet_format("Bernstein", font="slant")
console.print(Text(ascii_art, style="bold green"))
```

**Nerd Fonts** provide **10,000+ extra glyphs** across 14 icon sets (Powerline, Devicons, Material Design, Font Awesome, Octicons, weather icons, etc.) in 67+ patched font families. Detection is imperfect — no escape sequence queries the active font. Best practice: check `NERD_FONT=1` env var or offer `--nerd-font` flag. The `nerdfonts` PyPI package provides name-to-character mappings.

**Custom font rendering via Pillow + inline image** is the ultimate technique: render any TrueType font at any size with anti-aliasing using `ImageFont.truetype()`, then output via Sixel/Kitty protocol. This sidesteps terminal font limitations entirely — any font, any size, any style.

---

## 7. The terminal graphics state of the art in 2026

The most visually impressive terminal projects pushing boundaries today:

**notcurses** (C, by Nick Black) remains the most powerful terminal graphics library — supporting Sixel, Kitty protocol, sextants, octants, Braille, video playback via FFmpeg, and z-ordered compositing planes. Its `notcurses-demo` showcases Mandelbrot fractals, sprite animations, and multimedia rendering. The Python bindings (`pip install notcurses`) require the C library pre-installed, making them **impractical for a pip-only tool** — but notcurses proves what's possible.

**TerminalTextEffects** (pure Python, zero deps) represents the most practical state-of-the-art for Python CLI tools. Its 37+ effects with bezier curves, particle systems, and easing functions produce genuinely impressive text reveals at 60 FPS.

**fastfetch** (successor to neofetch) demonstrates the gold-standard multi-protocol approach: it auto-detects and uses Kitty → Sixel → Chafa → ASCII for logo rendering, with caching in `~/.cache/`. This is exactly the pattern Bernstein should emulate.

**Demoscene communities** remain active: Blocktronics releases ANSI artpacks, Revision and Assembly hold annual text-art competitions, and artists like Andreas Gysin (ertdfgcvb) create generative terminal art. These communities use tools like PabloDraw, Moebius, and custom renderers.

---

## 8. Practical tiered architecture for Bernstein

Given constraints — pip-installable, Python 3.12+, cross-platform (macOS primary, Linux, Windows), diverse terminal emulators — here is the recommended tiered approach.

### Detection logic

```python
import os

def detect_tier():
    term_program = os.environ.get("TERM_PROGRAM", "")
    colorterm = os.environ.get("COLORTERM", "")
    kitty = os.environ.get("KITTY_WINDOW_ID")

    if kitty or term_program == "WezTerm":
        return "kitty_protocol"  # Tier 1
    if "iTerm" in term_program:
        return "iterm2_protocol"  # Tier 1
    if colorterm in ("truecolor", "24bit"):
        return "truecolor"       # Tier 2
    if "256color" in os.environ.get("TERM", ""):
        return "256color"        # Tier 3
    return "minimal"             # Tier 4
```

For robust Sixel detection, use `blessed`'s `Terminal.does_sixel()` which queries DA1 device attributes at runtime.

### Tier 1 — pixel-perfect (Kitty/iTerm2/Sixel terminals)

Render the splash screen in Pillow with custom font, gradient background, glow effects, and anti-aliasing. Output via `term-image`'s `AutoImage` which auto-selects the best protocol. Quality: **9/10**. This looks indistinguishable from a GUI application.

### Tier 2 — truecolor + Unicode half-blocks

Render in Pillow, convert to half-block characters with 24-bit ANSI colors. Effective resolution ~80×48 with 16.7M colors per pixel. Add scanline dimming for CRT aesthetic. Quality: **7/10**. Recognizably an image with good color fidelity.

### Tier 3 — 256-color terminal

Use pyfiglet for large ASCII text with 256-color ANSI codes. Add simple gradient coloring across characters. No image rendering — text-only with color. Quality: **4/10**. Clean and readable but not graphically impressive.

### Tier 4 — dumb terminal / CI / pipe

Static plain text: tool name, version, one-line description. Detect via `TERM=dumb`, `NO_COLOR` env var, or `not sys.stdout.isatty()`. Quality: **2/10**. Functional only.

### Ranked recommendations for the Bernstein splash screen

**1. Rich + pyfiglet + TerminalTextEffects** (recommended primary approach)
Render "BERNSTEIN" in pyfiglet, then animate it with TTE's Beams, Decrypt, or Sweep effect. Add Rich panels/borders for framing. **Visual quality: 8/10. Install: `pip install rich pyfiglet terminaltexteffects`. Pure Python, zero C deps, works everywhere, 60 FPS animation.** This is the highest-impact, lowest-risk option.

**2. Pillow + term-image for pixel-protocol splash**
Pre-render a beautiful splash image (gradient, custom font, glow) in Pillow, display via `term-image`'s auto-detected protocol. **Visual quality: 9/10 on supported terminals, graceful fallback to 7/10 half-blocks.** Install: `pip install pillow term-image`. Requires bundling or generating the splash image. Adds ~5MB for Pillow binary wheels.

**3. asciimatics for full-screen animated splash**
Use asciimatics' built-in Fire + FigletText + Stars effects for a 3–5 second cinematic splash. Duration-limited scenes auto-exit. **Visual quality: 8/10 for animation impressiveness.** Install: `pip install asciimatics`. Cross-platform but uses curses (Windows needs pywin32).

**4. Rich + rich-pixels for static image splash**
Render splash image, display as Rich renderable using half-block characters. Simple, well-integrated with existing Rich usage. **Visual quality: 7/10.** Install: `pip install rich rich-pixels pillow`. No animation.

**5. Chafa Python bindings for maximum Unicode quality**
Use chafa.py to convert splash image with optimal multi-symbol Unicode selection. Highest quality Unicode rendering available. **Visual quality: 8/10.** Install: `pip install chafa.py`. Binary wheels available but requires system libchafa — adds install friction.

### The winning stack

For Bernstein specifically, the **recommended implementation** combines approaches 1 and 2:

- **Default path:** Rich panel with pyfiglet "BERNSTEIN" text, colored with Rich markup gradient, optional TTE animation on first run. Zero exotic dependencies beyond what's already used.
- **Enhanced path:** If `term-image` detects Kitty/iTerm2/Sixel support, display a pre-rendered Pillow splash image at pixel resolution instead.
- **CI/pipe path:** Single-line text with version number.

This gives demoscene-level visual impact on capable terminals while remaining **100% pip-installable** with pure-Python core dependencies, degrades gracefully across all terminal types, and adds <200ms to startup time.

### Quick-reference library matrix

| Library | Quality | Animation | Pure Python | pip install | Cross-platform |
|---|---|---|---|---|---|
| **terminaltexteffects** | ★★★★★ | ✅ 60fps | ✅ Zero deps | ✅ | ✅ |
| **rich** | ★★★★ | ⚠️ Live only | ✅ | ✅ | ✅ |
| **pyfiglet** | ★★★ | ❌ Static | ✅ | ✅ | ✅ |
| **term-image** | ★★★★★ | ✅ GIF | ✅ (needs Pillow) | ✅ | ✅ |
| **rich-pixels** | ★★★★ | ❌ Static | ✅ (needs Pillow) | ✅ | ✅ |
| **asciimatics** | ★★★★★ | ✅ 20fps | ✅ | ✅ | ✅ (Win needs pywin32) |
| **blessed** | ★★★ | ✅ Manual | ✅ | ✅ | ✅ |
| **Pillow** | ★★★★ (render) | ❌ | ❌ (C ext) | ✅ (binary wheels) | ✅ |
| **chafa.py** | ★★★★★ | ❌ | ❌ (C ext) | ⚠️ Needs libchafa | ⚠️ Linux/macOS best |
| **notcurses** | ★★★★★ | ✅ | ❌ (C lib) | ❌ Needs system install | ❌ Linux primary |
| **drawille** | ★★★ | ❌ | ✅ | ✅ | ✅ |
| **plotext** | ★★★ | ❌ | ✅ | ✅ | ✅ |

The terminal graphics ecosystem in 2026 has reached a remarkable level of maturity. A pip-installable Python tool can now achieve visual quality that would have required a GUI application five years ago — the key is layered detection and graceful degradation. Bernstein's existing Rich foundation is the right starting point; adding TerminalTextEffects for animation and term-image for pixel-protocol rendering creates a splash screen that genuinely impresses while remaining rock-solid across every terminal users might throw at it.