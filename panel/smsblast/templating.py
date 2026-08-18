"""Подстановка полей контакта в шаблон сообщения."""

import re

PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def placeholders(template):
    """Список переменных, встречающихся в шаблоне."""
    seen = []
    for name in PLACEHOLDER_RE.findall(template):
        key = name.strip()
        if key and key not in seen:
            seen.append(key)
    return seen


def render(template, fields):
    """Заполняет {переменные}. Регистр и пробелы в имени не важны.

    Неизвестная переменная превращается в пустую строку — иначе клиент
    получит SMS с текстом «{имя}».
    """
    lowered = {str(k).strip().lower(): ("" if v is None else str(v))
               for k, v in (fields or {}).items()}

    def substitute(match):
        return lowered.get(match.group(1).strip().lower(), "")

    text = PLACEHOLDER_RE.sub(substitute, template)

    # Пустая подстановка оставляет мусор вида «Привет, !» или «Привет,!» —
    # подчищаем, иначе это уедет клиенту в таком виде.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,;:])(?=[.!?])", "", text)
    text = re.sub(r"([,;:])\1+", r"\1", text)
    return text.strip().strip(",;: ")


def missing_for(template, fields):
    """Переменные шаблона, которых нет у контакта."""
    lowered = {str(k).strip().lower() for k in (fields or {})}
    return [p for p in placeholders(template) if p.strip().lower() not in lowered]
