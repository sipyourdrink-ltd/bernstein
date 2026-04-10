"""Internationalisation (i18n) foundation for the CLI.

Provides a lightweight built-in translation layer.  Translations are
embedded directly so that Bernstein ships with basic multi-language
support without requiring external locale files.
"""

from __future__ import annotations

import os

SUPPORTED_LOCALES: frozenset[str] = frozenset({"en", "es", "zh", "ja", "de"})

# ---------------------------------------------------------------------------
# Built-in translations
# ---------------------------------------------------------------------------

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "status.running": "Running",
        "status.idle": "Idle",
        "status.stopped": "Stopped",
        "status.error": "Error",
        "status.complete": "Complete",
        "task.created": "Task created",
        "task.started": "Task started",
        "task.succeeded": "Task succeeded",
        "task.failed": "Task failed",
        "error.budget_exceeded": "Budget exceeded",
        "error.no_agents": "No agents available",
        "error.spawn_failed": "Agent spawn failed",
        "error.timeout": "Operation timed out",
        "help.run": "Start an orchestrator run",
        "help.stop": "Stop the orchestrator",
        "help.status": "Show current status",
        "help.agents": "List active agents",
        "msg.welcome": "Welcome to {product_name}",
        "msg.shutdown": "Shutting down",
        "msg.tasks_remaining": "{count} tasks remaining",
    },
    "es": {
        "status.running": "Ejecutando",
        "status.idle": "Inactivo",
        "status.stopped": "Detenido",
        "status.error": "Error",
        "status.complete": "Completo",
        "task.created": "Tarea creada",
        "task.started": "Tarea iniciada",
        "task.succeeded": "Tarea exitosa",
        "task.failed": "Tarea fallida",
        "error.budget_exceeded": "Presupuesto excedido",
        "error.no_agents": "No hay agentes disponibles",
        "error.spawn_failed": "Error al crear agente",
        "error.timeout": "Tiempo de espera agotado",
        "help.run": "Iniciar una ejecucion",
        "help.stop": "Detener el orquestador",
        "help.status": "Mostrar estado actual",
        "help.agents": "Listar agentes activos",
        "msg.welcome": "Bienvenido a {product_name}",
        "msg.shutdown": "Apagando",
        "msg.tasks_remaining": "{count} tareas restantes",
    },
    "zh": {
        "status.running": "运行中",
        "status.idle": "空闲",
        "status.stopped": "已停止",
        "status.error": "错误",
        "status.complete": "完成",
        "task.created": "任务已创建",
        "task.started": "任务已开始",
        "task.succeeded": "任务成功",
        "task.failed": "任务失败",
        "error.budget_exceeded": "预算已超",
        "error.no_agents": "没有可用代理",
        "error.spawn_failed": "代理启动失败",
        "error.timeout": "操作超时",
        "help.run": "启动编排运行",
        "help.stop": "停止编排器",
        "help.status": "显示当前状态",
        "help.agents": "列出活跃代理",
        "msg.welcome": "欢迎使用 {product_name}",
        "msg.shutdown": "正在关闭",
        "msg.tasks_remaining": "剩余 {count} 个任务",
    },
    "ja": {
        "status.running": "実行中",
        "status.idle": "待機中",
        "status.stopped": "停止",
        "status.error": "エラー",
        "status.complete": "完了",
        "task.created": "タスク作成済み",
        "task.started": "タスク開始",
        "task.succeeded": "タスク成功",
        "task.failed": "タスク失敗",
        "error.budget_exceeded": "予算超過",
        "error.no_agents": "利用可能なエージェントなし",
        "error.spawn_failed": "エージェント起動失敗",
        "error.timeout": "操作タイムアウト",
        "help.run": "オーケストレーション実行を開始",
        "help.stop": "オーケストレーターを停止",
        "help.status": "現在の状態を表示",
        "help.agents": "アクティブなエージェントを一覧",
        "msg.welcome": "{product_name} へようこそ",
        "msg.shutdown": "シャットダウン中",
        "msg.tasks_remaining": "残りタスク {count} 件",
    },
    "de": {
        "status.running": "Laeuft",
        "status.idle": "Bereit",
        "status.stopped": "Gestoppt",
        "status.error": "Fehler",
        "status.complete": "Abgeschlossen",
        "task.created": "Aufgabe erstellt",
        "task.started": "Aufgabe gestartet",
        "task.succeeded": "Aufgabe erfolgreich",
        "task.failed": "Aufgabe fehlgeschlagen",
        "error.budget_exceeded": "Budget ueberschritten",
        "error.no_agents": "Keine Agenten verfuegbar",
        "error.spawn_failed": "Agent-Start fehlgeschlagen",
        "error.timeout": "Zeitlimit ueberschritten",
        "help.run": "Orchestrierung starten",
        "help.stop": "Orchestrierer stoppen",
        "help.status": "Aktuellen Status anzeigen",
        "help.agents": "Aktive Agenten auflisten",
        "msg.welcome": "Willkommen bei {product_name}",
        "msg.shutdown": "Herunterfahren",
        "msg.tasks_remaining": "{count} Aufgaben verbleibend",
    },
}


def get_locale() -> str:
    """Determine the active locale from environment variables.

    Checks ``BERNSTEIN_LANG`` first, then ``LC_ALL``, then ``LANG``.
    Returns the two-letter language code, defaulting to ``"en"``.

    Returns:
        Two-letter locale code (e.g. ``"en"``, ``"es"``).
    """
    for var in ("BERNSTEIN_LANG", "LC_ALL", "LANG"):
        value = os.environ.get(var, "")
        if value:
            # Handle formats like "en_US.UTF-8" or "en"
            lang = value.split("_")[0].split(".")[0].lower()
            if lang in SUPPORTED_LOCALES:
                return lang
    return "en"


def t(key: str, locale: str | None = None, **kwargs: str) -> str:
    """Translate a message key.

    Falls back to English when *locale* lacks the key, and returns
    the raw *key* when English also lacks it.

    Args:
        key: Dot-delimited translation key (e.g. ``"status.running"``).
        locale: Two-letter locale override; auto-detected when ``None``.
        **kwargs: Interpolation variables passed to ``str.format()``.

    Returns:
        Translated (and interpolated) string.
    """
    resolved_locale = locale if locale is not None else get_locale()
    translations = _TRANSLATIONS.get(resolved_locale, {})
    text = translations.get(key)

    if text is None and resolved_locale != "en":
        text = _TRANSLATIONS.get("en", {}).get(key)

    if text is None:
        return key

    if kwargs:
        try:
            return text.format(**kwargs)
        except KeyError:
            return text

    return text


def available_locales() -> list[str]:
    """Return sorted list of locale codes with translations.

    Returns:
        Sorted list of available locale codes.
    """
    return sorted(SUPPORTED_LOCALES)
