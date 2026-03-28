/* Bernstein Docs — script.js */
(function () {
  'use strict';

  var STORAGE_KEY = 'bernstein-theme';
  var html = document.documentElement;

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
    if (icon) icon.textContent = dark ? '\u2600' : '\u25D1';
  }

  // Apply before paint to avoid flash
  applyTheme(isDark());

  document.addEventListener('DOMContentLoaded', function () {
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
        navToggle.textContent = navLinks.classList.contains('open') ? '✕' : '☰';
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
