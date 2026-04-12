"""Internationalisation (i18n) foundation for the CLI.

Provides a lightweight built-in translation layer.  Translations are
embedded directly so that Bernstein ships with basic multi-language
support without requiring external locale files.
"""

from __future__ import annotations

import os

SUPPORTED_LOCALES: frozenset[str] = frozenset({"en", "es", "zh", "ja", "de"})

# ---------------------------------------------------------------------------
# Translation key constants
# ---------------------------------------------------------------------------

KEY_STATUS_RUNNING = "status.running"
KEY_STATUS_IDLE = "status.idle"
KEY_STATUS_STOPPED = "status.stopped"
KEY_STATUS_ERROR = "status.error"
KEY_STATUS_COMPLETE = "status.complete"
KEY_TASK_CREATED = "task.created"
KEY_TASK_STARTED = "task.started"
KEY_TASK_SUCCEEDED = "task.succeeded"
KEY_TASK_FAILED = "task.failed"
KEY_ERROR_BUDGET_EXCEEDED = "error.budget_exceeded"
KEY_ERROR_NO_AGENTS = "error.no_agents"
KEY_ERROR_SPAWN_FAILED = "error.spawn_failed"
KEY_ERROR_TIMEOUT = "error.timeout"
KEY_HELP_RUN = "help.run"
KEY_HELP_STOP = "help.stop"
KEY_HELP_STATUS = "help.status"
KEY_HELP_AGENTS = "help.agents"
KEY_MSG_WELCOME = "msg.welcome"
KEY_MSG_SHUTDOWN = "msg.shutdown"
KEY_MSG_TASKS_REMAINING = "msg.tasks_remaining"

# ---------------------------------------------------------------------------
# Built-in translations
# ---------------------------------------------------------------------------

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        KEY_STATUS_RUNNING: "Running",
        KEY_STATUS_IDLE: "Idle",
        KEY_STATUS_STOPPED: "Stopped",
        KEY_STATUS_ERROR: "Error",
        KEY_STATUS_COMPLETE: "Complete",
        KEY_TASK_CREATED: "Task created",
        KEY_TASK_STARTED: "Task started",
        KEY_TASK_SUCCEEDED: "Task succeeded",
        KEY_TASK_FAILED: "Task failed",
        KEY_ERROR_BUDGET_EXCEEDED: "Budget exceeded",
        KEY_ERROR_NO_AGENTS: "No agents available",
        KEY_ERROR_SPAWN_FAILED: "Agent spawn failed",
        KEY_ERROR_TIMEOUT: "Operation timed out",
        KEY_HELP_RUN: "Start an orchestrator run",
        KEY_HELP_STOP: "Stop the orchestrator",
        KEY_HELP_STATUS: "Show current status",
        KEY_HELP_AGENTS: "List active agents",
        KEY_MSG_WELCOME: "Welcome to {product_name}",
        KEY_MSG_SHUTDOWN: "Shutting down",
        KEY_MSG_TASKS_REMAINING: "{count} tasks remaining",
    },
    "es": {
        KEY_STATUS_RUNNING: "Ejecutando",
        KEY_STATUS_IDLE: "Inactivo",
        KEY_STATUS_STOPPED: "Detenido",
        KEY_STATUS_ERROR: "Error",
        KEY_STATUS_COMPLETE: "Completo",
        KEY_TASK_CREATED: "Tarea creada",
        KEY_TASK_STARTED: "Tarea iniciada",
        KEY_TASK_SUCCEEDED: "Tarea exitosa",
        KEY_TASK_FAILED: "Tarea fallida",
        KEY_ERROR_BUDGET_EXCEEDED: "Presupuesto excedido",
        KEY_ERROR_NO_AGENTS: "No hay agentes disponibles",
        KEY_ERROR_SPAWN_FAILED: "Error al crear agente",
        KEY_ERROR_TIMEOUT: "Tiempo de espera agotado",
        KEY_HELP_RUN: "Iniciar una ejecucion",
        KEY_HELP_STOP: "Detener el orquestador",
        KEY_HELP_STATUS: "Mostrar estado actual",
        KEY_HELP_AGENTS: "Listar agentes activos",
        KEY_MSG_WELCOME: "Bienvenido a {product_name}",
        KEY_MSG_SHUTDOWN: "Apagando",
        KEY_MSG_TASKS_REMAINING: "{count} tareas restantes",
    },
    "zh": {
        KEY_STATUS_RUNNING: "运行中",
        KEY_STATUS_IDLE: "空闲",
        KEY_STATUS_STOPPED: "已停止",
        KEY_STATUS_ERROR: "错误",
        KEY_STATUS_COMPLETE: "完成",
        KEY_TASK_CREATED: "任务已创建",
        KEY_TASK_STARTED: "任务已开始",
        KEY_TASK_SUCCEEDED: "任务成功",
        KEY_TASK_FAILED: "任务失败",
        KEY_ERROR_BUDGET_EXCEEDED: "预算已超",
        KEY_ERROR_NO_AGENTS: "没有可用代理",
        KEY_ERROR_SPAWN_FAILED: "代理启动失败",
        KEY_ERROR_TIMEOUT: "操作超时",
        KEY_HELP_RUN: "启动编排运行",
        KEY_HELP_STOP: "停止编排器",
        KEY_HELP_STATUS: "显示当前状态",
        KEY_HELP_AGENTS: "列出活跃代理",
        KEY_MSG_WELCOME: "欢迎使用 {product_name}",
        KEY_MSG_SHUTDOWN: "正在关闭",
        KEY_MSG_TASKS_REMAINING: "剩余 {count} 个任务",
    },
    "ja": {
        KEY_STATUS_RUNNING: "実行中",
        KEY_STATUS_IDLE: "待機中",
        KEY_STATUS_STOPPED: "停止",
        KEY_STATUS_ERROR: "エラー",
        KEY_STATUS_COMPLETE: "完了",
        KEY_TASK_CREATED: "タスク作成済み",
        KEY_TASK_STARTED: "タスク開始",
        KEY_TASK_SUCCEEDED: "タスク成功",
        KEY_TASK_FAILED: "タスク失敗",
        KEY_ERROR_BUDGET_EXCEEDED: "予算超過",
        KEY_ERROR_NO_AGENTS: "利用可能なエージェントなし",
        KEY_ERROR_SPAWN_FAILED: "エージェント起動失敗",
        KEY_ERROR_TIMEOUT: "操作タイムアウト",
        KEY_HELP_RUN: "オーケストレーション実行を開始",
        KEY_HELP_STOP: "オーケストレーターを停止",
        KEY_HELP_STATUS: "現在の状態を表示",
        KEY_HELP_AGENTS: "アクティブなエージェントを一覧",
        KEY_MSG_WELCOME: "{product_name} へようこそ",
        KEY_MSG_SHUTDOWN: "シャットダウン中",
        KEY_MSG_TASKS_REMAINING: "残りタスク {count} 件",
    },
    "de": {
        KEY_STATUS_RUNNING: "Laeuft",
        KEY_STATUS_IDLE: "Bereit",
        KEY_STATUS_STOPPED: "Gestoppt",
        KEY_STATUS_ERROR: "Fehler",
        KEY_STATUS_COMPLETE: "Abgeschlossen",
        KEY_TASK_CREATED: "Aufgabe erstellt",
        KEY_TASK_STARTED: "Aufgabe gestartet",
        KEY_TASK_SUCCEEDED: "Aufgabe erfolgreich",
        KEY_TASK_FAILED: "Aufgabe fehlgeschlagen",
        KEY_ERROR_BUDGET_EXCEEDED: "Budget ueberschritten",
        KEY_ERROR_NO_AGENTS: "Keine Agenten verfuegbar",
        KEY_ERROR_SPAWN_FAILED: "Agent-Start fehlgeschlagen",
        KEY_ERROR_TIMEOUT: "Zeitlimit ueberschritten",
        KEY_HELP_RUN: "Orchestrierung starten",
        KEY_HELP_STOP: "Orchestrierer stoppen",
        KEY_HELP_STATUS: "Aktuellen Status anzeigen",
        KEY_HELP_AGENTS: "Aktive Agenten auflisten",
        KEY_MSG_WELCOME: "Willkommen bei {product_name}",
        KEY_MSG_SHUTDOWN: "Herunterfahren",
        KEY_MSG_TASKS_REMAINING: "{count} Aufgaben verbleibend",
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
