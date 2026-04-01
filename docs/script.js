/* Bernstein Docs — script.js */
(function () {
  'use strict';

  var STORAGE_KEY = 'bernstein-theme';
  var html = document.documentElement;
  var LEGACY_ICON_MAP = {
    '\u2139\ufe0f': 'info',
    '\u2139': 'info',
    '\u2705': 'check-circle-2',
    '\u2713': 'check',
    '\u274c': 'x-circle',
    '\u26a0\ufe0f': 'triangle-alert',
    '\u26a0': 'triangle-alert',
    '\u2630': 'menu',
    '\u2715': 'x',
    '\u25d1': 'moon-star',
    '\u25d0': 'moon',
    '\u25cf': 'circle',
    '\u25cb': 'circle'
  };

  function replaceLegacySymbols(root) {
    var symbols = Object.keys(LEGACY_ICON_MAP).sort(function (a, b) {
      return b.length - a.length;
    });
    var selector = 'span,td,th,button,a,p,li,h1,h2,h3,h4,h5,h6';
    root.querySelectorAll(selector).forEach(function (el) {
      if (el.closest('pre, code') || el.querySelector('svg, [data-lucide]')) {
        return;
      }
      var raw = (el.textContent || '').trim();
      if (!raw) return;

      var matchedSymbol = '';
      for (var i = 0; i < symbols.length; i += 1) {
        if (raw === symbols[i] || raw.indexOf(symbols[i] + ' ') === 0 || raw.indexOf(symbols[i] + '(') === 0) {
          matchedSymbol = symbols[i];
          break;
        }
      }
      if (!matchedSymbol) return;

      var iconName = LEGACY_ICON_MAP[matchedSymbol];
      var remainder = raw.slice(matchedSymbol.length).replace(/^\s+/, '');
      el.textContent = '';
      var icon = document.createElement('i');
      icon.setAttribute('data-lucide', iconName);
      icon.setAttribute('aria-hidden', 'true');
      el.appendChild(icon);
      if (remainder) {
        el.appendChild(document.createTextNode(' ' + remainder));
      }
    });
  }

  function isDark() {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved) return saved === 'dark';
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function applyTheme(dark) {
    html.setAttribute('data-theme', dark ? 'dark' : 'light');
    localStorage.setItem(STORAGE_KEY, dark ? 'dark' : 'light');

    var link = document.getElementById('hljs-theme');
    if (link) {
      link.href = dark
        ? 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css'
        : 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css';
    }

    var icon = document.getElementById('theme-icon');
    if (icon) {
      icon.setAttribute('data-lucide', dark ? 'sun' : 'moon-star');
      if (typeof lucide !== 'undefined') {
        lucide.createIcons();
      }
    }
  }

  // Apply before paint to avoid flash
  applyTheme(isDark());

  document.addEventListener('DOMContentLoaded', function () {
    replaceLegacySymbols(document);

    // Initialize Lucide icons
    if (typeof lucide !== 'undefined') {
      lucide.createIcons();
    }

    // Theme toggle
    var btn = document.getElementById('theme-toggle');
    if (btn) {
      btn.addEventListener('click', function () {
        applyTheme(html.getAttribute('data-theme') !== 'dark');
      });
    }

    // Scroll-reveal animations
    if ('IntersectionObserver' in window) {
      var obs = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) {
            e.target.classList.add('visible');
            obs.unobserve(e.target);
          }
        });
      }, { threshold: 0.08, rootMargin: '0px 0px -40px 0px' });

      document.querySelectorAll('.animate').forEach(function (el) {
        obs.observe(el);
      });
    } else {
      document.querySelectorAll('.animate').forEach(function (el) {
        el.classList.add('visible');
      });
    }

    // Syntax highlighting (skip terminal lines)
    if (typeof hljs !== 'undefined') {
      document.querySelectorAll('pre code:not(.no-hljs)').forEach(function (block) {
        hljs.highlightElement(block);
      });
    }

    // Mobile nav toggle
    var navToggle = document.getElementById('nav-toggle');
    var navLinks = document.querySelector('.nav-links');
    if (navToggle && navLinks) {
      navToggle.addEventListener('click', function () {
        navLinks.classList.toggle('open');
        var navIcon = document.getElementById('nav-toggle-icon');
        if (navIcon) {
          navIcon.setAttribute('data-lucide', navLinks.classList.contains('open') ? 'x' : 'menu');
          if (typeof lucide !== 'undefined') {
            lucide.createIcons();
          }
        }
      });
    }

    // Active nav link based on current page
    var page = window.location.pathname.split('/').pop() || 'index.html';
    document.querySelectorAll('.nav-link[data-page]').forEach(function (link) {
      if (link.getAttribute('data-page') === page) {
        link.classList.add('active');
      }
    });
  });
})();
